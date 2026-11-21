"""Command line entrypoint and interactive REPL (startup-optimized).

Heavy modules (agent, account, update, doctor, serve, snapshots, journal,
editor, promptstore, input_ui, interrupt, approve) are imported lazily, only
in the code paths that use them.
"""

import argparse
import json
import os
import re
import sys
import threading
from pathlib import Path

from .config import (
    HOME,
    SESSIONS,
    TMP_TTL_DAYS,
    install_dir,
    load_config,
    save_config,
    version,
)
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

ACCOUNT_COMMANDS = ("login", "signin", "signup", "logout", "whoami", "account")
ATTACH_RE = re.compile(r"(?<![\w@])@([^\s,;]+)")
ATTACH_MAX_BYTES = 400_000
ATTACH_BUDGET = 24_000
REPLAY_LIMIT = 40


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def compact_session(cfg, session, provider):
    messages = session.get("messages", [])
    if sum(1 for m in messages if m.get("role") != "system") < 3:
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
    import time

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


def _recent_paths(limit=10):
    """Most recently modified session files, newest first (stat only, no JSON parsing)."""
    entries = []
    try:
        with os.scandir(SESSIONS) as it:
            for entry in it:
                if entry.name.endswith(".json"):
                    try:
                        entries.append((entry.stat().st_mtime, entry.name[:-5]))
                    except OSError:
                        continue
    except OSError:
        return []
    entries.sort(reverse=True)
    return [stem for _mtime, stem in entries[:limit]]


def recent_sessions(limit=10):
    out = []
    for stem in _recent_paths(limit):
        data = load_session(stem)
        if data:
            out.append(data)
    return out


def resolve_session_arg(value):
    value = value.strip()
    if value.isdigit():
        index = int(value) - 1
        stems = _recent_paths()
        if 0 <= index < len(stems):
            return load_session(stems[index])  # parse only the one we need
        return None
    return load_session(value)


def expand_attachments(text, cwd):
    if "@" not in text:
        return text
    import stat as _stat

    budget = ATTACH_BUDGET
    blocks = "\n\nAttached files:"
    attached = 0
    for match in ATTACH_RE.findall(text):
        if budget <= 0:
            break
        raw = match.rstrip(").,]")
        path = Path(raw)
        if not path.is_absolute():
            path = Path(cwd) / path
        try:
            info = path.stat()
            if not _stat.S_ISREG(info.st_mode) or info.st_size > ATTACH_MAX_BYTES:
                continue
            with path.open(errors="replace") as handle:
                body = handle.read(budget)  # never read more than we can use
        except OSError:
            continue
        budget -= len(body)
        attached += 1
        blocks += f"\n\n--- {raw} ---\n{body}"
    if not attached:
        return text
    print(dim(f"attached {attached} file{'s' if attached != 1 else ''}"))
    return text + blocks


def maybe_compact(cfg, session, provider):
    limit = int(cfg.get("auto_compact") or 0)
    messages = session.get("messages") or []
    if limit <= 0 or len(messages) <= 6:
        return
    estimate = context_estimate(messages)
    if estimate > limit:
        print(dim(f"context ≈ {estimate:,} tok — compacting first"))
        compact_session(cfg, session, provider)


def replay(session, limit=REPLAY_LIMIT):
    """Render only the tail of a long conversation."""
    messages = session.get("messages") or []
    if len(messages) <= limit:
        render_history(session)
        return
    tail = messages[-limit:]
    # start on a user turn so we never open on an orphaned tool result
    for index, message in enumerate(tail):
        if message.get("role") == "user":
            tail = tail[index:]
            break
    hidden = len(messages) - len(tail)
    print(dim(f"… {hidden} earlier messages hidden (/export to save the full log)"))
    render_history({**session, "messages": tail})


def git_status(cwd):
    import subprocess

    try:
        out = subprocess.run(
            ["git", "-C", cwd, "status", "--short", "--branch"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception as exc:
        print(red(f"git failed: {exc}"))
        return
    if out.returncode != 0:
        print(dim("not a git repository"))
        return
    rows = out.stdout.splitlines()
    head = rows[0][3:] if rows and rows[0].startswith("## ") else ""
    branch = head.split("...")[0].strip()
    for prefix in ("No commits yet on ", "Initial commit on "):
        if branch.startswith(prefix):
            branch = branch[len(prefix):]
    if not branch or branch.startswith("HEAD (no branch)"):
        branch = "detached"
    print("  " + muted("branch  ") + bold(accent(branch)))
    entries = [row for row in rows[1:] if row.strip()]
    if not entries:
        print("  " + muted("clean"))
        return
    for entry in entries[:30]:
        print("  " + muted(entry))
    if len(entries) > 30:
        print("  " + muted(f"… {len(entries) - 30} more"))


def git_diff(cwd, stat):
    import subprocess

    command = ["git", "-C", cwd, "diff"] + (["--stat"] if stat else [])
    try:
        output = subprocess.run(command, capture_output=True, text=True, timeout=10).stdout
    except Exception as exc:
        print(red(f"git failed: {exc}"))
        return
    if not output.strip():
        print(dim("no changes"))
        return
    rows = output.splitlines()
    for entry in rows[:200]:
        if entry.startswith("+") and not entry.startswith("+++"):
            print(ok(entry))
        elif entry.startswith("-") and not entry.startswith("---"):
            print(red(entry))
        else:
            print(muted(entry))
    if len(rows) > 200:
        print(dim(f"… {len(rows) - 200} more lines"))


# --------------------------------------------------------------------------
# a single model turn
# --------------------------------------------------------------------------

def run_turn(cfg, session, prompt, provider, tools=None):
    from .agent import agent_run
    from .approve import ask as ask_approval
    from .interrupt import watch

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
    elapsed = result.get("elapsed") or 0
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
    if elapsed >= 10 and tty and not quiet and cfg.get("bell", True):
        sys.stdout.write("\a")
        sys.stdout.flush()
    save_session(session)
    return result


# --------------------------------------------------------------------------
# REPL
# --------------------------------------------------------------------------

def repl(cfg, session, provider, first_prompt=None, tools=None):
    from . import input_ui

    input_ui.setup(HOME / "history", session["cwd"])
    if first_prompt is None:
        try:
            first_prompt = splash(cfg, session)
        except SystemExit:
            return
    if cfg.get("clear", True):
        clear_screen()
    tty = sys.stdout.isatty()
    if tty:
        boxed_banner(cfg, session)
    if session.get("messages"):
        print(dim("resumed — replaying the conversation below"))
        replay(session)
    pending = first_prompt or None
    while True:
        if pending:
            line = pending
            pending = None
            if tty:
                print()
                print(bold(accent("› ")) + line)
        else:
            if tty:
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
        if tty and line.endswith("\\"):
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

        # Fast path: plain prompts skip the whole command chain.
        if not line.startswith("/"):
            run_turn(cfg, session, _prepare(cfg, session, provider, line), provider, tools=tools)
            continue

        if line == "/q":
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
        if line in ("/login", "/signin"):
            from . import account

            try:
                account.login(cfg)
            except account.AccountError as exc:
                print(red(f"sign-in failed: {exc}"))
            continue
        if line == "/signup":
            from . import account

            print(f"  create an account at {account.base_url(cfg)}/signup")
            continue
        if line == "/logout":
            from . import account

            account.logout(cfg)
            print(dim("signed out on this device"))
            continue
        if line in ("/whoami", "/account"):
            from . import account

            try:
                profile = account.me(cfg)
            except account.AccountError as exc:
                print(red(str(exc)))
                continue
            print(f"  @{profile.get('username', '?')}  " + muted(account.base_url(cfg)))
            print(muted(f"  scopes: {profile.get('scope', '')}"))
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
            replay(session)
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
            from .update import UpdateError, perform_update

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
        if line == "/todo" or line.startsw