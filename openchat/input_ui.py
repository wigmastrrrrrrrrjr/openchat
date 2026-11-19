"""Readline: persistent history, tab completion, prompt wrapping."""

import atexit
import os
import readline
from pathlib import Path

COMMANDS = [
    "/new", "/sessions", "/resume", "/model", "/cwd", "/paths", "/compact",
    "/retry", "/undo", "/changes", "/export", "/theme", "/init", "/doctor",
    "/clear", "/think", "/update", "/help", "/exit",
]

_cwd = "."
_history_path = None
_enabled = False


def _names(directory):
    try:
        return sorted(os.listdir(directory))
    except OSError:
        return []


def _paths(text):
    if text.startswith("~"):
        directory, prefix = os.path.split(os.path.expanduser(text))
        home = os.path.expanduser("~")
        out = []
        for name in _names(directory):
            if not name.startswith(prefix):
                continue
            full = os.path.join(directory, name)
            display = "~/" + os.path.relpath(full, home)
            out.append(display + ("/" if os.path.isdir(full) else ""))
        return out
    if os.path.isabs(text):
        directory, prefix = os.path.split(text)
    else:
        directory, prefix = os.path.split(text)
        directory = os.path.join(_cwd, directory) if directory else _cwd
    out = []
    for name in _names(directory):
        if not name.startswith(prefix):
            continue
        full = os.path.join(directory, name)
        if os.path.isabs(text):
            display = full
        elif text.startswith("./"):
            display = os.path.join(".", os.path.relpath(full, _cwd))
        else:
            display = os.path.relpath(full, _cwd)
        out.append(display + ("/" if os.path.isdir(full) else ""))
    return out


def _complete(text, state):
    buffer = readline.get_line_buffer()
    if buffer.startswith("/") and " " not in buffer:
        options = [c for c in COMMANDS if c.startswith(text)]
    else:
        options = _paths(text)
    if state < len(options):
        return options[state]
    return None


def save():
    if not (_enabled and _history_path):
        return
    try:
        readline.write_history_file(str(_history_path))
        os.chmod(_history_path, 0o600)
    except OSError:
        pass


def setup(history_path, cwd):
    """Enable history + completion for interactive terminals."""
    global _cwd, _history_path, _enabled
    _cwd = str(cwd)
    if not (os.isatty(0) and os.isatty(1)):
        return False
    _history_path = Path(history_path)
    try:
        _history_path.parent.mkdir(parents=True, exist_ok=True)
        if _history_path.exists():
            readline.read_history_file(str(_history_path))
    except OSError:
        pass
    readline.set_history_length(1000)
    readline.set_completer(_complete)
    readline.set_completer_delims(" \t\n\"'`@$><=;|&{(")
    readline.parse_and_bind("tab: complete")
    atexit.register(save)
    _enabled = True
    return True


def set_cwd(cwd):
    global _cwd
    _cwd = str(cwd)


def prompt(text):
    """Wrap an ANSI prompt so readline measures it correctly."""
    return "\001" + text + "\002"
