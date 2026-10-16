"""Assert-based checks for trimming, tool-call parsing, rendering, the turn loop, confinement and edit; exits non-zero on failure."""
import re
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # main() does the same; Windows consoles default to cp1252

import localcode
from localcode import (STY, Markdown, call_complete, call_summary, file_from_reply, parse_tool_call, show, tool_edit,
                       tool_grep, tool_read, trim)


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

# Early stop: only once a fenced or tagged call is closed; a reply that is only a shell block keeps streaming.
assert not call_complete('```tool\n{"tool": "write", "path": "a.md", "content": "# X\\n```')
assert not call_complete('```tool\n{"tool": "ls"}\n')
assert call_complete('```tool\n{"tool": "ls"}\n```')
assert call_complete('<tool_call>{"name": "ls", "arguments": {}}</tool_call>')
assert not call_complete("```shell\ndir\n```"), "prose may follow, which makes it an example"
assert not call_complete("All done.")


def render(text, styled=False, verbose=False):
    """Feed text to the renderer one char at a time, the hardest streaming case; return (output, renderer)."""
    out = []
    md = Markdown(out.append, styled, verbose)
    for ch in text:
        md.feed(ch)
    md.close()
    return "".join(out), md


def plain(s):
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", s)


# Unstyled (piped) output is the reply as written, minus a hidden tool block.
doc = "# Title\n- a **b** `c`\n> quote\n---\n```python\nx = 1\n```\n```json\n{\"a\": 1}\n```\n2*3 = 6\nend"
assert render(doc)[0] == doc + "\n", render(doc)[0]
call = '```tool\n{"tool": "write", "path": "a.md", "content": "# X\\n```\\n"}\n```'
assert render("Sure.\n" + call)[0] == "Sure.\n", render("Sure.\n" + call)[0]
assert render('```\n{"tool": "ls"}\n```')[0] == "", "a plain fence holding a call is a tool block"
assert render("Sure.\n" + call, verbose=True)[0] == "Sure.\n" + call + "\n", "verbose shows the raw block"

# Styled output: markers become styles, code gets a gutter, and a hidden call leaves a marker that status() replaces.
out, _ = render(doc, styled=True)
assert plain(out) == "Title\n• a b c\n│ quote\n" + "─" * 40 + "\n│ x = 1\n│ {\"a\": 1}\n2*3 = 6\nend\n", plain(out)
assert STY["bold"] + "b" in out and STY["code"] + "c" in out and STY["head"] + "Title" in out, out
assert plain(render("```hi```", styled=True)[0]) == "hi\n", "a one-line fence is inline code"
out, md = render("Sure.\n" + call, styled=True)
assert plain(out) == "Sure.\n● tool call…\n" and md.marker, plain(out)
md.status("● write a.md")
assert md.write.__self__[-1] == "\x1b[1A\x1b[2K● write a.md\n", "status must overwrite the marker line"
assert render('<tool_call>{"name": "ls", "arguments": {}}</tool_call>', styled=True)[1].marker
assert call_summary("write", {"path": "a.md", "content": "x"}) == "a.md (1 line)"
assert call_summary("write", {"path": "a.md"}) == "a.md", "a write call without content has no line count yet"
assert call_summary("read", {"path": "a.py", "start": 5}) == "a.py lines 5-end"


def agent_with(history, replies, allow=False, cut_off=()):
    """An Agent with no server: ask_model returns the given replies in order (those at cut_off positions are truncated)."""
    a = localcode.Agent.__new__(localcode.Agent)
    a.history, a.max_steps, a.allowed, a.asked, a.truncated = history, 10, set(), [], False
    a.max_prompt, a.ratio, a.max_tokens, a.ctx = 7000, 3.0, 2048, 8192
    a.md = Markdown(lambda s: None, False, False)
    replies, count = iter(replies), iter(range(1000))

    def ask(stop_at_call=True):
        a.truncated = next(count) in cut_off
        return next(replies)

    a.ask_model = ask
    a.permitted = lambda name, args, streamed=False: a.asked.append(name) or allow  # allow=False: the user says no
    return a


# A user message left unanswered (denial, interrupt, server error) is merged so roles keep alternating.
agent = agent_with([system, {"role": "user", "content": "[write result]\ndenied", "tool": True}], ["done"])
agent.turn("do something else")
assert [m["role"] for m in agent.history] == ["system", "user", "assistant"], agent.history
assert not agent.history[1].get("tool") and agent.history[1]["content"].endswith("do something else")

# A denied call goes back to the model, which may try something else; repeating the same call ends the turn.
write = '```tool\n{"tool": "write", "path": "a.py", "content": "x"}\n```'
agent = agent_with([system], [write, "OK, I won't write it."])
agent.turn("make a.py")
assert agent.asked == ["write"] and agent.history[-1]["content"] == "OK, I won't write it.", agent.history
assert localcode.DENIED in agent.history[-2]["content"], agent.history
agent = agent_with([system], [write, write, "never reached"])
agent.turn("make a.py")
assert agent.asked == ["write"], "a repeated denied call must not ask again"
assert [m["role"] for m in agent.history] == ["system", "user", "assistant", "user", "assistant"], agent.history

# Trimming applies to the copy sent, so the full history survives and a bigger budget brings it back.
agent = localcode.Agent.__new__(localcode.Agent)
agent.history = [system, *turn(1, 4000), *turn(2, 4000), latest]
agent.ratio, agent.last_note, agent.last_used, agent.max_prompt, sent = 4.0, "", None, 300, []
agent.stream = lambda msgs, stop_at_call=True: (sent.append(msgs), ("answer", None))[1]
agent.max_tokens, agent.ctx = 1024, 8192
full = [dict(m) for m in agent.history]
agent.ask_model()
assert agent.history == full, "the stored history must not be trimmed"
assert len(sent[0]) < len(full) and agent.count(sent[0]) <= 300, len(sent[0])
agent.max_prompt = 100_000
agent.ask_model()
assert len(sent[1]) == len(full), "raising max-prompt must send the whole history again"

# Temperature and max-steps are validated.
for t, s in [(-0.1, 10), (2.5, 10), (float("nan"), 10), (0.2, 0)]:
    try:
        agent.set_sampling(t, s)
    except ValueError:
        pass
    else:
        raise AssertionError(f"accepted temperature {t}, max-steps {s}")
agent.set_sampling(0.7, 3)
assert (agent.temperature, agent.max_steps) == (0.7, 3)

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

# A write call may carry only the path; the file text then arrives in a code block.
assert parse_tool_call('```tool\n{"tool": "write", "path": "a.py"}\n```')[:2] == ("write", {"path": "a.py"})
assert file_from_reply("```python\nprint(1)\n```") == "print(1)\n"
assert file_from_reply("Sure!\n```html\n<p>hi</p>\n```\nThat is the file.") == "<p>hi</p>\n"
assert file_from_reply("```\nno closing fence\nstill the file") == "no closing fence\nstill the file\n"
md_file = "# Doc\n```bash\nls\n```\ntext\n"
assert file_from_reply("```markdown\n" + md_file + "```") == md_file, "a Markdown file holding fences must stay whole"
assert file_from_reply("here is the code in plain text") is None
assert file_from_reply("```python\n\n```") is None, "an empty block is not a file"

with tempfile.TemporaryDirectory() as tmp:
    localcode.ROOT = Path(tmp).resolve()
    call = '```tool\n{"tool": "write", "path": "page.html"}\n```'
    body = "```html\n<h1>hi</h1>\n<p>there</p>\n```"

    # Two-step write: call, then the code block, then the model's closing answer. The file holds the real text.
    agent = agent_with([system], [call, body, "Done."], allow=True)
    agent.turn("make page.html")
    assert (Path(tmp) / "page.html").read_text() == "<h1>hi</h1>\n<p>there</p>\n", list(Path(tmp).iterdir())
    assert [m["role"] for m in agent.history] == ["system", "user", "assistant", "user", "assistant", "user", "assistant"]
    result = agent.history[-2]["content"]
    assert result.startswith("[write result]\nwrote 2 lines to page.html"), result
    assert 'The user asked: "make page.html"' in result, "the reminder after a tool must repeat the user's request"

    # A reply that was cut off at max-tokens is not written, and the model is told why.
    (Path(tmp) / "page.html").unlink()
    agent = agent_with([system], [call, body, "I will make it shorter."], allow=True, cut_off={1})
    agent.turn("make page.html")
    assert not (Path(tmp) / "page.html").exists(), "a cut-off file must not be written"
    assert "cut off at the token limit" in agent.history[-2]["content"], agent.history[-2]["content"]

    # A second cut-off file in one turn ends the turn with a plain answer instead of another long attempt.
    agent = agent_with([system], [call, body, call, body, "The file was too long."], allow=True, cut_off={1, 3})
    agent.turn("make page.html")  # a fifth ask for another attempt would raise StopIteration
    assert not (Path(tmp) / "page.html").exists()
    assert agent.history[-1] == {"role": "assistant", "content": "The file was too long."}, agent.history[-1]
    assert localcode.FINAL in agent.history[-2]["content"], "the turn must end with the final-answer request"
    roles = [m["role"] for m in agent.history]
    assert all(a != b for a, b in zip(roles[1:], roles[2:])), roles  # the roles keep alternating

    # A reply without a code block is not written either.
    agent = agent_with([system], [call, "Here is the page: it has a heading.", "Sorry."], allow=True)
    agent.turn("make page.html")
    assert not (Path(tmp) / "page.html").exists() and "no code block" in agent.history[-2]["content"]

    # The user's answer is asked only once the text is known, and a no leaves nothing behind.
    agent = agent_with([system], [call, body, "Understood."])
    agent.turn("make page.html")
    assert agent.asked == ["write"] and not (Path(tmp) / "page.html").exists()

    # content inside the call still works (short files, older habits).
    agent = agent_with([system], ['```tool\n{"tool": "write", "path": "b.txt", "content": "hello\\n"}\n```', "Done."], allow=True)
    agent.turn("make b.txt")
    assert (Path(tmp) / "b.txt").read_text() == "hello\n"

    # Two calls in one block: the first runs and the model is told the rest did not.
    (Path(tmp) / "a.txt").write_text("one\n")
    two = '```tool\n{"tool": "ls", "path": "."}\n{"tool": "read", "path": "a.txt"}\n```'
    agent = agent_with([system], [two, "Done."], allow=True)
    agent.turn("list the files, then read a.txt")
    assert "Only the first ran" in agent.history[-2]["content"], agent.history[-2]["content"]
    agent = agent_with([system], [call, body, "Done."], allow=True)
    agent.turn("make page.html")
    assert "Only the first ran" not in agent.history[3]["content"], "no note when there was only one call"

    # The reminder lists the tools used so far.
    agent = agent_with([system], ['```tool\n{"tool": "ls", "path": "."}\n```', "Done."], allow=True)
    agent.turn("list the files")
    assert "Tools you have used so far: ls." in agent.history[-2]["content"], agent.history[-2]["content"]

    # Repeating the same read-only call is answered with a note instead of running it again; a second repeat ends the turn.
    rd = '```tool\n{"tool": "read", "path": "a.txt"}\n```'
    agent = agent_with([system], [rd, rd, rd, "It says one."], allow=True)  # a fifth ask would raise StopIteration
    agent.turn("read a.txt")
    assert any(localcode.REPEAT in m["content"] for m in agent.history), "the first repeat must get the note"
    assert sum("[read result]\n[a.txt lines" in m["content"] for m in agent.history) == 1, "the read must run only once"
    assert agent.history[-1] == {"role": "assistant", "content": "It says one."}, "the second repeat asks for a final answer"
    assert localcode.FINAL in agent.history[-2]["content"] and agent.history[-2]["role"] == "user"
    roles = [m["role"] for m in agent.history]
    assert all(a != b for a, b in zip(roles[1:], roles[2:])), roles  # the roles keep alternating

    # Hitting the step cap also ends with a plain-text answer, and the capped call does not run.
    agent = agent_with([system], ['```tool\n{"tool": "ls", "path": "."}\n```', rd, '```tool\n{"tool": "grep", "pattern": "x"}\n```',
                                  "Two files."], allow=True)
    agent.max_steps = 2
    agent.turn("look around")
    assert agent.history[-1]["content"] == "Two files." and not any("[grep result]" in m["content"] for m in agent.history)

    # Listing again after a shell command is legitimate (it may have created files); an identical command is not.
    ls_call = '```tool\n{"tool": "ls", "path": "."}\n```'
    sh = '```tool\n{"tool": "shell", "command": "echo hi"}\n```'
    agent = agent_with([system], [ls_call, sh, ls_call, "Done."], allow=True)
    agent.turn("ls, run, ls")
    assert not any(localcode.REPEAT in m["content"] for m in agent.history), "state changed, so ls is not a repeat"

    # A change in between makes reading again legitimate.
    wr = '```tool\n{"tool": "write", "path": "c.txt", "content": "x\\n"}\n```'
    agent = agent_with([system], [rd, wr, rd, "Done."], allow=True)
    agent.turn("read, write, read")
    assert sum("[read result]\n[a.txt lines" in m["content"] for m in agent.history) == 2
    assert not any(localcode.REPEAT in m["content"] for m in agent.history)

    # A tool call cut off by max-tokens is reported as such, not as bad JSON.
    agent = agent_with([system], ['```tool\n{"tool": "write", "path": "z.py", "content": "def f():\\n  re', "ok"], cut_off={0})
    agent.turn("make z.py")
    assert "cut off at the token limit" in agent.history[-2]["content"], agent.history[-2]["content"]



class FakeResponse:
    """Stands in for the HTTP response of a streaming chat request: iterating it yields the given byte lines."""

    def __init__(self, lines):
        self.lines = lines

    def __enter__(self):
        return iter(self.lines)

    def __exit__(self, *exc):
        return False


def streamer(native):
    a = localcode.Agent.__new__(localcode.Agent)
    a.native, a.url, a.base, a.model = native, "http://x/v1", "http://x", "m"
    a.ctx, a.max_tokens, a.temperature, a.verbose, a.truncated = 4096, 512, 0.2, False, False
    return a


real_http, sent = localcode.http, {}
try:
    # Ollama native: thinking is switched off, num_ctx is sent, and done_reason "length" marks a cut-off reply.
    def ollama_http(url, body=None, timeout=10):
        sent.update(url=url, body=body)
        return FakeResponse([b'{"message": {"content": "hel"}, "done": false}\n',
                             b'{"message": {"content": "lo"}, "done": true, "done_reason": "length", "prompt_eval_count": 50}\n'])
    localcode.http = ollama_http
    a = streamer(True)
    assert a.stream([{"role": "user", "content": "hi"}]) == ("hello", 50) and a.truncated
    assert sent["url"] == "http://x/api/chat" and sent["body"]["think"] is False and sent["body"]["options"]["num_ctx"] == 4096, sent

    # OpenAI-compatible: finish_reason "length" marks a cut-off reply; "stop" does not.
    def openai_http(url, body=None, timeout=10, reason="length"):
        return FakeResponse([b'data: {"choices": [{"delta": {"content": "hi"}, "finish_reason": null}]}\n',
                             b'data: {"choices": [{"delta": {}, "finish_reason": "%s"}]}\n' % reason.encode(), b"data: [DONE]\n"])
    localcode.http = openai_http
    a = streamer(False)
    assert a.stream([{"role": "user", "content": "hi"}])[0] == "hi" and a.truncated
    localcode.http = lambda *args, **kw: openai_http(*args, reason="stop", **kw)
    a = streamer(False)
    assert a.stream([{"role": "user", "content": "hi"}])[0] == "hi" and not a.truncated
finally:
    localcode.http = real_http
print("all checks passed")
