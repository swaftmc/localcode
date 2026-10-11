"""Assert-based checks for trimming, tool-call parsing, path confinement and edit; exits non-zero on failure."""
import tempfile
from pathlib import Path

import localcode
from localcode import parse_tool_call, show, tool_edit, tool_grep, tool_read, trim


def count(msgs):
    return sum(len(m["content"]) for m in msgs) // 4 + 4 * len(msgs)


def turn(i, result_chars):
    return [{"role": "user", "content": f"question {i}"},
            {"role": "assistant", "content": '```tool\n{"tool": "read", "path": "a.py"}\n```'},
            {"role": "user", "content": f"[read result]\n" + "x" * result_chars, "tool": True},
            {"role": "assistant", "content": f"answer {i}"}]


system = {"role": "system", "content": "s" * 400}
latest = {"role": "user", "content": "the latest question"}

# Under budget: nothing changes, no note.
h = [system, *turn(1, 100), latest]
out, note = trim(h, 1000, count)
assert out == h and note == "", (out, note)

# Shortening old tool outputs is enough: nothing is dropped.
h = [system, *turn(1, 4000), *turn(2, 4000), latest]
out, note = trim(h, 600, count)
assert count(out) <= 600 and len(out) == len(h), (count(out), len(out))
assert note == "[trimmed: 2 old tool outputs shortened]", note
assert h[3]["content"].endswith("x" * 100) and len(h[3]["content"]) > 4000, "input history must not be mutated"

# Tight budget: oldest whole turns go, system prompt and latest user message stay.
h = [system, *turn(1, 4000), *turn(2, 4000), *turn(3, 4000), latest]
out, note = trim(h, 300, count)  # room for one shortened turn plus latest
assert count(out) <= 300, count(out)
assert out[0] == system and out[-1] == latest, out
assert "old messages dropped" in note and "shortened" in note, note
assert [m["role"] for m in out[1:]] == ["user", "assistant", "user", "assistant", "user"], [m["role"] for m in out]

# Inside one long tool loop: old reply/result pairs go, the newest result stays whole.
loop = [{"role": r, "content": c, **({"tool": True} if r == "user" else {})}
        for _ in range(4) for r, c in (("assistant", "call"), ("user", "y" * 2000))]
h = [system, latest, *loop]
out, note = trim(h, 900, count)
assert count(out) <= 900 and out[:2] == [system, latest] and out[-1] == loop[-1], (count(out), note)

# Valid calls, including small-model variants.
assert parse_tool_call('ok\n```tool\n{"tool": "read", "path": "a.py"}\n```\nmore')[:2] == ("read", {"path": "a.py"})
assert parse_tool_call('```json\n{"name": "ls", "arguments": {"path": "src"}}\n```')[:2] == ("ls", {"path": "src"})
assert parse_tool_call('<tool_call>\n{"name": "shell", "arguments": "{\\"command\\": \\"dir\\"}"}\n</tool_call>')[:2] == ("shell", {"command": "dir"})
assert parse_tool_call("```tool\n{'tool': 'grep', 'pattern': 'def ',}\n```")[:2] == ("grep", {"pattern": "def "})
assert parse_tool_call('```tool\n{"tool": "write", "path": "a.py", "content": "x = 1\ny = 2"}')[:2] == ("write", {"path": "a.py", "content": "x = 1\ny = 2"})
assert parse_tool_call('{"tool": "ls"}')[:2] == ("ls", {})
assert parse_tool_call("```shell\npython --version\n```\n")[:2] == ("shell", {"command": "python --version"})
assert parse_tool_call("Run this:\n```bash\npip install x\n```") is None, "shell blocks inside prose are examples"
text = 'a\n```tool\n{"tool": "ls"}\n```\n[ls result]\ninvented'
assert text[:parse_tool_call(text)[2]].endswith("```"), "end must point just past the tool block"

# A code fence inside a JSON string (writing Markdown) must not end the tool block.
md = "# X\n```\nrun\n```\n"
text = '```tool\n{"tool": "write", "path": "README.md", "content": "# X\\n```\\nrun\\n```\\n"}\n```\nafter'
name, args, end = parse_tool_call(text)
assert (name, args) == ("write", {"path": "README.md", "content": md}), args
assert text[:end].endswith("}\n```"), text[:end]
text = '<tool_call>{"name": "edit", "arguments": {"path": "a.md", "old": "```py", "new": "```python"}}</tool_call>'
assert parse_tool_call(text)[:2] == ("edit", {"path": "a.md", "old": "```py", "new": "```python"})
assert parse_tool_call(text)[2] == len(text)

# Permission prompts escape control characters so the user sees the real command.
assert show("dir\r\x1b[2Kecho ok\nnext\t1") == "dir\\r\\x1b[2Kecho ok\nnext\t1", show("dir\r\x1b[2Kecho ok\nnext\t1")

# File tools stay inside the working directory; edit fails clearly on 0 or 2+ matches and keeps CRLF.
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp, "proj")
    root.mkdir()
    Path(tmp, "outside.txt").write_text("secret\n")
    (root / "a.txt").write_bytes(b"x = 1\r\ny = 2\r\nx = 1\r\n")
    localcode.ROOT = root.resolve()
    for call, why in [(lambda: tool_read({"path": "../outside.txt"}), "outside the working directory"),
                      (lambda: tool_edit({"path": "a.txt", "old": "z", "new": "w"}), "not found"),
                      (lambda: tool_edit({"path": "a.txt", "old": "x = 1", "new": "w"}), "matches 2 times")]:
        try:
            call()
        except ValueError as e:
            assert why in str(e), e
        else:
            raise AssertionError(f"expected error: {why}")
    assert tool_edit({"path": "a.txt", "old": "y = 2", "new": "y = 3"}) == "edited a.txt"
    assert (root / "a.txt").read_bytes() == b"x = 1\r\ny = 3\r\nx = 1\r\n"
    try:
        (root / "link.txt").symlink_to(Path(tmp, "outside.txt"))
        assert tool_grep({"pattern": "secret"}) == "no matches", "grep must skip symlinks leading outside"
    except OSError:
        print("note: symlink check skipped (needs Developer Mode or admin on Windows)")

# A user message left unanswered (denial, interrupt, server error) is merged so roles keep alternating.
agent = localcode.Agent.__new__(localcode.Agent)
agent.history = [system, {"role": "user", "content": "[write result]\nThe user denied this action.", "tool": True}]
agent.ask_model = lambda: "done"
agent.turn("do something else")
assert [m["role"] for m in agent.history] == ["system", "user", "assistant"], agent.history
assert not agent.history[1].get("tool") and agent.history[1]["content"].endswith("do something else")

# Missing: plain answers and ordinary code blocks are not tool calls.
assert parse_tool_call("All done, the file is fixed.") is None
assert parse_tool_call("```python\nprint({'tool': 1})\n```") is None
assert parse_tool_call('```\n{"a": 1}\n```') is None

# Malformed: raise ValueError with a reason the model can act on.
for bad, why in [('```tool\n{"tool": "read", "path": }\n```', "bad JSON"),
                 ('```tool\nread a.py\n```', "no JSON"),
                 ('```tool\n{"tool": "delete", "path": "a.py"}\n```', "unknown tool"),
                 ('```tool\n{"tool": "edit", "path": "a.py", "old": "x"}\n```', "needs new"),
                 ('```tool\n{1, 2}\n```', "one JSON object")]:
    try:
        parse_tool_call(bad)
    except ValueError as e:
        assert why in str(e), (bad, e)
    else:
        raise AssertionError(f"no error for {bad!r}")

print("all checks passed")
