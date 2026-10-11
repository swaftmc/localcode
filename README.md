# localcode

A terminal coding agent for small, locally hosted models (1.5B to 8B). One Python file, standard library only.

## Install

Needs Python 3.10+ and a running local server. No pip packages.

```
git clone <this repo> localcode
cd your-project
python path/to/localcode/localcode.py
```

localcode works in the directory you start it from. File tools can't reach outside that directory.

## Flags

| Flag | Default | Meaning |
|---|---|---|
| `--url URL` | `$LOCALCODE_URL` or `http://localhost:11434/v1` | OpenAI-compatible base URL |
| `--model NAME` | `$LOCALCODE_MODEL` or the first entry of `GET /v1/models` | model to use |
| `--ctx N` | 8192 | model context window in tokens |
| `--max-prompt N` | ctx minus max-tokens | cap on prompt tokens per request |
| `--max-tokens N` | 1024 | cap on reply length |

If max-prompt plus max-tokens is more than ctx, localcode warns and lowers max-prompt.

## Slash commands

| Command | Effect |
|---|---|
| `/help` | list commands |
| `/clear` | forget the conversation |
| `/model [name]` | show available models, or switch |
| `/ctx N`, `/maxprompt N`, `/maxtokens N` | change the token limits mid-session |
| `/tokens` | estimated prompt size against each limit |
| `/exit` | quit (Ctrl+C at the prompt also quits; during a reply it cancels the turn) |

## Tools

The model calls tools by writing a fenced block:

````
```tool
{"tool": "read", "path": "main.py"}
```
````

Tools: `read` (200 lines by default, `start`/`end` for ranges), `write`, `edit` (exact text, must match once), `ls`, `grep`, `shell` (cmd.exe on Windows, /bin/sh elsewhere, killed after 60 s, no stdin).
`write`, `edit` and `shell` ask `allow? [y/n/a]` first. `a` allows that tool for the rest of the session. Answering `n` ends the turn so you can say what to do instead.

## Context handling

Before each request localcode fits the history under max-prompt. It shortens old tool outputs first, then drops the oldest turns. The system prompt and your latest message always stay. A dim `[trimmed: ...]` line shows when this happens. Dropped messages are gone for the rest of the session.

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

Asserts the trimming and tool-call parsing logic; exits non-zero on failure.
