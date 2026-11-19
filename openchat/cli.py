"""Command line entrypoint and interactive REPL."""

import argparse
import json
import os
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

from . import editor, input_ui, journal, promptstore, snapshots
from .agent import agent_run
from .approve import ask as ask_approval
from .config import (
    HOME,
    SESSIONS,
    TMP_TTL_DAYS,
    install_dir,
    load_config,
    save_config,
    version,
)
from .doctor import run as doctor_run
from .interrupt import watch
from .render import (
    THEME_NAMES,
    MarkdownPrinter,
    accent,
    bold,
    boxed_banner,
    clear_screen,
    context_estimate,
    dim,
    muted,
    ok,
    print_footer,
    print_help,
    print_tool_call,
    print_tool_result,
    red,
    render_history,
    set_theme,
    short_path,
    splash,
    warn,
)
from .serve import serve_main
from .store import (
    describe_session,
    ensure_dirs,
    latest_session,
    load_session,
    new_session,
    purge_tmp,
    save_session,
    search_sessions,
    session_title,
)
from .update import UpdateError, perform_update

OPENCHAT_MD = """# OPENCHAT.md

Project notes loaded into every openchat request made from this directory.
Keep it short and factual.

## Overview
- what this project is

## Commands
- test:
- build:
- lint:

## Conventions
- style, patterns, gotchas
"""


def compact_session(cfg, session, provider):
    messages = session.get("messages", [])
    if len([m for m in messages if m.get("role") != "system"]) < 3:
        print(dim("not enough history to compact yet"))
        return
    from .agent import complete

    lines = []
    for message in messages:
        role = message.get("role")
        content = (message.get("content") or "").strip()
        if role == "system":
            continue
        if role == "tool":
            lines.append(f"tool result: {content[:300]}")
            continue
        calls = message.get("tool_calls") or []
        if calls:
            names = ", ".join(c.get("function", {}).get("name", "?") for c in calls)
            content += f" [tools: {names}]"
        if content:
            lines.append(f"{role}: {content[:1500]}")
    transcript = "\n".join(lines)[-60000:]
    prompt = [
        {
            "role": "system",
            "content": (
                "You compress coding sessions. Preserve decisions, file paths, "
                "commands run, errors, and open tasks. Be terse, no preamble."
            ),
        },
        {"role": "user", "content": "Summarize this session for continued work:\n\n" + transcript},
    ]
    print(dim("compacting…"))
    try:
        reply, _provider, _model = complete(cfg, prompt, [], provider, None, False)
    except Exception as exc:
        print(red(f"compact failed: {exc}"))
        return
    summary = (reply.get("content") or "").strip()
    if not summary:
        print(red("compact failed: empty summary"))
        return
    before = len(messages)
    session["messages"] = [
        {"role": "user", "content": "[conversation summary — earlier turns compacted]\n" + summary}
    ]
    save_session(session)
    print(dim(f"compacted {before} messages → 1 · ≈ {max(1, len(summary) // 4)} tok"))


def export_session(session, target):
    lines = [
        f"# openchat session {session.get('id', '?')}",
        "",
        f"- cwd: `{session.get('cwd', '?')}`",
        f"- model: `{session.get('model', '?')}`",
        f"- exported: {time.strftime('%Y-%m-%d %H:%M')}",
        "",
    ]
    for message in session.get("messages", []):
        role = message.get("role")
        content = (message.get("content") or "").rstrip()
        if role == "system":
            continue
        if role == "tool":
            lines += ["<details><summary>tool result</summary>", "", "```", content[:4000], "```", "", "</details>", ""]
            continue
        lines += ["## user" if role == "user" else "## assistant", "", content or "_(no text)_", ""]
        for call in message.get("tool_calls") or []:
            fn = call.get("function", {})
            lines += [f"_tool call: `{fn.get('name')}` {fn.get('arguments', '')[:300]}_", ""]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines).rstrip() + "\n")
    return target


def recent_sessions(limit=10):
    files = sorted(SESSIONS.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)[:limit]
    out = []
    for path in files:
        data = load_session(path.stem)
        if data:
            out.append(data)
    return out


def resolve_session_arg(value):
    value = value.strip()
    if value.isdigit():
        items = recent_sessions()
        index = int(value) - 1
        if 0 <= index < len(items):
            return items[index]
        return None
    return load_session(value)


def expand_attachments(text, cwd):
    import re as _re

    found = []
    for match in _re.findall(r"(?<![\w@])@([^\s,;]+)", text):
        raw = match.rstrip(").,]")
        path = Path(raw)
        if not path.is_absolute():
            path = Path(cwd) / path
        try:
            if path.is_file() and path.stat().st_size <= 400_000:
                found.append((raw, path.read_text(errors="replace")))
        except OSError:
            continue
    if not found:
        return text
    budget = 24000
    blocks = "\n\nAttached files:"
    for name, body in found:
        if budget <= 0:
            break
        body = body[:budget]
        budget -= len(body)
        blocks += f"\n\n--- {name} ---\n{body}"
    print(dim(f"attached {len(found)} file{'s' if len(found) != 1 else ''}"))
    return text + blocks


def maybe_compact(cfg, session, provider):
    limit = int(cfg.get("auto_compact") or 0)
    if limit <= 0:
        return
    messages = session.get("messages") or []
    if len(messages) > 6 and context_estimate(messages) > limit:
        print(dim(f"context ≈ {context_estimate(messages):,} tok — compacting first"))
        compact_session(cfg, session, provider)


def run_turn(cfg, session, prompt, provider, tools=None):
    tty = sys.stdout.isatty()
    quiet = bool(cfg.get("quiet"))
    printer = None if quiet else MarkdownPrinter(enabled=tty and not cfg.get("raw"))
    thinking = tty and not cfg.get("raw") and not quiet and bool(cfg.get("thinking", True))
    tool_count = 0

    def on_tool(name, args):
        nonlocal tool_count
        tool_count += 1
        if not quiet:
            print_tool_call(name, args)

    def on_tool_result(name, args, result, elapsed=None):
        if not quiet:
            print_tool_result(name, args, result, elapsed)

    ensure_dirs(session)
    with watch():
        result = agent_run(
            cfg,
            session["cwd"],
            session["messages"],
            prompt,
            provider,
            printer,
            on_tool=on_tool,
            on_tool_result=on_tool_result,
            approve=lambda name, args: ask_approval(cfg, session, name, args),
            project=session.get("project"),
            tmp=session.get("tmp"),
            thinking=thinking,
            tools=tools,
            pins=session.get("pins"),
            todos=session.get("todos"),
            plan=bool(session.get("plan")),
        )
    session["messages"] = result["messages"]
    if result.get("model"):
        session["model"] = result["model"]
    if not session.get("title") and not quiet:
        title = prompt.strip().splitlines()[0][:60]
        if title and not title.startswith("["):
            session["title"] = title
    if result.get("error"):
        print(red(f"error: {result['error']}"))
        session["messages"].append(
            {"role": "assistant", "content": f"[request failed: {result['error']}]"}
        )
    elif result.get("interrupted"):
        print(muted("  ╰─ ") + warn("interrupted"))
    elif tty and not quiet:
        print_footer(
            result.get("provider") or None,
            result.get("model") or None,
            result.get("elapsed"),
            tool_count,
            context_estimate(result["messages"]) if result.get("messages") else None,
        )
    if (
        result.get("elapsed")
        and tty
        and not quiet
        and cfg.get("bell", True)
        and (result.get("elapsed") or 0) >= 10
    ):
        sys.stdout.write("\a")
        sys.stdout.flush()
    save_session(session)
    return result



def repl(cfg, session, provider, first_prompt=None, tools=None):
    input_ui.setup(HOME / "history", session["cwd"])
    if first_prompt is None:
        try:
            first_prompt = splash(cfg, session)
        except SystemExit:
            return
    if cfg.get("clear", True):
        clear_screen()
    if sys.stdout.isatty():
        boxed_banner(cfg, session)
    if session.get("messages"):
        print(dim("resumed — replaying the conversation below"))
        render_history(session)
    pending = first_prompt or None
    while True:
        if pending:
            line = pending
            pending = None
            if sys.stdout.isatty():
                print()
                print(bold(accent("› ")) + line)
        else:
            if sys.stdout.isatty():
                print()
                prompt_text = input_ui.prompt(bold(accent("› ")))
            else:
                prompt_text = ""
            try:
                line = input(prompt_text).strip()
            except EOFError:
                print()
                return
            except KeyboardInterrupt:
                print()
                print(muted("  ^C"))
                continue
        if sys.stdout.isatty() and line.endswith("\\"):
            chunks = [line[:-1]]
            while True:
                try:
                    more = input(input_ui.prompt(bold(accent("  … "))))
                except (EOFError, KeyboardInterrupt):
                    break
                if more.endswith("\\"):
                    chunks.append(more[:-1])
                    continue
                chunks.append(more)
                break
            line = "\n".join(chunks).strip()
        if not line:
            continue
        if line in ("/q",):
            return
        if line == "/?":
            print_help()
            continue
        if line == "/u":
            line = "/undo"
        if line in ("/exit", "/quit"):
            return
        if line == "/help":
            print_help()
            continue
        if line == "/new":
            session = new_session(Path(session["cwd"]), cfg["model"])
            save_session(session)
            print(dim(f"new session {session['id']} · fresh context"))
            continue
        if line == "/sessions":
            items = recent_sessions()
            if not items:
                print("  (none)")
                continue
            for index, data in enumerate(items, 1):
                print(f"  {accent(str(index).rjust(2))}  " + describe_session(data))
            continue
        if line.startswith("/resume"):
            parts = line.split(maxsplit=1)
            if len(parts) < 2:
                print(dim("usage: /resume <number|session-id>"))
                continue
            data = resolve_session_arg(parts[1])
            if not data:
                print(dim("no such session"))
                continue
            data["cwd"] = session["cwd"]
            session = data
            save_session(session)
            input_ui.set_cwd(session["cwd"])
            print()
            boxed_banner(cfg, session)
            render_history(session)
            continue
        if line.startswith("/delete"):
            parts = line.split(maxsplit=1)
            if len(parts) < 2:
                print(dim("usage: /delete <number|session-id>"))
                continue
            data = resolve_session_arg(parts[1])
            if not data:
                print(dim("no such session"))
                continue
            try:
                (SESSIONS / f"{data['id']}.json").unlink()
            except OSError as exc:
                print(red(f"could not delete: {exc}"))
                continue
            print(dim(f"deleted {data['id']}"))
            if data["id"] == session["id"]:
                session = new_session(Path(session["cwd"]), cfg["model"])
                save_session(session)
                print(dim(f"new session {session['id']}"))
            continue
        if line.startswith("/title"):
            parts = line.split(maxsplit=1)
            if len(parts) < 2:
                print(f"  {session_title(session)}")
                continue
            session["title"] = parts[1].strip()
            save_session(session)
            print(dim("title updated"))
            continue
        if line.startswith("/search"):
            parts = line.split(maxsplit=1)
            if len(parts) < 2:
                print(dim("usage: /search <text>"))
                continue
            hits = search_sessions(parts[1])
            if not hits:
                print(dim("no matches"))
                continue
            for data, snippet in hits:
                print("  " + accent(data.get("id", "?")) + muted(f"  {session_title(data)[:48]}"))
                print("     " + muted(snippet))
            continue
        if line.startswith("/model"):
            parts = line.split(maxsplit=1)
            if len(parts) == 2:
                cfg["model"] = parts[1].strip()
                print(dim(f"model → {cfg['model']}"))
            else:
                print(f"  {cfg['model']} (fallbacks: {', '.join(cfg.get('models', []))})")
            continue
        if line == "/cwd":
            print(f"  {session['cwd']}")
            continue
        if line == "/update":
            print(dim("checking for a new version…"))
            try:
                info = perform_update(cfg)
            except UpdateError as exc:
                print(red(f"update failed: {exc}"))
                continue
            print(dim(f"updated {info['previous']} → {info['version']} — restarting"))
            input_ui.save()
            os.environ["OPENCHAT_SKIP_SPLASH"] = "1"
            main_py = Path(info["dir"]) / "main.py"
            os.execv(
                sys.executable,
                [sys.executable, str(main_py), "--cwd", session["cwd"],
                 "--session", session["id"]],
            )
            continue
        if line == "/think":
            cfg["thinking"] = not cfg.get("thinking", True)
            print(dim("thinking " + ("on" if cfg["thinking"] else "off")))
            continue
        if line == "/paths":
            print(f"  cwd      {session['cwd']}")
            print(f"  project  {session.get('project')}")
            print(f"  tmp      {session.get('tmp')}  (auto-deleted after {TMP_TTL_DAYS} days)")
            continue
        if line == "/todo" or line.startswith("/todo "):
            parts = line.split(maxsplit=2)
            todos = session.setdefault("todos", [])
            action = parts[1].lower() if len(parts) > 1 else ""
            if not action or action in ("list", "ls"):
                if not todos:
                    print(dim("no tasks — /todo add <text>"))
                    continue
                for index, task in enumerate(todos, 1):
                    mark = ok("✓") if task.get("done") else muted("○")
                    text = muted(task.get("text", "")) if task.get("done") else task.get("text", "")
                    print(f"  {index:>2}  {mark}  {text}")
                continue
            if action == "add":
                text = parts[2].strip() if len(parts) > 2 else ""
                if not text:
                    print(dim("usage: /todo add <text>"))
                    continue
                todos.append({"text": text, "done": False})
                save_session(session)
                print(dim(f"added task {len(todos)}"))
                continue
            if action in ("done", "rm", "remove", "open", "undone"):
                try:
                    index = int(parts[2]) - 1
                except (IndexError, ValueError):
                    print(dim(f"usage: /todo {action} <number>"))
                    continue
                if not 0 <= index < len(todos):
                    print(dim("no such task"))
                    continue
                if action == "done":
                    todos[index]["done"] = True
                elif action in ("rm", "remove"):
                    todos.pop(index)
                else:
                    todos[index]["done"] = False
                save_session(session)
                print(dim("updated"))
                continue
            if action == "clear":
                session["todos"] = []
                save_session(session)
                print(dim("cleared"))
                continue
            print(dim("usage: /todo [add <text> | done N | open N | rm N | clear]"))
            continue
        if line == "/pin" or line.startswith("/pin "):
            parts = line.split(maxsplit=1)
            if len(parts) == 1 or parts[1].strip() in ("list", "ls"):
                pins = session.get("pins") or []
                if not pins:
                    print(dim("no pinned files — /pin <path>"))
                for pin in pins:
                    print("  " + accent("•") + " " + short_path(pin))
                continue
            arg = parts[1].strip()
            if arg in ("all", "clear"):
                session["pins"] = []
                save_session(session)
                print(dim("pins cleared"))
                continue
            path = Path(arg).expanduser()
            if not path.is_absolute():
                path = Path(session["cwd"]) / path
            if not path.is_file():
                print(dim(f"not a file: {arg}"))
                continue
            try:
                rel = str(path.resolve().relative_to(Path(session["cwd"]).resolve()))
            except Exception:
                rel = str(path.resolve())
            pins = session.setdefault("pins", [])
            if rel in pins:
                print(dim("already pinned"))
            else:
                pins.append(rel)
                save_session(session)
                print(dim(f"pinned {rel}"))
            continue
        if line == "/pins":
            pins = session.get("pins") or []
            if not pins:
                print(dim("no pinned files — /pin <path>"))
            for pin in pins:
                print("  " + accent("•") + " " + short_path(pin))
            continue
        if line.startswith("/unpin"):
            parts = line.split(maxsplit=1)
            pins = session.get("pins") or []
            if len(parts) == 1:
                session["pins"] = []
                save_session(session)
                print(dim("pins cleared"))
                continue
            arg = parts[1].strip()
            before = len(pins)
            session["pins"] = [pin for pin in pins if pin != arg and Path(pin).name != Path(arg).name]
            save_session(session)
            print(dim(f"unpinned {before - len(session['pins'])}"))
            continue
        if line == "/snapshot":
            info = snapshots.create(session.get("tmp"), session["cwd"])
            print(dim(f"snapshot {info['name']} · {info['files']} files · {info['size'] // 1024} KB"))
            continue
        if line in ("/snapshots", "/snaps"):
            items = snapshots.listing(session.get("tmp"))
            if not items:
                print(dim("no snapshots yet — /snapshot"))
                continue
            for index, item in enumerate(items, 1):
                print(f"  {index:>2}  {muted(item['name'])}  {muted(str(item['size'] // 1024) + ' KB')}")
            continue
        if line.startswith("/restore"):
            parts = line.split(maxsplit=1)
            index = None
            if len(parts) == 2 and parts[1].strip().isdigit():
                index = int(parts[1].strip()) - 1
            result = snapshots.restore(session.get("tmp"), session["cwd"], index)
            if not result:
                print(dim("no snapshots to restore"))
                continue
            print(dim(f"restored {result['restored']} files from {result['name']}"))
            continue
        if line == "/status":
            try:
                branch = subprocess.run(
                    ["git", "-C", session["cwd"], "branch", "--show-current"],
                    capture_output=True, text=True, timeout=2,
                )
                if branch.returncode != 0:
                    print(dim("not a git repository"))
                    continue
                status = subprocess.run(
                    ["git", "-C", session["cwd"], "status", "--short"],
                    capture_output=True, text=True, timeout=5,
                )
                print("  " + muted("branch  ") + bold(accent(branch.stdout.strip() or "detached")))
                lines = [line for line in status.stdout.splitlines() if line.strip()]
                if not lines:
                    print("  " + muted("clean"))
                    continue
                for entry in lines[:30]:
                    print("  " + muted(entry))
                if len(lines) > 30:
                    print("  " + muted(f"… {len(lines) - 30} more"))
            except Exception as exc:
                print(red(f"git failed: {exc}"))
            continue
        if line.startswith("/diff"):
            stat = "--stat" in line
            command = ["git", "-C", session["cwd"], "diff"] + (["--stat"] if stat else [])
            try:
                output = subprocess.run(command, capture_output=True, text=True, timeout=10).stdout
            except Exception as exc:
                print(red(f"git failed: {exc}"))
                continue
            if not output.strip():
                print(dim("no changes"))
                continue
            lines = output.splitlines()
            for entry in lines[:200]:
                if entry.startswith("+") and not entry.startswith("+++"):
                    print(ok(entry))
                elif entry.startswith("-") and not entry.startswith("---"):
                    print(red(entry))
                else:
                    print(muted(entry))
            if len(lines) > 200:
                print(dim(f"… {len(lines) - 200} more lines"))
            continue
        if line == "/stats":
            messages = session.get("messages", [])
            counts = Counter(message.get("role") for message in messages)
            calls = sum(len(message.get("tool_calls") or []) for message in messages)
            changes = journal.changed(session.get("tmp"))
            open_todos = len([task for task in session.get("todos") or [] if not task.get("done")])
            rows = [
                ("session", str(session.get("id"))),
                ("title", session_title(session)),
                ("model", cfg.get("model", "?")),
                ("messages", f"{len(messages)}  (user {counts.get('user', 0)}, assistant {counts.get('assistant', 0)}, tool {counts.get('tool', 0)})"),
                ("tool calls", str(calls)),
                ("context", f"≈ {context_estimate(messages):,} tok"),
                ("changes", str(len(changes))),
                ("todos", f"{open_todos} open"),
                ("pins", str(len(session.get("pins") or []))),
                ("scratch", short_path(session.get("tmp") or "")),
            ]
            for key, value in rows:
                print("  " + muted(f"{key:<13}") + value)
            continue
        if line == "/context":
            from .prompts import SYSTEM_PROMPT

            base = len(SYSTEM_PROMPT)
            memory_path = Path(session["cwd"]) / "OPENCHAT.md"
            memory = memory_path.stat().st_size if memory_path.is_file() else 0
            pins_size = 0
            for pin in session.get("pins") or []:
                path = Path(pin)
                if not path.is_absolute():
                    path = Path(session["cwd"]) / path
                try:
                    pins_size += path.stat().st_size
                except OSError:
                    continue
            history = sum(
                len(json.dumps(message))
                for message in session.get("messages", [])
                if message.get("role") != "system"
            )
            total = base + memory + pins_size + history
            for key, value in (
                ("system", base),
                ("memory", memory),
                ("pins", pins_size),
                ("history", history),
            ):
                print("  " + muted(f"{key:<10}") + f"{value // 4:,} tok")
            print("  " + muted(f"{'total':<10}") + bold(f"{total // 4:,} tok"))
            continue
        if line == "/plan" or line.startswith("/plan "):
            parts = line.split(maxsplit=1)
            if len(parts) == 1:
                print(dim("plan mode " + ("on" if session.get("plan") else "off")))
                continue
            session["plan"] = parts[1].strip().lower() in ("on", "true", "1", "yes")
            save_session(session)
            print(dim("plan mode " + ("on" if session["plan"] else "off")))
            continue
        if line == "/config" or line.startswith("/config "):
            parts = line.split(maxsplit=2)
            if len(parts) == 1:
                for key in sorted(cfg):
                    value = cfg.get(key)
                    if key in ("api_key", "worker_token") and value:
                        value = "set"
                    elif isinstance(value, (dict, list)):
                        value = json.dumps(value)[:60]
                    print("  " + muted(f"{key:<15}") + str(value))
                continue
            key = parts[1].strip()
            if len(parts) == 2:
                value = cfg.get(key)
                if key in ("api_key", "worker_token") and value:
                    value = "set"
                print("  " + muted(f"{key:<15}") + str(value))
                continue
            raw = parts[2].strip()
            low = raw.lower()
            if low in ("true", "false"):
                value = low == "true"
            else:
                try:
                    value = int(raw)
                except ValueError:
                    try:
                        value = float(raw)
                    except ValueError:
                        value = raw
            cfg[key] = value
            if key == "theme":
                set_theme(value)
            save_config(cfg)
            print(dim(f"{key} → {value}"))
            continue
        if line == "/env":
            names = [
                "OPENCHAT_TOKEN", "OPENCHAT_BASE", "OPENCHAT_API_KEY", "NO_COLOR",
                "TERM", "TERM_PROGRAM", "EDITOR", "VISUAL", "SHELL", "LANG",
                "OPENCHAT_PYTHON",
            ]
            shown = 0
            for name in names:
                value = os.environ.get(name)
                if value is None:
                    continue
                if "TOKEN" in name or "KEY" in name:
                    value = (value[:4] + "…" + value[-4:]) if len(value) > 10 else "set"
                print("  " + muted(f"{name:<18}") + value)
                shown += 1
            if not shown:
                print(dim("no openchat-specific variables set"))
            continue
        if line == "/version":
            print(f"  openchat {version()}")
            continue
        if line == "/edit":
            text = editor.compose(session.get("tmp"))
            if not text:
                print(dim("empty prompt"))
                continue
            print(dim(f"composed {len(text.splitlines())} lines"))
            maybe_compact(cfg, session, provider)
            run_turn(cfg, session, text, provider, tools=tools)
            continue
        if line in ("/prompt", "/prompts"):
            names = promptstore.listing(HOME)
            if not names:
                print(dim("no saved prompts — /prompt save <name> <text>"))
            for name in names:
                print("  " + accent("•") + " " + name)
            continue
        if line.startswith("/prompt "):
            parts = line.split(maxsplit=2)
            action = parts[1].strip()
            if action == "save":
                if len(parts) < 3 or not parts[2].strip():
                    print(dim("usage: /prompt save <name> <text>"))
                    continue
                spec = parts[2].strip()
                name, _, text = spec.partition(" ")
                text = text.strip()
                if not text:
                    text = next(
                        (
                            message.get("content", "")
                            for message in reversed(session.get("messages", []))
                            if message.get("role") == "user"
                        ),
                        "",
                    )
                saved = promptstore.save(HOME, name, text)
                print(dim(f"saved {name}") if saved else dim("bad prompt name"))
                continue
            if action in ("rm", "delete"):
                name = parts[2].strip() if len(parts) > 2 else ""
                removed = promptstore.remove(HOME, name)
                print(dim(f"removed {name}") if removed else dim("no such prompt"))
                continue
            text = promptstore.load(HOME, action)
            if not text:
                print(dim(f"no prompt named {action}"))
                continue
            maybe_compact(cfg, session, provider)
            run_turn(cfg, session, text, provider, tools=tools)
            continue
        if line == "/clear":
            clear_screen()
            if sys.stdout.isatty():
                boxed_banner(cfg, session)
            continue
        if line.startswith("/theme"):
            parts = line.split(maxsplit=1)
            if len(parts) == 2:
                name = set_theme(parts[1].strip())
                cfg["theme"] = name
                save_config(cfg)
                print(dim(f"theme → {name}"))
            else:
                print(f"  {cfg.get('theme', 'cyan')}  ({', '.join(THEME_NAMES)})")
            continue
        if line == "/compact":
            compact_session(cfg, session, provider)
            continue
        if line == "/retry":
            last = None
            for index in range(len(session.get("messages", [])) - 1, -1, -1):
                message = session["messages"][index]
                content = message.get("content")
                if (
                    message.get("role") == "user"
                    and isinstance(content, str)
                    and not content.startswith("[conversation summary")
                ):
                    last = (index, content)
                    break
            if not last:
                print(dim("nothing to retry"))
                continue
            index, content = last
            session["messages"] = session["messages"][:index]
            print(dim("retrying last message"))
            run_turn(cfg, session, content, provider, tools=tools)
            continue
        if line == "/undo":
            info = journal.undo(session.get("tmp"))
            if not info:
                print(dim("nothing to undo"))
            elif info["restored"]:
                print(dim(f"restored {short_path(info['path'])}"))
            else:
                print(dim(f"removed {short_path(info['path'])} (it did not exist before)"))
            continue
        if line == "/changes":
            changes = journal.changed(session.get("tmp"))
            if not changes:
                print(dim("no file changes yet"))
                continue
            for path, count in sorted(changes.items()):
                unit = "change" if count == 1 else "changes"
                print("  " + accent("•") + " " + muted(short_path(path)) + muted(f"  ·  {count} {unit}"))
            continue
        if line.startswith("/export"):
            parts = line.split(maxsplit=1)
            if len(parts) == 2:
                target = Path(parts[1].strip()).expanduser()
            else:
                target = Path(session["cwd"]) / f"openchat-{session['id']}.md"
            try:
                target = export_session(session, target)
            except OSError as exc:
                print(red(f"export failed: {exc}"))
                continue
            print(dim(f"wrote {target}"))
            continue
        if line.startswith("/init"):
            parts = line.split(maxsplit=1)
            force = len(parts) == 2 and parts[1].strip() in ("force", "--force", "-f")
            target = Path(session["cwd"]) / "OPENCHAT.md"
            if target.exists() and not force:
                print(dim("OPENCHAT.md exists — /init force to replace it"))
                continue
            try:
                target.write_text(OPENCHAT_MD)
            except OSError as exc:
                print(red(f"could not write OPENCHAT.md: {exc}"))
                continue
            print(dim(f"wrote {short_path(str(target))} — loaded into every request"))
            continue
        if line == "/doctor":
            doctor_run(cfg, session)
            continue
        if line.startswith("/"):
            print(dim("unknown command — /help"))
            continue
        prompt_text = expand_attachments(line, session["cwd"])
        maybe_compact(cfg, session, provider)
        run_turn(cfg, session, prompt_text, provider, tools=tools)



def main():
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        serve_main(sys.argv[2:])
        return
    parser = argparse.ArgumentParser(
        prog="openchat",
        description="terminal coding assistant (use 'openchat serve' for the headless HTTP server)",
    )
    parser.add_argument("prompt", nargs="*", help="one-shot request")
    parser.add_argument("-m", "--model", help="model id")
    parser.add_argument("-c", "--continue", dest="resume", action="store_true",
                        help="resume the most recent session (any directory)")
    parser.add_argument("--new", action="store_true", help="start a fresh session")
    parser.add_argument("--session", help="resume a specific session id")
    parser.add_argument("--provider", choices=["auto", "novita", "worker"], default=None)
    parser.add_argument("--update", action="store_true",
                        help="download and install the latest release, then exit")
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--raw", action="store_true", help="print unrendered markdown")
    parser.add_argument("--think", action=argparse.BooleanOptionalAction, default=None,
                        help="show the model's reasoning (default: on in a terminal)")
    parser.add_argument("--clear", action=argparse.BooleanOptionalAction, default=None,
                        help="clear the terminal before starting (default: yes)")
    parser.add_argument("--theme", help="color theme: " + ", ".join(THEME_NAMES))
    parser.add_argument("--yes", action="store_true",
                        help="approve every action for this run")
    parser.add_argument("--no-tools", action="store_true", help="answer without tools")
    parser.add_argument("--plan", action="store_true", help="start in plan-only mode")
    parser.add_argument("--temperature", type=float, help="sampling temperature")
    parser.add_argument("--doctor", action="store_true", help="run diagnostics and exit")
    parser.add_argument("--snapshot", action="store_true",
                        help="snapshot the project before starting")
    parser.add_argument("--title", help="name this session")
    parser.add_argument("--json", action="store_true",
                        help="one-shot mode: print the result as JSON")
    args = parser.parse_args()

    cfg = load_config()
    if args.theme:
        cfg["theme"] = set_theme(args.theme)
    else:
        cfg["theme"] = set_theme(cfg.get("theme"))
    if args.version:
        from .config import version

        print(f"openchat {version()}")
        return
    if args.update:
        try:
            info = perform_update(cfg)
        except UpdateError as exc:
            print(red(f"update failed: {exc}"), file=sys.stderr)
            sys.exit(1)
        print(f"updated {info['previous']} → {info['version']} ({info['dir']})")
        return
    from .setup import ensure_configured

    if not ensure_configured(cfg):
        sys.exit(1)
    if args.raw:
        cfg["raw"] = True
    if args.think is not None:
        cfg["thinking"] = args.think
    if args.clear is not None:
        cfg["clear"] = args.clear
    if args.model:
        cfg["model"] = args.model
    if args.yes:
        cfg["approvals"] = False
    if args.temperature is not None:
        cfg["temperature"] = args.temperature
    if args.json:
        cfg["quiet"] = True
        cfg["raw"] = True
        cfg["clear"] = False
        cfg["bell"] = False
    tools = [] if args.no_tools else None

    provider = args.provider or cfg.get("provider", "auto")

    cwd = Path(args.cwd).expanduser().resolve()
    if not cwd.exists():
        try:
            cwd.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(red(f"cannot create {cwd}: {exc}"), file=sys.stderr)
            sys.exit(1)
    elif not cwd.is_dir():
        print(red(f"not a directory: {cwd}"), file=sys.stderr)
        sys.exit(1)
    session = None
    if args.session:
        session = load_session(args.session)
        if not session:
            print(red(f"no session named {args.session}"), file=sys.stderr)
            sys.exit(1)
        session["cwd"] = str(cwd)
    elif args.resume:
        last = latest_session()
        if last:
            session = load_session(last)
            if session:
                session["cwd"] = str(cwd)
    elif not args.new:
        # Sessions persist: reopening openchat in the same directory picks up
        # where the last one left off.
        last = latest_session(cwd)
        if last:
            session = load_session(last)
    if session:
        ensure_dirs(session)
        save_session(session)
    if not session:
        session = new_session(cwd, cfg["model"])
        save_session(session)
    if args.plan:
        session["plan"] = True
    if args.title:
        session["title"] = args.title
        save_session(session)
    purge_tmp()

    if args.doctor:
        doctor_run(cfg, session)
        return
    if args.snapshot:
        info = snapshots.create(session.get("tmp"), session["cwd"])
        print(dim(f"snapshot {info['name']} · {info['files']} files · {info['size'] // 1024} KB"))

    prompt = " ".join(args.prompt).strip()
    if prompt:
        prompt = expand_attachments(prompt, session["cwd"])
        result = run_turn(cfg, session, prompt, provider, tools=tools)
        if args.json:
            print(json.dumps({
                "content": (result or {}).get("content", ""),
                "provider": (result or {}).get("provider"),
                "model": (result or {}).get("model"),
                "elapsed": (result or {}).get("elapsed"),
                "error": (result or {}).get("error"),
                "session": session["id"],
            }, indent=1))
        return
    repl(cfg, session, provider, tools=tools)


