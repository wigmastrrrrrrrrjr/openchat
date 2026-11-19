"""Permission prompt for shell commands and paths outside the project."""

import os
import sys
from pathlib import Path

from . import interrupt
from .render import accent, bold, err, muted, ok, warn
from .store import save_session
from .tools import safe_path

PATH_TOOLS = {"read", "write", "edit", "ls"}


def needs_approval(name, args, cwd, trusted=()):
    if name == "bash":
        return True
    if name in PATH_TOOLS:
        try:
            target = Path(safe_path(cwd, args.get("path") or ".")).resolve()
        except Exception:
            return True
        for root in (cwd, *[t for t in trusted if t]):
            try:
                root_path = Path(root).resolve()
            except Exception:
                continue
            if target == root_path or target.is_relative_to(root_path):
                return False
        return True
    return False


def describe(name, args):
    if name == "bash":
        command = " ".join(str(args.get("command", "")).split())
        if len(command) > 160:
            command = command[:159] + "…"
        return "$ " + command
    return f"{name}  {args.get('path', '')}"


def _read_key():
    import termios
    import tty

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        while True:
            data = os.read(fd, 1)
            if not data:
                continue
            key = data.decode(errors="ignore").lower()
            if key in ("a", "y", "n", "\r", "\n", "\x1b", "\x03"):
                return key
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def _popup(name, args, cwd, outside):
    inner = 54
    title = f"approve {name}"
    lead = len("─ " + title + " ")
    print("  " + muted("╭") + muted("─ ") + bold(accent(title)) + muted(" ")
          + muted("─" * max(1, inner + 2 - lead)) + muted("╮"))
    action = describe(name, args)
    if len(action) > inner:
        action = action[: inner - 1] + "…"
    print("  " + muted("│ ") + warn(action) + " " * max(0, inner - len(action)) + muted(" │"))
    if outside:
        note = f"outside {cwd}"
        if len(note) > inner:
            note = note[: inner - 1] + "…"
        print("  " + muted("│ ") + muted(note) + " " * max(0, inner - len(note)) + muted(" │"))
    print("  " + muted("╰─ ") + accent("[a]") + muted(" always  ")
          + accent("[y]") + muted(" approve  ") + accent("[n]") + muted(" reject  ")
          + accent("›") + " ", end="", flush=True)


def ask(cfg, session, name, args):
    """Return True when the action may run."""
    if session.get("always_approve"):
        return True
    trusted = (session.get("tmp"), session.get("project"))
    if not needs_approval(name, args, session["cwd"], trusted):
        return True
    if not cfg.get("approvals", True):
        return True
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return True

    outside = name in PATH_TOOLS
    was_watching = interrupt.pause()
    try:
        _popup(name, args, session["cwd"], outside)
        key = _read_key()
    except (OSError, KeyboardInterrupt):
        key = "n"
    finally:
        interrupt.resume(was_watching)

    if key in ("y", "\r", "\n"):
        print(muted("approved"))
        return True
    if key == "a":
        print(muted("approved · always for this session"))
        session["always_approve"] = True
        save_session(session)
        return True
    print(err("rejected"))
    return False
