# localcode

A terminal coding agent for small, locally hosted models (1.5B to 8B). One Python file, standard library only.

## Install

localcode talks to a model server you already run locally, such as Ollama, llama.cpp, LM Studio or KoboldCpp. Download the build for your system from the [latest release](https://github.com/swaftmc/localcode/releases/latest). Each one bundles its own Python 3.11, so nothing else needs to be installed.

| System | Download | Setup |
|---|---|---|
| Windows 64-bit | `localcode-<version>-windows-x64-setup.exe` | Run it. It installs per user and adds localcode to your PATH. |
| Windows 64-bit | `localcode-<version>-windows-x64.zip` | Unzip anywhere and add the `localcode` folder to your PATH, or run `localcode\localcode.exe` directly. |
| Linux x86_64 | `localcode-<version>-linux-x64.tar.gz` | `tar -xzf` it, then run `localcode/localcode`. To use it from anywhere: `ln -s "$PWD/localcode/localcode" ~/.local/bin/localcode`. |
| Linux ARM64 | `localcode-<version>-linux-arm64.tar.gz` | The same as x86_64. |

**After the Windows installer, open a new terminal.** Windows gives a PATH change only to programs started afterwards, so a terminal that was already open keeps the old PATH and says `localcode` "is not recognized as the name of a cmdlet". A terminal inside an editor such as VS Code or Kiro needs the editor restarted. To use it without reopening anything, paste this into PowerShell:

```
$env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')
```

The Linux builds need glibc 2.17 or newer (most distributions from 2014 on). Alpine and other musl-based systems aren't supported. The Windows files aren't code-signed, so SmartScreen may ask you to click "More info" and then "Run anyway". Check a download against `SHA256SUMS.txt` from the release if you like.

Then, from the folder you want to work in:

```
localcode --model qwen2.5-coder:1.5b --ctx 4096 --max-prompt 3000
```

localcode works in the directory you start it from. File tools can't reach outside that directory.

**From source:** with Python 3.10 or newer and no other packages, `python localcode.py` takes the same arguments.

## License

Free for noncommercial use under the [PolyForm Noncommercial License 1.0.0](LICENSE). Commercial use needs a separate license from the author: open an issue on this repository or contact [@swaftmc](https://github.com/swaftmc) first. The bundled CPython runtime keeps its own license, in the `runtime` folder.

## Flags

| Flag | Default | Meaning |
|---|---|---|
| `--url URL` | `$LOCALCODE_URL` or `http://127.0.0.1:11434/v1` | OpenAI-compatible base URL |
| `--model NAME` | `$LOCALCODE_MODEL` or the first entry of `GET /v1/models` | model to use |
| `--ctx N` | 8192 | model context window in tokens |
| `--max-prompt N` | ctx minus max-tokens | cap on prompt tokens per request |
| `--max-tokens N` | 2048 | cap on reply length. A file longer than this is cut off and not written, so raise it for big files |
| `--temperature T` | 0.2 | sampling temperature, 0 to 2 |
| `--max-steps N` | 10 | cap on tool calls per user turn |
| `--verbose` | off | show the raw tool-call blocks |

If max-prompt plus max-tokens is more than ctx, localcode warns and lowers max-prompt.

The default URL uses `127.0.0.1` rather than `localhost`. On Windows, `localhost` tries IPv6 first, and Ollama listens on IPv4 only, which adds about 2 s to every request.

## Slash commands

| Command | Effect |
|---|---|
| `/help` | list commands |
| `/clear` | forget the conversation |
| `/model [name]` | show available models, or switch |
| `/ctx N`, `/maxprompt N`, `/maxtokens N` | change the token limits mid-session |
| `/tokens` | estimated size of the full history and of the trimmed prompt actually sent, against each limit |
| `/temperature T`, `/maxsteps N` | change the temperature or the tool-call cap mid-session |
| `/verbose` | show or hide the raw tool-call blocks |
| `/exit` | quit (Ctrl+C at the prompt also quits; during a reply it cancels the turn) |

## Display

Replies are rendered as Markdown while they stream. Supported: headings, `**bold**`, `` `code` ``, bullet lists, block quotes, horizontal rules, and fenced code blocks, which get a `│` left edge. Tables, nested formatting and syntax highlighting aren't supported.

A tool call shows as one line, such as `● write hello.py (1 line)`, with its result indented under it. `/verbose` shows the raw block instead. Colors only appear in a real terminal. Piped output is the reply as the model wrote it, with tool blocks hidden unless verbose is on.

## Which model

localcode works with any model your server runs, and the model matters more than any setting. In testing on a CPU-only machine:

- **qwen2.5-coder:1.5b** (about 1 GB) is fast and handles one-step requests: listing files, reading one, writing a small page. It loses track on multi-step requests and cannot write a full app. localcode ends its loops with a message, but it cannot make the model smarter.
- **qwen3.5:4b** (about 3 GB) follows multi-step requests, such as reading files and then writing a summary of them, and writes noticeably better code, but runs about three times slower on a CPU. localcode asks Ollama not to use the model's hidden "thinking" mode, which would spend your `--max-tokens` on text localcode never shows.

If a request fails or loops, try a bigger model before changing settings.

## Tools

The model calls tools by writing a fenced block:

````
```tool
{"tool": "read", "path": "main.py"}
```
````

Tools: `read` (200 lines by default, `start`/`end` for ranges), `write`, `edit` (exact text, must match once), `ls`, `grep`, `shell` (cmd.exe on Windows, /bin/sh elsewhere, killed after 60 s, no stdin).

`write` takes two steps because small models fail at packing a whole file into one JSON string. The model sends only the path, then localcode asks for the complete file text as an ordinary code block and writes that. You see the code stream in, and the permission question names the file and its line count. A `content` field inside the call still works. If the reply hits `--max-tokens` before the file ends, nothing is written and you are told to raise the limit. After each tool result the model is reminded of your request and asked to continue until every part is done.
`write`, `edit` and `shell` ask `allow? [y/n/a]` first. `a` allows that tool for the rest of the session. Answering `n` tells the model you said no, and it can try something else. If it sends the exact same call again, the turn ends without asking you again.

Reading a reply stops as soon as a tool call's closing fence arrives. That saves the time a small model would spend writing text after the call. Those replies come back without a server token count, so the token estimate is corrected only from plain-text replies.

## Context handling

Before each request localcode fits a copy of the history under max-prompt. It shortens old tool outputs first, then drops the oldest turns. The system prompt and your latest message always stay. A dim `[trimmed: ...]` line shows when the trimming result changes. The full history is kept, so raising `/maxprompt` later sends the older turns again.

Token counts are estimated from character count, then corrected after each reply using the count the server reports.

## Ollama

```
ollama pull qwen2.5-coder:1.5b
python localcode.py --model qwen2.5-coder:1.5b --ctx 8192
```

When the server is Ollama (detected via `GET /api/version`), localcode uses Ollama's native `/api/chat` and sends `--ctx` as `options.num_ctx`. Ollama's `/v1` endpoint ignores `num_ctx`. `/ctx 4096` reloads the model with a 4096 context on the next request; `ollama ps` shows it in the CONTEXT column.

## llama.cpp server

```
llama-server -m qwen2.5-coder-1.5b-instruct-q4_k_m.gguf -c 8192 --port 8080
python localcode.py --url http://localhost:8080/v1 --ctx 8192
```

llama.cpp fixes the context size when the server starts (`-c`). Here `--ctx` and `/ctx` only change localcode's own budget, so set them to the `-c` value or lower. LM Studio and KoboldCpp work the same way through their OpenAI-compatible URL.

## Check

```
python check_localcode.py
```

Asserts the trimming, tool-call parsing, two-step write, early stop, denial handling, path confinement and edit logic; exits non-zero on failure.
