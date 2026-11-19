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
openchat --plan "outline a fix"   # read-only planning, no edits
openchat --json "..."             # script-friendly one-shot result
openchat --doctor                 # environment + connectivity check
openchat serve --port 8787        # headless HTTP server
```

Other flags: `--yes` (auto-approve), `--no-tools`, `--max-rounds N`,
`--temperature X`, `--snapshot` (save the project first), `--title "name"`,
`--theme <cyan|violet|amber|blue|mono>`.

The prompt has history (Up/Down), `/` command completion, path completion on
Tab, and persistent history in `~/.openchat/history`. End a line with `\` to
keep typing on the next line.`

Sessions live in `~/.openchat/sessions/` and reopen automatically when you start
`openchat` in the same directory. Scratch files go to `~/.openchat/tmp/<session>`
and project folders to `~/.openchat/projects/<session>`; scratch older than 30
days is cleaned up on launch.

### Approvals

Shell commands, and file tools that reach outside the project directory, ask first:

```
  ╭─ approve bash ─────────────────────────────────────────╮
  │ $ rm -rf build                                        │
  ╰─ [a] always  [y] approve  [n] reject  ›
```

`a` approves everything for the rest of the session, `y` approves once, `n` rejects and the model is told why. Rejections never abort the turn. If stdin is not a terminal (pipes, `openchat serve`), actions are allowed as before; set `"approvals": false` in `~/.openchat/config.json` to skip prompts entirely.

### In-session commands

| Command | What it does |
| --- | --- |
| `/new` | Start a fresh session |
| `/sessions` | List recent sessions, numbered |
| `/resume <n\|id>` | Switch to a saved session |
| `/title [text]` | Show or set the session title |
| `/search <text>` | Search every saved session |
| `/delete <n\|id>` | Remove a saved session |
| `/model [id]` | Show or change the model |
| `/cwd`, `/paths` | Show working/scratch folders |
| `/compact` | Summarize history to free context |
| `/retry` | Run the last message again |
| `/undo` | Revert the last file change |
| `/changes` | Files changed this session |
| `/export [file]` | Save a markdown transcript |
| `/todo` | Session task list (`add`, `done N`, `rm N`) |
| `/pin <file>`, `/pins`, `/unpin` | Keep files in every request |
| `/snapshot`, `/snapshots`, `/restore [n]` | Zip and roll back the project |
| `/status`, `/diff [--stat]` | Git status and working-tree diff |
| `/stats`, `/context` | Session counters and context budget |
| `/plan [on\|off]` | Plan-only mode (mutating tools removed) |
| `/config [key] [value]` | Inspect or set configuration |
| `/env` | Relevant environment variables |
| `/edit` | Compose this prompt in `$EDITOR` |
| `/prompt save <name> <text>`, `/prompt <name>` | Reusable prompts |
| `/theme [name]` | Switch the color theme |
| `/init` | Write an `OPENCHAT.md` project memory |
| `/doctor` | Check config, worker, git, permissions |
| `/think`, `/clear`, `/version` | Toggles and info |
| `/update` | Reinstall the latest release |
| `/exit` | Quit |

Mention a local file as `@path/to/file` to attach its contents to the message.
Files pinned with `/pin` and notes in `OPENCHAT.md` are injected into every
request. Set `"auto_compact": <tokens>` to compact automatically before a turn
once the context grows past that estimate.

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
| `multi_edit` | Several exact replacements in one atomic write |
| `mkdir` | Create directories |
| `move` | Rename or move a file or directory |
| `delete` | Remove a file or directory (always approved) |
| `tree` | Compact directory tree, depth 1-4 |
| `fetch` | Fetch an http(s) URL as text (always approved) |

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
  "approvals": true,
  "clear": true,
  "theme": "cyan",
  "temperature": 0.3,
  "auto_compact": 0,
  "bell": true
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

### Seeing what the model sees

`/context` breaks down the system prompt, memory file, pinned files, and
history by estimated tokens. `/stats` adds message/tool counts and how many
files changed this session. `/changes` lists the touched paths, and `/undo`
walks backwards through them (snapshots made by `/snapshot` roll back whole
directories at once).

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
