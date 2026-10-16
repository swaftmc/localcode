"""localcode: a terminal coding agent tuned for small, locally hosted models."""
import argparse
import ast
import ctypes
import json
import os
import re
import signal
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

__version__ = "1.0.2"
ROOT = Path.cwd().resolve()
SHELL_TIMEOUT = 60      # seconds before a shell command is killed
OLD_TOOL_KEEP = 400     # chars an old tool output is shortened to when trimming
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv"}
SHELL_NAME = "cmd.exe" if os.name == "nt" else "/bin/sh"

SYSTEM = """You are localcode, a coding assistant working in {cwd} on {os}.
To use a tool, reply with ONE block like this, then stop and wait for the result:
```tool
{{"tool": "read", "path": "main.py"}}
```
Tools:
- read: {{"tool": "read", "path": "a.py", "start": 1, "end": 200}} (start/end optional)
- write: {{"tool": "write", "path": "a.py"}} (you are then asked for the complete file text)
- edit: {{"tool": "edit", "path": "a.py", "old": "exact text to replace", "new": "new text"}}
- ls: {{"tool": "ls", "path": "."}}
- grep: {{"tool": "grep", "pattern": "regex", "path": "."}}
- shell: {{"tool": "shell", "command": "python --version"}} (runs in {shell})
Rules:
- One tool call per reply. Never write the tool result yourself.
- Always write the complete finished file with all the real code, never a description of it.
- Read a file before editing it. "old" must match the file text exactly and only once.
- Paths are relative to the working directory.
- When the task is done, reply with no tool block: a short Markdown answer saying what you did.
- Never put a normal answer inside a code block."""

CORRECTION = """Your tool call was invalid: {err}
Either send one ```tool block using a tool from the list, or, if the task is done, reply in plain text with no tool block."""

NUDGE = ('[end of result] The user asked: "{task}"\nTools you have used so far: {used}.\n'
         "If a part of the request is still undone, call the tool for it now. If every part is done, answer the user "
         "in one sentence. Never repeat a tool call you already made.")

FINAL = "[stop] Answer the user's request now, in plain text, in one or two sentences. Do not call a tool."

REPEAT = ("[note] You already made this exact call and nothing has changed since. Do not repeat it. "
          "If the request is done, answer the user in one sentence; otherwise call a different tool.")

ASK_FILE = "[write result]\nSend the complete text of {path} now, inside one code block, with nothing before or after it."

NO_BLOCK = "error: no code block in your reply, so nothing was written. Call write again, then put the complete file in one code block."

CUT_OFF = ("error: your reply was cut off at the token limit, so nothing was written. "
           "Make the file shorter, or split it into several smaller files.")

CUT_OFF_CALL = "your reply was cut off at the token limit, so the tool call is incomplete. Send a shorter one."

EXTRA_NOTE = "[note] Your reply held more than one tool call. Only the first ran. Send the others one at a time.\n"

DENIED = "The user denied this action. Try a different approach, or ask the user in plain text."

HELP = """/help              show this help
/clear             forget the conversation
/model [name]      show or switch the model
/ctx N             set the model context window
/maxprompt N       set the prompt token cap per request
/maxtokens N       set the reply token cap
/tokens            show estimated prompt size against the limits
/temperature T     set the sampling temperature (0 to 2)
/maxsteps N        set the tool-call cap per user turn
/verbose           show or hide the raw tool-call blocks
/exit              quit"""

STY = {"bold": "\033[1m", "dim": "\033[2m", "code": "\033[36m", "head": "\033[1;34m", "quote": "\033[3m",
       "red": "\033[31m", "green": "\033[32m"}
RESET = "\033[0m"
COLOR = False  # set in main() when the terminal shows ANSI styles


def paint(text, style):
    """Wrap text in an ANSI style when colors are on."""
    return f"{STY[style]}{text}{RESET}" if COLOR else text


def dim(text):
    """Print a dim status line."""
    print(paint(text, "dim"), flush=True)


def show(text):
    """Escape control characters (keeping newlines and tabs) so a permission prompt shows what will really run."""
    return "".join(c if c in "\n\t" or c.isprintable() else repr(c)[1:-1] for c in str(text))


def cut(text, limit):
    """Keep the head and tail of text within about limit chars, marking the cut."""
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    return f"{text[:head]}\n[... {len(text) - limit} chars cut ...]\n{text[len(text) - (limit - head):]}"


def trim(msgs, budget, count):
    """Fit msgs under budget tokens: shorten old tool outputs, then drop oldest turns; return (msgs, note)."""
    msgs = [dict(m) for m in msgs]
    last_user = max(i for i, m in enumerate(msgs) if m["role"] == "user" and not m.get("tool"))
    shortened = dropped = 0
    for m in msgs[1:-1]:  # oldest first; the newest message stays whole
        if count(msgs) <= budget:
            break
        if m.get("tool") and len(m["content"]) > OLD_TOOL_KEEP + 50:
            m["content"] = cut(m["content"], OLD_TOOL_KEEP)
            shortened += 1
    while count(msgs) > budget and last_user > 1:  # drop whole turns before the latest user message
        end = next(i for i in range(2, last_user + 1) if msgs[i]["role"] == "user" and not msgs[i].get("tool"))
        del msgs[1:end]
        dropped += end - 1
        last_user -= end - 1
    while count(msgs) > budget and len(msgs) - last_user > 3:  # then old reply/result pairs of this turn
        del msgs[last_user + 1:last_user + 3]
        dropped += 2
    parts = [f"{shortened} old tool outputs shortened"] * bool(shortened) + [f"{dropped} old messages dropped"] * bool(dropped)
    return msgs, f"[trimmed: {', '.join(parts)}]" if parts else ""


TOOL_ARGS = {"read": ["path"], "write": ["path"], "edit": ["path", "old", "new"],  # write's content is optional
             "ls": [], "grep": ["pattern"], "shell": ["command"]}
BLOCK_RE = re.compile(r"```[ \t]*(\w*)[ \t]*\n?(.*?)(?:```|\Z)|<tool_call>(.*?)(?:</tool_call>|\Z)", re.S)
LOOKS_LIKE_CALL = re.compile(r"""\s*\{\s*["']?(tool|name)["']?\s*:""")
SHELL_ONLY_RE = re.compile(r"\s*```[ \t]*(?:shell|bash|sh|cmd|bat|powershell|console)[ \t]*\n(.*?)```\s*", re.S | re.I)
EXTRA_CALL_RE = re.compile(r"""\{\s*["']tool["']\s*:""")
BARE_RE = re.compile(r"""^[ \t]*\{\s*"tool"\s*:.*\}[ \t]*$""", re.M)
DECODER = json.JSONDecoder(strict=False)  # strict=False allows raw newlines inside strings


def parse_tool_call(text):
    """Return (name, args, end) for the first tool call in text, or None; raise ValueError if it is malformed."""
    for m in BLOCK_RE.finditer(text):
        tag = m.group(3) is not None
        lang, body = ("tool", m.group(3)) if tag else (m.group(1).lower(), m.group(2))
        if lang == "tool" or (lang in ("", "json") and LOOKS_LIKE_CALL.match(body)):
            start = text.find("{", m.start(3 if tag else 2), m.end())
            try:  # decode from the brace so a ``` inside a JSON string doesn't end the block
                obj, j = DECODER.raw_decode(text, start) if start != -1 else (None, -1)
            except ValueError:
                obj = None
            if not isinstance(obj, dict):
                return (*_parse_call(body), m.end())
            close = "</tool_call>" if tag else "```"
            k = text.find(close, j)
            end = k + len(close) if k != -1 and not text[j:k].strip() else j
            return (*_check_call(obj), end)
    m = SHELL_ONLY_RE.fullmatch(text)  # a reply that is only a shell block means "run this"
    if m and m.group(1).strip():
        return "shell", {"command": " && ".join(s.strip() for s in m.group(1).splitlines() if s.strip())}, len(text)
    m = BARE_RE.search(text)  # small models sometimes skip the fence
    return (*_parse_call(m.group()), m.end()) if m else None


def file_from_reply(reply):
    """Return the text of the first code block in reply, or None when there is no block or it is empty."""
    lines = reply.split("\n")
    start = next((i for i, s in enumerate(lines) if s.lstrip().startswith("```")), None)
    if start is None:
        return None
    # ponytail: the last bare fence closes the block, so a Markdown file holding code fences stays whole; prose
    # that follows the block with a fenced snippet of its own would be swallowed into the file.
    end = next((i for i in range(len(lines) - 1, start, -1) if lines[i].strip() == "```"), len(lines))
    body = "\n".join(lines[start + 1:end])
    return body + "\n" if body.strip() else None


def call_complete(text):
    """True once text holds a whole fenced or tagged tool call, so streaming can stop early."""
    try:
        call = parse_tool_call(text)
    except ValueError:  # usually a call that is still streaming
        return False
    # a reply that is only a shell block is a call only if nothing follows it, so keep reading those
    return bool(call) and text[:call[2]].endswith(("```", "</tool_call>")) and not SHELL_ONLY_RE.fullmatch(text)


def _parse_call(body):
    """Turn a tool block body into (name, args), accepting common small-model variants."""
    s = body[body.find("{"):body.rfind("}") + 1]
    if not s:
        raise ValueError("no JSON object in the tool block")
    try:
        obj = json.loads(s, strict=False)  # strict=False allows raw newlines inside strings
    except ValueError as e:
        try:
            obj = ast.literal_eval(s)  # single quotes, trailing commas
        except Exception:
            raise ValueError(f"bad JSON ({e})") from None
    return _check_call(obj)


def _check_call(obj):
    """Validate a decoded tool call object and return (name, args)."""
    if not isinstance(obj, dict):
        raise ValueError("the tool block must hold one JSON object")
    name = obj.get("tool") or obj.get("name")
    args = obj.get("args") or obj.get("arguments") or obj.get("parameters") or {k: v for k, v in obj.items() if k not in ("tool", "name")}
    if isinstance(args, str):
        try:
            args = json.loads(args, strict=False)
        except ValueError:
            raise ValueError("arguments must be a JSON object") from None
    if name not in TOOL_ARGS:
        raise ValueError(f"unknown tool {name!r}; use one of {', '.join(TOOL_ARGS)}")
    if not isinstance(args, dict):
        raise ValueError("arguments must be a JSON object")
    missing = [k for k in TOOL_ARGS[name] if k not in args]
    if missing:
        raise ValueError(f"{name} needs {', '.join(missing)}")
    return name, args


def call_summary(name, args):
    """One-line description of a tool call, shown instead of the raw block."""
    path, n = args.get("path") or ".", len(str(args.get("content", "")).splitlines())
    lines = f" lines {args.get('start') or 1}-{args.get('end') or 'end'}" if args.get("start") or args.get("end") else ""
    return {"read": path + lines, "write": path + (f" ({n} line{'s' * (n != 1)})" if "content" in args else ""),
            "edit": path, "ls": path,
            "grep": f'"{args.get("pattern")}" in {path}', "shell": str(args.get("command"))}[name]


MARK_CHARS = set(" \t#-*+>_.)0123456789")  # chars that may still turn into a block marker at line start
LINE_STYLES = [(re.compile(r"(\s*)#{1,6} "), "", "head"), (re.compile(r"(\s*)[-*+] "), "• ", ""),
               (re.compile(r"(\s*)> ?"), "│ ", "quote")]
HR_RE = re.compile(r"\s*([-*_])(?:\s*\1){2,}\s*")


class Markdown:
    """Stream markdown to the terminal with ANSI styles, hiding tool-call blocks unless verbose."""
    # ponytail: styles chars as they arrive (headings, lists, quotes, rules, **bold**, `code`, fenced blocks).
    # No tables, nesting or syntax highlighting; upgrade path is a real markdown renderer such as rich.

    def __init__(self, write, styled, verbose):
        self.write, self.styled, self.verbose = write, styled, verbose
        self.line = self.held = self.probe = self.base = ""
        self.block = None  # None, "code", "probe" (``` or ```json that may open a tool call) or "tool"
        self.open = self.raw = self.bold = self.code = self.marker = False
        self.at_start = True

    def _out(self, s):
        if s:
            self.write(s)
            self.marker, self.at_start = False, s.endswith("\n")

    def feed(self, text):
        """Render the next streamed piece of the reply."""
        for ch in text:
            if self.block == "tool":  # the rest of the reply is the call; anything after it is dropped anyway
                if self.verbose:
                    self._out(ch)
            elif self.open:
                self._end_line() if ch == "\n" else self._inline(ch)
            else:  # hold the line start until it can't become a block marker
                self.line += ch
                s = self.line.lstrip()
                if ch == "\n":
                    line, self.line = self.line[:-1], ""
                    self._whole_line(line)
                elif ch not in MARK_CHARS and not (self.block == "probe" or s.startswith("`")
                                                   or (self.block is None and s.startswith("<"))):
                    line, self.line = self.line, ""
                    self._start_line(line)

    def _start_line(self, text):
        """Write the line's block marker and style, then stream the rest of the line."""
        self.open = True
        if self.block == "code":
            self.raw, self.base = True, STY["code"] if self.styled else ""
            self._out(f"{STY['dim']}│ {RESET}{self.base}" if self.styled else "")
            return self._out(text)
        for rx, fancy, style in LINE_STYLES:
            if m := rx.match(text):
                self.base = STY.get(style, "") if self.styled else ""
                self._out((m.group(1) + fancy if self.styled else m.group()) + self.base)
                text = text[m.end():]
                break
        for ch in text:
            self._inline(ch)

    def _inline(self, ch):
        """Stream one char, turning ** into bold and backtick runs into inline code."""
        if self.raw:
            return self._out(ch)
        if self.held and ch != self.held[0]:
            self._flush_held()
        if ch == "`" or (ch == "*" and not self.code):
            self.held += ch
        else:
            self._out(ch)

    def _flush_held(self):
        run, self.held = self.held, ""
        if not self.styled or (run[0] == "*" and len(run) != 2):
            return self._out(run)
        if run[0] == "`":
            self.code = not self.code
        else:
            self.bold = not self.bold
        self._out(RESET + self.base + STY["bold"] * self.bold + STY["code"] * self.code)

    def _end_line(self):
        if self.held:
            self._flush_held()
        if self.styled and (self.base or self.bold or self.code):
            self._out(RESET)
        self._out("\n")
        self.open = self.raw = self.bold = self.code = False
        self.base = ""

    def _whole_line(self, line):
        """Render a line that had to be seen whole: fences, tool-call openers, rules and marker-only lines."""
        s = line.strip()
        if self.block == "probe":  # the line after ``` or ```json decides: tool call or code block
            fence, self.block = self.probe, None
            if LOOKS_LIKE_CALL.match(line):
                self._tool_start(fence)
                return self.feed(line + "\n")
            self.block = "code"
            self._fence(fence)
        if self.block == "code":
            if s.startswith("```"):
                self.block = None
                return self._fence(line)
            self._start_line(line)
            return self._end_line()
        if s.startswith(("```", "<tool_call>")):
            inner = s[3:-3] if len(s) > 6 and s.endswith("```") else None
            lang = s[3:].strip().lower()
            if s.startswith("<") or re.match(r"tool\b", lang) or (
                    inner is not None and LOOKS_LIKE_CALL.match(re.sub(r"^json\b", "", inner))):
                return self._tool_start(line)
            if inner is None and lang in ("", "json"):
                self.block, self.probe = "probe", line
                return
            if inner is None:
                self.block = "code"
                return self._fence(line)
        if HR_RE.fullmatch(line):  # a one-line ```text``` falls through to here and renders as inline code
            return self._out((paint("─" * 40, "dim") if self.styled else line) + "\n")
        self._start_line(line)
        self._end_line()

    def _fence(self, line):
        if not self.styled:  # styled output shows code blocks by their │ gutter instead
            self._out(line + "\n")

    def _tool_start(self, line):
        self.block = "tool"
        if self.verbose:
            self._out((STY["dim"] if self.styled else "") + line + "\n")
        elif self.styled:
            self._out(f"{STY['dim']}● tool call…{RESET}\n")
            self.marker = True

    def close(self):
        """Finish the reply: render any partial line and reset styles."""
        if self.open:
            self._end_line()
        elif self.line:
            line, self.line = self.line, ""
            self._whole_line(line)
        if self.block == "probe":
            self._fence(self.probe)
        if self.block == "tool" and self.verbose and self.styled:
            self.write(RESET)  # not _out: a reset doesn't move the cursor
        if not self.at_start:
            self._out("\n")
        self.block = None

    def status(self, line):
        """Print a line, replacing the "tool call…" marker if it is still the last thing on screen."""
        self.write(("\033[1A\033[2K" if self.marker else "") + line + "\n")
        self.marker, self.at_start = False, True


def resolve(p):
    """Resolve a tool path, refusing anything outside the working directory."""
    path = (ROOT / str(p)).resolve()
    if not path.is_relative_to(ROOT):
        raise ValueError(f"{p} is outside the working directory")
    return path


def tool_read(a):
    lines = resolve(a["path"]).read_text(encoding="utf-8", errors="replace").splitlines()
    start = max(1, int(a.get("start") or 1))
    end = min(len(lines), int(a.get("end") or start + 199))
    if start > len(lines):
        raise ValueError(f"start {start} is past the end; {a['path']} has {len(lines)} lines")
    eof = ", end of file" if end == len(lines) else ""
    return f"[{a['path']} lines {start}-{end} of {len(lines)}{eof}]\n" + "\n".join(lines[start - 1:end])


def tool_write(a):
    path = resolve(a["path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(a["content"]), encoding="utf-8", newline="")
    return f"wrote {len(str(a['content']).splitlines())} lines to {a['path']}"


def tool_edit(a):
    path = resolve(a["path"])
    with open(path, encoding="utf-8", newline="") as f:  # strict utf-8: never rewrite a file we can't decode
        raw = f.read()
    text, old, new = (str(s).replace("\r\n", "\n") for s in (raw, a["old"], a["new"]))
    n = text.count(old) if old else 0
    if n == 0:
        raise ValueError(f"old text not found in {a['path']}; read the file and copy the exact text")
    if n > 1:
        raise ValueError(f"old text matches {n} times in {a['path']}; include more surrounding lines so it is unique")
    text = text.replace(old, new)
    path.write_text(text.replace("\n", "\r\n") if "\r\n" in raw else text, encoding="utf-8", newline="")
    return f"edited {a['path']}"


def tool_ls(a):
    entries = sorted(resolve(a.get("path") or ".").iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
    return "\n".join(e.name + "/" * e.is_dir() for e in entries) or "(empty)"


def tool_grep(a):
    try:
        rx = re.compile(a["pattern"])
    except re.error:
        rx = re.compile(re.escape(a["pattern"]))  # small models often send unescaped literals
    base, out = resolve(a.get("path") or "."), []
    walk = [(base.parent, [], [base.name])] if base.is_file() else os.walk(base)
    for d, dirs, names in walk:
        dirs[:] = sorted(x for x in dirs if x not in SKIP_DIRS)
        for name in sorted(names):
            f = Path(d, name)
            if not f.resolve().is_relative_to(ROOT):
                continue  # a symlink pointing outside the working directory
            try:
                if f.stat().st_size > 1_000_000:
                    continue
                lines = f.read_text(encoding="utf-8").splitlines()
            except (UnicodeDecodeError, OSError):
                continue  # binary or unreadable
            out += [f"{f.relative_to(ROOT).as_posix()}:{i}: {s.strip()[:200]}" for i, s in enumerate(lines, 1) if rx.search(s)]
            if len(out) >= 200:
                return "\n".join(out[:200]) + "\n[stopped at 200 matches]"
    return "\n".join(out) or "no matches"


def decode_output(b):
    """Decode shell output as UTF-8 when valid, else as the Windows console code page that cmd.exe built-ins use."""
    # ponytail: one guess per command, so mixed UTF-8 and code-page output gets the code page. Upgrade: per-line decode.
    try:
        return b.decode("utf-8").replace("\r\n", "\n")
    except UnicodeDecodeError:
        cp = ctypes.windll.kernel32.GetConsoleOutputCP() if os.name == "nt" else 0
        return b.decode(f"cp{cp}" if cp else ("oem" if os.name == "nt" else "latin-1"), "replace").replace("\r\n", "\n")


def tool_shell(a):
    p = subprocess.Popen(str(a["command"]), shell=True, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, start_new_session=os.name != "nt")
    try:
        out, _ = p.communicate(timeout=SHELL_TIMEOUT)
    except subprocess.TimeoutExpired:
        if os.name == "nt":  # kill the whole tree, or a grandchild keeps the pipe open
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
        else:
            os.killpg(p.pid, signal.SIGKILL)
        out, _ = p.communicate()
        return f"{decode_output(out)}\n[killed after {SHELL_TIMEOUT}s timeout; interactive commands are not supported]"
    return f"{decode_output(out)}\n[exit code {p.returncode}]"


TOOLS = {"read": tool_read, "write": tool_write, "edit": tool_edit, "ls": tool_ls, "grep": tool_grep, "shell": tool_shell}
ASK = {"write", "edit", "shell"}


def http(url, body=None, timeout=10):
    """Open a GET (or JSON POST when body is given) request."""
    data = json.dumps(body).encode() if body is not None else None
    return urllib.request.urlopen(urllib.request.Request(url, data, {"Content-Type": "application/json"}), timeout=timeout)


class Agent:
    def __init__(self, url, model, ctx, max_prompt, max_tokens, temperature, max_steps, verbose=False):
        self.url, self.verbose = url.rstrip("/"), verbose
        self.base = re.sub(r"/v1$", "", self.url)
        self.native = self.is_ollama()
        self.ratio, self.last_used, self.last_note, self.allowed = 3.0, None, "", set()
        self.truncated = False
        self.set_limits(ctx, max_tokens, max_prompt)
        self.set_sampling(temperature, max_steps)
        self.model = model or self.pick_model()
        self.history = [{"role": "system", "content": SYSTEM.format(cwd=ROOT, os=sys.platform, shell=SHELL_NAME)}]

    def is_ollama(self):
        """Ollama answers GET /api/version; other OpenAI-compatible servers don't."""
        try:
            with http(self.base + "/api/version", timeout=3) as r:
                return "version" in json.load(r)
        except (OSError, ValueError):
            return False

    def models(self):
        with http(self.url + "/models") as r:
            return [m["id"] for m in json.load(r).get("data", [])]

    def pick_model(self):
        try:
            names = self.models()
        except (OSError, ValueError) as e:
            sys.exit(f"cannot list models at {self.url}/models ({e}); is the server running? or pass --model")
        if not names:
            sys.exit(f"no models available at {self.url}; pull or load one first")
        dim(f"[no model given; using {names[0]}]")
        return names[0]

    def set_limits(self, ctx, max_tokens, max_prompt):
        """Validate and apply token limits; max_prompt None means ctx minus max_tokens."""
        if min(ctx, max_tokens) < 1 or max_tokens >= ctx or (max_prompt is not None and max_prompt < 1):
            raise ValueError("limits must be positive and max-tokens must be smaller than ctx")
        room = ctx - max_tokens
        if max_prompt is not None and max_prompt > room:
            dim(f"[warning: max-prompt {max_prompt} + max-tokens {max_tokens} exceeds ctx {ctx}; max-prompt clamped to {room}]")
            max_prompt = room
        self.ctx, self.max_tokens, self.max_prompt_set = ctx, max_tokens, max_prompt
        self.max_prompt = max_prompt or room

    def set_sampling(self, temperature, max_steps):
        """Validate and apply the temperature and the tool-call cap per user turn."""
        if not 0 <= temperature <= 2 or max_steps < 1:  # 0-2 is the OpenAI range; `not` also rejects NaN
            raise ValueError("temperature must be 0 to 2 and max-steps at least 1")
        self.temperature, self.max_steps = temperature, max_steps

    def count(self, msgs):
        """Estimate prompt tokens for msgs."""
        # ponytail: chars/ratio estimate (+4 per message for the chat template), corrected after each reply from the
        # server's prompt_tokens; it can be off for unusual text. Upgrade: a real tokenizer or llama.cpp's /tokenize.
        return int(sum(len(m["content"]) for m in msgs) / self.ratio) + 4 * len(msgs)

    def stream(self, msgs, stop_at_call=True):
        """Send msgs, print the reply as it streams, and return (text, server prompt tokens or None)."""
        if self.native:  # Ollama's /v1 ignores num_ctx, so use the native API there
            # think=False: thinking models (qwen3.5, ...) would otherwise spend max-tokens on hidden reasoning localcode never shows
            url, body = self.base + "/api/chat", {"model": self.model, "messages": msgs, "stream": True, "think": False,
                                                  "options": {"num_ctx": self.ctx, "num_predict": self.max_tokens,
                                                              "temperature": self.temperature}}
        else:
            url, body = self.url + "/chat/completions", {
                "model": self.model, "messages": msgs, "stream": True, "max_tokens": self.max_tokens,
                "temperature": self.temperature, "stream_options": {"include_usage": True}}
        text, used = [], None
        md = self.md = Markdown(lambda s: print(s, end="", flush=True), COLOR, self.verbose)
        try:
            with http(url, body, timeout=600) as r:
                for raw in r:
                    line = raw.decode("utf-8", "replace").strip()
                    if not self.native:
                        if not line.startswith("data:"):
                            continue
                        line = line[5:].strip()
                        if line == "[DONE]":
                            break
                    if not line:
                        continue
                    ev = json.loads(line)
                    if ev.get("error"):
                        raise RuntimeError(str(ev["error"]))
                    if self.native:
                        piece = (ev.get("message") or {}).get("content") or ""
                        used = ev.get("prompt_eval_count", used)
                        self.truncated |= ev.get("done_reason") == "length"
                    else:
                        piece = "".join((c.get("delta") or {}).get("content") or "" for c in ev.get("choices") or [])
                        used = (ev.get("usage") or {}).get("prompt_tokens", used)
                        self.truncated |= any(c.get("finish_reason") == "length" for c in ev.get("choices") or [])
                    if piece:
                        md.feed(piece)
                        text.append(piece)
                        # closing the connection stops generation; cost: no server prompt count for this reply
                        if stop_at_call and ("`" in piece or ">" in piece) and call_complete("".join(text)):
                            break
        finally:  # also on Ctrl+C, so styles never leak into the prompt
            md.close()
        return "".join(text), used

    def ask_model(self, stop_at_call=True):
        """Send a copy of the history trimmed to the prompt budget, stream one reply and correct the token ratio."""
        msgs, note = trim(self.history, self.max_prompt, self.count)  # full history stays, so /maxprompt can grow back
        if note and note != self.last_note:  # print once, not on every tool step
            dim(note)
        self.last_note = note
        if (est := self.count(msgs)) > self.max_prompt:
            dim(f"[warning: prompt ~{est} tokens is still over max-prompt {self.max_prompt}]")
        msgs = [{"role": m["role"], "content": m["content"]} for m in msgs]
        self.truncated = False  # stream() sets it when the server says the reply hit max-tokens
        text, used = self.stream(msgs, stop_at_call)
        if self.truncated:
            dim(f"[the reply was cut off at max-tokens {self.max_tokens}; raise it with /maxtokens N (it must stay below ctx {self.ctx})]")
        if used and used > 4 * len(msgs):  # 0.9 keeps the estimate on the high side
            chars = sum(len(m["content"]) for m in msgs)
            self.ratio = min(6.0, max(1.5, 0.9 * chars / (used - 4 * len(msgs))))
            self.last_used = used
        return text

    def fetch_file(self, path):
        """After a write call without content, ask the model for the file text; return (text, None) or (None, error)."""
        self.history.append({"role": "user", "content": ASK_FILE.format(path=path), "tool": True})
        reply = self.ask_model(stop_at_call=False)
        self.history.append({"role": "assistant", "content": reply})
        if self.truncated:  # a cut-off file would be written half finished
            return None, CUT_OFF
        text = file_from_reply(reply)
        return (text, None) if text else (None, NO_BLOCK)

    def permitted(self, name, args, streamed=False):
        """Ask y/n/a before a tool that changes things; 'a' allows that tool for the session."""
        if name not in ASK or name in self.allowed:
            return True
        what = name
        if name == "edit":  # shell is fully described by the summary line above
            print("\n".join([paint(f"    - {show(s)}", "red") for s in str(args["old"]).splitlines()]
                            + [paint(f"    + {show(s)}", "green") for s in str(args["new"]).splitlines()]), flush=True)
        elif name == "write":
            body = str(args["content"]).splitlines()
            what = f"write {show(args['path'])} ({len(body)} line{'s' * (len(body) != 1)})"
            if not streamed:  # content that was streamed to the screen has just been seen; content inside the call was hidden
                print("\n".join([paint(f"    {show(s)[:120]}", "dim") for s in body[:5]]
                                + [paint(f"    ... ({len(body) - 5} more lines)", "dim")] * (len(body) > 5)), flush=True)
        try:
            ans = input(paint(f"  allow {what}? [y/n/a] ", "bold")).strip().lower()
        except EOFError:
            ans = "n"
        if ans == "a":
            self.allowed.add(name)
        return ans in ("y", "a")

    def turn(self, user_text):
        """Run one user turn: model reply, tool call, result back, until a reply has no tool call."""
        if self.history[-1]["role"] == "user":  # left by a denial, interrupt or server error; strict templates need alternation
            user_text = self.history.pop()["content"] + "\n\n" + user_text
        self.history.append({"role": "user", "content": user_text})
        corrected, denied = False, set()  # one correction per user turn; calls the user said no to
        used, seen, repeats, cuts = [], set(), 0, 0  # tools run this turn; calls made since the last change; repeats; cut-off files
        for step in range(self.max_steps + 1):
            reply = self.ask_model()
            try:
                call = parse_tool_call(reply)
            except ValueError as e:
                e = CUT_OFF_CALL if self.truncated else e  # a cut-off call looks like bad JSON; say what really happened
                self.history.append({"role": "assistant", "content": reply})
                if corrected:
                    self.md.status(paint(f"[model sent a malformed tool call again ({e}); stopping this turn]", "dim"))
                    return
                self.md.status(paint(f"[malformed tool call: {e}; asking the model to retry]", "dim"))
                self.history.append({"role": "user", "content": CORRECTION.format(err=e), "tool": True})
                corrected = True
                continue
            if call is None:
                self.history.append({"role": "assistant", "content": reply})
                return
            name, args, end = call
            self.history.append({"role": "assistant", "content": reply[:end]})  # drop anything invented after the call
            self.md.status(f"{paint('●', 'green')} {paint(name, 'bold')} {show(call_summary(name, args))}")
            if step == self.max_steps:
                break
            result, streamed = None, False
            if name == "write" and "content" not in args:  # two-step write: the file text comes in a code block
                text, result = self.fetch_file(args["path"])
                if text is not None:
                    args, streamed = {**args, "content": text}, True
                elif result == CUT_OFF:
                    cuts += 1
                    if cuts > 1:  # each attempt can cost minutes on a slow machine, and the model rarely shortens it
                        dim("[the file was cut off twice; raise /maxtokens or ask for a smaller file. Asking for a final answer]")
                        self.history.append({"role": "user", "content": f"[write result]\n{CUT_OFF}", "tool": True})
                        self.finish()
                        return
            key = json.dumps([name, args], sort_keys=True, default=str)
            if result is None and key in seen:  # small models redo the same call until the step cap; make them answer instead
                repeats += 1
                if repeats > 1:
                    dim(f"[model kept repeating the same {name} call; asking it for a final answer]")
                    self.finish()
                    return
                result = REPEAT
            if result is None:
                if key in denied:  # small models tend to repeat a denied call; don't ask again
                    dim(f"[model repeated a denied {name} call; stopping this turn]")
                    return
                if not self.permitted(name, args, streamed):
                    denied.add(key)
                    self.history.append({"role": "user", "content": f"[{name} result]\n{DENIED}", "tool": True})
                    dim(f"[{name} denied; the model will try something else]")
                    continue
                try:
                    result = TOOLS[name](args)
                except Exception as e:  # tool errors go back to the model so it can fix its call
                    result = f"error: {e}"
                if name in ASK:
                    seen.clear()  # files may have changed, so reading or listing again is legitimate
                seen.add(key)
            used.append(name)
            lines = result.splitlines() or ["(no output)"]
            shown = lines[:8] + [f"... ({len(lines) - 8} more lines)"] * (len(lines) > 8)
            dim("\n".join(("  └ " if i == 0 else "    ") + s for i, s in enumerate(shown)))
            cap = max(400, int(self.max_prompt * self.ratio / 4))  # one result may use about 1/4 of the prompt budget
            note = EXTRA_NOTE * bool(EXTRA_CALL_RE.search(reply[end:]))
            task = " ".join(user_text.split())[:300]
            self.history.append({"role": "user", "content": f"[{name} result]\n{cut(result, cap)}\n{note}{NUDGE.format(task=task, used=', '.join(used))}",
                                 "tool": True})
        dim(f"[stopped: {self.max_steps} tool steps in one turn; asking the model for a final answer]")
        self.finish()

    def finish(self):
        """End a turn that was going nowhere by asking the model once for a plain-text answer."""
        if self.history[-1]["role"] == "user":  # keep the roles alternating
            self.history[-1]["content"] += "\n" + FINAL
        else:
            self.history.append({"role": "user", "content": FINAL, "tool": True})
        reply = self.ask_model()
        self.history.append({"role": "assistant", "content": reply})
        try:
            wants_tool = parse_tool_call(reply) is not None
        except ValueError:
            wants_tool = True
        if wants_tool:  # ignored on purpose; the turn is over
            self.md.status(paint("[the model asked for another tool; ending the turn]", "dim"))

    def tokens(self):
        sent = self.count(trim(self.history, self.max_prompt, self.count)[0])
        print(f"history ~{self.count(self.history)} tokens, sent after trimming ~{sent} / max-prompt {self.max_prompt}\n"
              f"prompt + max-tokens ~{sent + self.max_tokens} / ctx {self.ctx} (max-tokens {self.max_tokens})\n"
              f"estimate: {self.ratio:.2f} chars/token, last server prompt count: {self.last_used or 'none yet'}\n"
              + ("backend: Ollama native /api/chat, num_ctx sent with every request" if self.native else
                 "backend: OpenAI-compatible; ctx only sets localcode's budget (set the server's context at its start)"))

    def command(self, line):
        """Handle a slash command; return False to quit."""
        cmd, _, arg = line.strip().partition(" ")
        arg = arg.strip()
        if cmd == "/exit":
            return False
        if cmd == "/help":
            print(HELP)
        elif cmd == "/clear":
            self.history = self.history[:1]
            dim("[conversation cleared]")
        elif cmd == "/model":
            if arg:
                self.model = arg
            else:
                try:
                    dim("[available: " + ", ".join(self.models()) + "]")
                except (OSError, ValueError) as e:
                    dim(f"[cannot list models: {e}]")
            dim(f"[model: {self.model}]")
        elif cmd in ("/ctx", "/maxprompt", "/maxtokens"):
            if arg:
                try:
                    n = int(arg)
                    self.set_limits(*{"/ctx": (n, self.max_tokens, self.max_prompt_set),
                                      "/maxtokens": (self.ctx, n, self.max_prompt_set),
                                      "/maxprompt": (self.ctx, self.max_tokens, n)}[cmd])
                except ValueError as e:
                    dim(f"[error: {e}]")
            dim(f"[ctx {self.ctx}, max-prompt {self.max_prompt}, max-tokens {self.max_tokens}]")
        elif cmd in ("/temperature", "/maxsteps"):
            if arg:
                try:
                    self.set_sampling(float(arg), self.max_steps) if cmd == "/temperature" else \
                        self.set_sampling(self.temperature, int(arg))
                except ValueError as e:
                    dim(f"[error: {e}]")
            dim(f"[temperature {self.temperature}, max-steps {self.max_steps}]")
        elif cmd == "/verbose":
            self.verbose = not self.verbose
            dim(f"[verbose {'on: raw tool-call blocks are shown' if self.verbose else 'off: tool calls show as one line'}]")
        elif cmd == "/tokens":
            self.tokens()
        else:
            dim(f"[unknown command {cmd}; try /help]")
        return True


def enable_color():
    """Return True if the terminal can show ANSI colors, enabling them on Windows consoles."""
    if not sys.stdout.isatty():
        return False
    if os.name != "nt":
        return True
    k = ctypes.windll.kernel32
    h, mode = k.GetStdHandle(-11), ctypes.c_uint32()
    return bool(k.GetConsoleMode(h, ctypes.byref(mode)) and k.SetConsoleMode(h, mode.value | 4))


def main():
    global COLOR
    for s in (sys.stdin, sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Terminal coding agent for small local models (OpenAI-compatible or Ollama).")
    # 127.0.0.1, not localhost: on Windows localhost tries IPv6 first and Ollama listens on IPv4 only (~2.3 s per request)
    ap.add_argument("--url", default=os.environ.get("LOCALCODE_URL", "http://127.0.0.1:11434/v1"),
                    help="OpenAI-compatible base URL (env LOCALCODE_URL, default %(default)s)")
    ap.add_argument("--model", default=os.environ.get("LOCALCODE_MODEL"),
                    help="model name (env LOCALCODE_MODEL; default: first from /v1/models)")
    ap.add_argument("--ctx", type=int, default=8192, help="model context window in tokens (default %(default)s)")
    ap.add_argument("--max-prompt", type=int, help="prompt token cap per request (default ctx minus max-tokens)")
    ap.add_argument("--max-tokens", type=int, default=2048, help="reply token cap (default %(default)s)")
    ap.add_argument("--temperature", type=float, default=0.2, help="sampling temperature, 0 to 2 (default %(default)s)")
    ap.add_argument("--max-steps", type=int, default=10, help="tool-call cap per user turn (default %(default)s)")
    ap.add_argument("--verbose", action="store_true", help="show raw tool-call blocks (toggle with /verbose)")
    ap.add_argument("--version", action="version", version=f"localcode {__version__}")
    args = ap.parse_args()
    COLOR = enable_color()
    try:
        agent = Agent(args.url, args.model, args.ctx, args.max_prompt, args.max_tokens, args.temperature,
                      args.max_steps, args.verbose)
    except ValueError as e:
        sys.exit(f"error: {e}")
    dim(f"localcode | {agent.model} @ {agent.url} | {'ollama native' if agent.native else 'openai-compatible'} | "
        f"ctx {agent.ctx}, max-prompt {agent.max_prompt}, max-tokens {agent.max_tokens} | "
        f"temperature {agent.temperature}, max-steps {agent.max_steps} | /help")
    while True:
        try:
            line = input("> ")
        except (EOFError, KeyboardInterrupt):
            break
        if not line.strip():
            continue
        if line.startswith("/"):
            if not agent.command(line):
                break
            continue
        try:
            agent.turn(line)
        except KeyboardInterrupt:
            dim("[interrupted]")
        except urllib.error.HTTPError as e:
            dim(f"[server error {e.code}: {e.read().decode('utf-8', 'replace')[:300]}]")
        except (OSError, RuntimeError, ValueError) as e:
            dim(f"[request failed: {e}]")


if __name__ == "__main__":
    main()
