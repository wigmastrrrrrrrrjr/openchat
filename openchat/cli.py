"""Command line entrypoint and interactive REPL."""

import argparse
import os
import sys
from pathlib import Path

from .agent import agent_run
from .config import SESSIONS, TMP_TTL_DAYS, install_dir, load_config
from .render import (
    MarkdownPrinter,
    bold,
    boxed_banner,
    clear_screen,
    color,
    context_estimate,
    dim,
    print_tool_call,
    print_tool_result,
    red,
    render_history,
    splash,
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
)
from .update import UpdateError, perform_update

def run_turn(cfg, session, prompt, provider):
    tty = sys.stdout.isatty()
    printer = MarkdownPrinter(enabled=tty and not cfg.get("raw"))
    thinking = tty and not cfg.get("raw") and bool(cfg.get("thinking", True))
    tool_count = 0

    def on_tool(name, args):
        nonlocal tool_count
        tool_count += 1
        print_tool_call(name, args)

    def on_tool_result(name, args, result):
        print_tool_result(name, args, result)

    ensure_dirs(session)
    result = agent_run(
        cfg,
        session["cwd"],
        session["messages"],
        prompt,
        provider,
        printer,
        on_tool=on_tool,
        on_tool_result=on_tool_result,
        project=session.get("project"),
        tmp=session.get("tmp"),
        thinking=thinking,
    )
    session["messages"] = result["messages"]
    if result.get("model"):
        session["model"] = result["model"]
    if result.get("error"):
        print(red(f"error: {result['error']}"))
        session["messages"].append(
            {"role": "assistant", "content": f"[request failed: {result['error']}]"}
        )
    elif tty:
        meta = []
        if result.get("provider"):
            meta.append(f"{result['provider']}/{result.get('model') or '?'}")
        if result.get("elapsed") is not None:
            meta.append(f"{result['elapsed']}s")
        if tool_count:
            meta.append(f"{tool_count} tool{'s' if tool_count != 1 else ''}")
        if result.get("messages"):
            meta.append(f"ctx {context_estimate(result['messages']):,} tok")
        if meta:
            print(dim("  ⋯ " + " · ".join(meta)))
    save_session(session)



def repl(cfg, session, provider, first_prompt=None):
    if first_prompt is None:
        try:
            first_prompt = splash(cfg, session)
        except SystemExit:
            return
    if cfg.get("clear", True):
        clear_screen()
    boxed_banner(cfg, session)
    if session.get("messages"):
        print(dim("resumed — replaying the conversation below"))
        render_history(session)
    print(dim("type /help for commands, /exit to quit"))
    pending = first_prompt or None
    while True:
        if pending:
            line = pending
            pending = None
            print()
            print(bold(color("› ", "36")) + line)
        else:
            try:
                line = input(bold("\n› ")).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
        if not line:
            continue
        if line in ("/exit", "/quit"):
            return
        if line == "/help":
            print("  /new        start a fresh session")
            print("  /sessions   list recent sessions")
            print("  /resume id  switch to a saved session")
            print("  /model [m]  show or set the model")
            print("  /cwd        show the working directory")
            print("  /paths      show cwd, project and scratch folders")
            print("  /think      toggle showing the model's reasoning")
            print("  /update     reinstall the latest release")
            print("  /exit       quit")
            print(dim("  serve mode: openchat serve --port 8787"))
            continue
        if line == "/new":
            session = new_session(Path.cwd(), cfg["model"])
            save_session(session)
            print(dim(f"new session {session['id']} · fresh context"))
            continue
        if line == "/sessions":
            files = sorted(
                SESSIONS.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True
            )[:10]
            rows = []
            for path in files:
                data = load_session(path.stem)
                if data:
                    rows.append("  " + describe_session(data))
            print("\n".join(rows) or "  (none)")
            continue
        if line.startswith("/resume"):
            parts = line.split(maxsplit=1)
            if len(parts) < 2:
                print(dim("usage: /resume <session-id>"))
                continue
            data = load_session(parts[1].strip())
            if not data:
                print(dim("no such session"))
                continue
            data["cwd"] = session["cwd"]
            session = data
            print()
            boxed_banner(cfg, session)
            render_history(session)
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
        if line.startswith("/"):
            print(dim("unknown command — /help"))
            continue
        run_turn(cfg, session, line, provider)



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
    args = parser.parse_args()

    cfg = load_config()
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
    if args.raw:
        cfg["raw"] = True
    if args.think is not None:
        cfg["thinking"] = args.think
    if args.clear is not None:
        cfg["clear"] = args.clear
    if args.model:
        cfg["model"] = args.model

    provider = args.provider or cfg.get("provider", "auto")

    cwd = Path(args.cwd).resolve()
    if not cwd.is_dir():
        print(red(f"no such directory: {cwd}"), file=sys.stderr)
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
    purge_tmp()

    prompt = " ".join(args.prompt).strip()
    if prompt:
        run_turn(cfg, session, prompt, provider)
        return
    repl(cfg, session, provider)


