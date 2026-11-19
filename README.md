# openchat

A small terminal coding assistant. It talks to a model, runs real tools in your
project directory, keeps sessions on disk, and renders answers as markdown.

<p align="center">
  <img src="assets/logo.svg" alt="openchat" width="520">
</p>

## Install

```sh
curl -fsSL https://github.com/wigmastrrrrrrrrjr/openchat/releases/latest/download/install.sh | bash
```

The installer downloads the latest release, installs anything missing from
`libraries.txt`, links the `openchat` command, and starts it.

Requirements: `python3` (3.10+), `curl` or `wget`, `unzip` (or the Python
`zipfile` fallback). Tested on Linux, macOS, and Termux/proot (Android);
Windows works through WSL.

The command goes to `/usr/local/bin` when writable, otherwise `~/.local/bin`
(added to your shell profile automatically).

### First run

openchat needs a way to reach a model, so the first launch asks once:

1. **hosted worker** — paste a token from the maintainer, or
2. **my own endpoint** — any OpenAI-compatible base URL and API key.

Your answer is saved to `~/.openchat/config.json` (mode 600). Running
non-interactively? Set `OPENCHAT_TOKEN=...` for the hosted worker, or
`OPENCHAT_BASE=...` and `OPENCHAT_API_KEY=...` for your own endpoint.

Already installed? Update in place:

```sh
openchat --update      # or type /update inside a session
```

## Use it

```sh
openchat                          # chat in the current directory
openchat "fix the failing tests"  # one-shot request, then exit
openchat -c                       # resume the most recent session
openchat --new                    # start with a fresh context
openchat -m deepseek-v4.1-flash   # pick a model
openchat --cwd ~/code/app         # work in another directory
openchat serve --port 8787        # headless HTTP server
```

Sessions live in `~/.openchat/sessions/` and reopen automatically when you start
`openchat` in the same directory. Scratch files go to `~/.openchat/tmp/<session>`
and project folders to `~/.openchat/projects/<session>`; scratch older than 30
days is cleaned up on launch.

### In-session commands

| Command | What it does |
| --- | --- |
| `/new` | Start a fresh session |
| `/sessions` | List recent sessions |
| `/resume <id>` | Switch to a saved session |
| `/model [id]` | Show or change the model |
| `/cwd` | Show the working directory |
| `/paths` | Show cwd, project, and scratch folders |
| `/think` | Toggle reasoning output |
| `/update` | Reinstall the latest release |
| `/exit` | Quit |

Press **Esc twice** to interrupt a streaming response or a running shell
command. The interrupted turn is kept in the session history.

## Tools

The model can call these, and every call is shown in the transcript:

| Tool | Purpose |
| --- | --- |
| `read` | Read a text file |
| `write` | Create or overwrite a file |
| `edit` | Replace an exact string in a file |
| `bash` | Run a shell command (180s timeout) |
| `ls` | List a directory |
| `glob` | Find files by pattern |
| `grep` | Search file contents |

Edits are shown as red/green diffs, and bash commands print a compact result
summary. Nothing runs outside the working directory unless you ask for it.

## Configuration

Config is optional; defaults live in the code. To override, create
`~/.openchat/config.json`:

```json
{
  "model": "glm-5.3-flash",
  "models": ["glm-5.3-flash", "deepseek-v4.1-flash"],
  "retries": 3,
  "thinking": true,
  "clear": true
}
```

Environment overrides:

| Variable | Effect |
| --- | --- |
| `OPENCHAT_MODEL` | Default model |
| `OPENCHAT_BASE` / `OPENCHAT_API_KEY` | Use your own OpenAI-compatible endpoint |
| `OPENCHAT_TOKEN` | Token for the hosted worker |
| `OPENCHAT_TMP` / `OPENCHAT_PROJECTS` | Move scratch/project folders |
| `OPENCHAT_APP_DIR` | Install location |
| `OPENCHAT_PYTHON` | Interpreter used by the launcher |

By default openchat sends requests to a hosted worker that holds the provider
key as a Cloudflare secret; no API keys ship in this repository. To run
completely against your own endpoint, set `base` and `api_key` (or the
`OPENCHAT_BASE` / `OPENCHAT_API_KEY` variables) and use `--provider novita`.

## Serve mode

`openchat serve` exposes the same agent over HTTP:

| Endpoint | Description |
| --- | --- |
| `POST /v1/chat/completions` | OpenAI-compatible, streaming and non-streaming |
| `POST /api/chat` | JSON request with `session_id` memory |
| `GET /api/sessions` | List server sessions |
| `GET /v1/models` | Available models |
| `GET /health` | Health check |

```sh
openchat serve --host 127.0.0.1 --port 8787 --token my-secret
```

When binding to a non-loopback address without a token, one is generated and
printed. Sessions persist in `~/.openchat/serve-sessions.json`.

## Layout

```
main.py              entrypoint
openchat/cli.py      arguments, REPL, turn loop
openchat/agent.py    providers and the tool loop
openchat/tools.py    tool schemas and execution
openchat/render.py   splash, banner, markdown, diffs
openchat/store.py    sessions, scratch, project dirs
openchat/serve.py    HTTP server
openchat/update.py   self-update and install helpers
install.sh           installer
libraries.txt        pip dependencies (none today)
```
