"""Terminal rendering: theme, logo, splash, banner, markdown, tool lines."""

import html as html_lib
import json
import os
import re
import shutil
import sys
import threading
import time

ACCENT = "38;5;45"
ACCENT_ALT = "38;5;75"
MUTED = "38;5;244"
OK = "38;5;42"
WARN = "38;5;214"
ERR = "38;5;203"
CODE = "38;5;252"


def color(text, code):
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return text
    return f"\x1b[{code}m{text}\x1b[0m"


def accent(text):
    return color(text, ACCENT)


def accent_alt(text):
    return color(text, ACCENT_ALT)


def muted(text):
    return color(text, MUTED)


def ok(text):
    return color(text, OK)


def warn(text):
    return color(text, WARN)


def err(text):
    return color(text, ERR)


def dim(text):
    return color(text, "2")


def bold(text):
    return color(text, "1")


def red(text):
    return color(text, ERR)


def chip(text):
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return text
    return f"\x1b[{CODE};48;5;236m {text} \x1b[0m"


def short_path(value):
    value = str(value)
    home = os.path.expanduser("~")
    if home and value.startswith(home):
        value = "~" + value[len(home):]
    return value


class Spinner:
    """Tiny braille spinner shown while waiting for the first token."""

    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, label="thinking"):
        self.label = label
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if not sys.stdout.isatty():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        index = 0
        started = time.time()
        while not self._stop.is_set():
            elapsed = time.time() - started
            label = self.label
            if elapsed > 8:
                label = "still thinking"
            frame = self.FRAMES[index % len(self.FRAMES)]
            suffix = f" {int(elapsed)}s" if elapsed >= 3 else ""
            prefix = "" if index == 0 else "\r\x1b[2K"
            sys.stdout.write(f"{prefix}  {accent(frame)} {muted(label + '…' + suffix)}")
            sys.stdout.flush()
            index += 1
            self._stop.wait(0.09)

    def stop(self):
        if not self._thread:
            return
        self._stop.set()
        self._thread.join(timeout=1)
        self._thread = None
        sys.stdout.write("\r\x1b[2K")
        sys.stdout.flush()


class ThinkingPrinter:
    """Streams the model's reasoning in dim text, capped so it can't flood."""

    LIMIT = 4000

    def __init__(self, enabled):
        self.enabled = enabled
        self.shown = 0
        self.open = False

    def feed(self, text):
        if not self.enabled or self.shown >= self.LIMIT:
            return
        if not self.open:
            sys.stdout.write(dim("  ┆ ") + accent("thinking") + "\n")
            self.open = True
        chunk = text
        room = self.LIMIT - self.shown
        if len(chunk) > room:
            chunk = chunk[:room] + " …"
        self.shown += len(chunk)
        sys.stdout.write(dim(chunk))
        sys.stdout.flush()

    def close(self):
        if not self.open:
            return
        if self.shown >= self.LIMIT:
            sys.stdout.write(dim("\n  ┆ (reasoning truncated)"))
        sys.stdout.write("\n")
        sys.stdout.flush()
        self.open = False


class MarkdownPrinter:
    """Line-buffered markdown + inline HTML renderer for streamed replies."""

    def __init__(self, enabled=True):
        self.enabled = enabled
        self.buf = ""
        self.fence = None
        self.started = False

    def feed(self, text):
        self.buf += text
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            self.emit(line)

    def flush(self):
        if self.buf:
            self.emit(self.buf)
            self.buf = ""

    def emit(self, line):
        if not self.enabled:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
            return
        stripped = line.strip()
        if self.fence:
            if stripped == self.fence or stripped.startswith(self.fence):
                self.fence = None
                sys.stdout.write(muted("  ╰─") + "\n")
            else:
                sys.stdout.write(muted("  │ ") + color(line, CODE) + "\n")
            return
        fence = re.match(r"^(```|~~~)\s*([\w+-]*)\s*$", stripped)
        if fence:
            self.fence = fence.group(1)
            lang = fence.group(2)
            sys.stdout.write(muted("  ╭─ ") + accent_alt(lang or "code") + "\n")
            return
        if not stripped:
            sys.stdout.write("\n")
            return
        if re.match(r"^(-{3,}|\*{3,}|_{3,})$", stripped):
            cols = min(shutil.get_terminal_size((80, 24)).columns - 4, 52)
            sys.stdout.write(muted("  " + "─" * cols) + "\n")
            return
        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            text = inline_md(heading.group(2))
            level = len(heading.group(1))
            if level == 1:
                sys.stdout.write(bold(accent(text)) + "\n")
            else:
                sys.stdout.write(bold(text) + "\n")
            return
        if stripped.startswith(">"):
            sys.stdout.write(accent("  ▏ ") + muted(inline_md(stripped.lstrip("> ").strip())) + "\n")
            return
        item = re.match(r"^([-*+])\s+(.*)$", line)
        if item:
            indent = line[: len(line) - len(line.lstrip())]
            sys.stdout.write(indent + accent("• ") + inline_md(item.group(2)) + "\n")
            return
        sys.stdout.write(inline_md(line) + "\n")


def strip_html(text):
    text = re.sub(r"<((?:https?|mailto):[^>\s]+)>", r"\1", text)
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|li|tr|h[1-6]|pre|blockquote)>", "\n", text)
    text = re.sub(r"(?s)</?[a-zA-Z][^>]*>", "", text)
    return html_lib.unescape(text)


def inline_md(text):
    parts = re.split(r"(`[^`\n]+`)", text)
    out = []
    for part in parts:
        if part.startswith("`") and part.endswith("`") and len(part) > 2:
            out.append(chip(part[1:-1]))
            continue
        s = strip_html(part)
        s = re.sub(r"\*\*(.+?)\*\*", lambda m: bold(m.group(1)), s)
        s = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", lambda m: color(m.group(1), "3"), s)
        s = re.sub(
            r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
            lambda m: accent(m.group(1)) + " " + muted(m.group(2)),
            s,
        )
        out.append(s)
    return "".join(out)


TOOL_GLYPH = {
    "read": "◧",
    "write": "✎",
    "edit": "✎",
    "bash": "$",
    "ls": "▤",
    "glob": "⌕",
    "grep": "⌕",
}


def tool_label(name, args):
    label = args.get("path") or args.get("command") or args.get("pattern") or ""
    if name == "bash":
        label = label.splitlines()[0] if label else ""
    if len(label) > 96:
        label = label[:95] + "…"
    return label


def print_tool_call(name, args):
    glyph = TOOL_GLYPH.get(name, "●")
    label = tool_label(name, args)
    line = "  " + accent(glyph) + "  " + bold(name)
    if label:
        line += "  " + muted(short_path(label))
    print(line.rstrip())


def print_diff(args):
    old = args.get("old", "")
    new = args.get("new", "")
    for line in old.splitlines()[:10]:
        print("     " + muted("│ ") + err("- " + line))
    for line in new.splitlines()[:10]:
        print("     " + muted("│ ") + ok("+ " + line))
    if len(old.splitlines()) > 10 or len(new.splitlines()) > 10:
        print(muted("     │ …"))


def tool_summary(name, result):
    text = result if isinstance(result, str) else str(result)
    first = text.strip().splitlines()
    if name == "bash":
        head = first[0] if first else ""
        extra = len(first) - 1
        return head[:100] + (f" · {extra} more lines" if extra > 0 else "")
    if name in ("read", "grep", "glob", "ls"):
        count = len([line for line in first if line.strip()])
        unit = "lines" if name == "read" else "results"
        return f"{count} {unit}"
    if name == "write":
        return first[0][:100] if first else "written"
    return first[0][:100] if first else ""


def print_tool_result(name, args, result):
    if name == "edit":
        print_diff(args)
        return
    summary = tool_summary(name, result)
    if summary:
        print(muted("     └─ ") + muted(summary))


def context_estimate(messages):
    total = sum(len(json.dumps(m)) for m in messages if m.get("role") != "system")
    return max(1, int(total / 4))


LOGO_FONT = {
    "o": ["████", "█  █", "█  █", "█  █", "████"],
    "p": ["████", "█  █", "████", "█   ", "█   "],
    "e": ["████", "█   ", "███ ", "█   ", "████"],
    "n": ["█  █", "██ █", "█ ██", "█  █", "█  █"],
    "c": ["████", "█   ", "█   ", "█   ", "████"],
    "h": ["█  █", "█  █", "████", "█  █", "█  █"],
    "a": [" ██ ", "█  █", "████", "█  █", "█  █"],
    "t": ["███ ", " █  ", " █  ", " █  ", " █  "],
}

LOGO_GRADIENT = ["1;38;5;51", "1;38;5;45", "1;38;5;45", "1;38;5;39", "1;38;5;33"]


def clear_screen():
    if not sys.stdout.isatty():
        return
    sys.stdout.write("\x1b[2J\x1b[3J\x1b[H")
    sys.stdout.flush()


def render_logo():
    rows = [""] * 5
    for letter in "openchat":
        glyph = LOGO_FONT[letter]
        for i in range(5):
            rows[i] += glyph[i] + "  "
    return "\n".join(row.rstrip() for row in rows)


def splash(cfg, session):
    """Full-screen start card with a single input. Returns what was typed."""
    if not (sys.stdout.isatty() and sys.stdin.isatty()):
        return ""
    if os.environ.get("OPENCHAT_SKIP_SPLASH"):
        return ""
    from .config import version

    if cfg.get("clear", True):
        clear_screen()
    logo = render_logo()
    width = max(len(line) for line in logo.splitlines())
    cols = shutil.get_terminal_size((80, 24)).columns
    pad = " " * max(0, (min(cols, 80) - width) // 2)
    print()
    for line, code in zip(logo.splitlines(), LOGO_GRADIENT):
        print(pad + color(line, code))
    print()
    print(pad + bold(accent("openchat")) + muted(" " + version())
          + muted("  ·  ") + bold(cfg.get("model", "?")))
    print(pad + muted(short_path(session.get("cwd", "?"))))
    print()
    print(pad + accent("▸ ") + "ask anything, or describe what to build")
    print(pad + muted("─" * min(width, 40)))
    print(pad + muted("esc esc interrupts  ·  /help for commands"))
    try:
        return input("\n" + pad + bold(accent("› "))).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise SystemExit(0)


def boxed_banner(cfg, session):
    from .config import version

    inner = 50
    label_w = 8
    title = f"openchat {version()}"
    lead = len("─ " + title + " ")
    top = "  " + muted("╭") + muted("─ ") + bold(accent(title)) + muted(" ")
    top += muted("─" * max(1, inner + 2 - lead)) + muted("╮")
    print()
    print(top)
    rows = [
        ("model", bold(accent(cfg.get("model", "?")))),
        ("cwd", short_path(session.get("cwd", "?"))),
        ("scratch", muted(short_path(session.get("tmp", "?")))),
        (
            "session",
            session.get("id", "?")
            + muted(f"  ·  {len(session.get('messages', []))} messages"),
        ),
    ]
    for key, styled in rows:
        plain = re.sub(r"\x1b\[[0-9;]*m", "", styled)
        plain = f"{key:<{label_w}}  {plain}"
        if len(plain) > inner:
            plain = plain[: inner - 1] + "…"
            styled = plain
        pad = " " * max(0, inner - len(plain))
        print("  " + muted("│ ") + muted(f"{key:<{label_w}}") + "  " + styled + pad + muted(" │"))
    print("  " + muted("╰" + "─" * (inner + 2) + "╯"))
    print(muted("  type /help for commands, /exit to quit · esc esc interrupts"))


def print_footer(provider=None, model=None, elapsed=None, tools=0, ctx=None):
    parts = []
    if provider:
        parts.append(accent(f"{provider}/{model or '?'}"))
    if elapsed is not None:
        parts.append(bold(f"{elapsed}s"))
    if tools:
        parts.append(f"{tools} tool{'s' if tools != 1 else ''}")
    if ctx:
        parts.append(f"≈ {ctx:,} tok")
    if not parts:
        return
    print("  " + muted("╰─ ") + muted(" · ").join(parts))


def print_help():
    rows = [
        ("/new", "start a fresh session"),
        ("/sessions", "list recent sessions"),
        ("/resume id", "switch to a saved session"),
        ("/model [m]", "show or change the model"),
        ("/cwd", "show the working directory"),
        ("/paths", "show cwd, project and scratch folders"),
        ("/think", "toggle showing the model's reasoning"),
        ("/update", "reinstall the latest release"),
        ("/exit", "quit"),
    ]
    print("  " + bold(accent("commands")))
    for command, description in rows:
        print("    " + accent(f"{command:<12}") + muted(description))
    print("    " + accent(f"{'esc esc':<12}") + muted("interrupt the current response"))
    print()
    print("  " + muted("serve mode  ") + accent("openchat serve --port 8787"))


def render_history(session):
    """Replay every stored message so a resumed session looks continuous."""
    messages = session.get("messages", [])
    if not messages:
        return
    names = {}
    for message in messages:
        if message.get("role") == "system":
            continue
        if message.get("role") == "user":
            print()
            body = str(message.get("content", ""))
            lines = body.splitlines() or [""]
            print(bold(accent("› ")) + lines[0])
            for line in lines[1:]:
                print("  " + line)
        elif message.get("role") == "assistant":
            for call in message.get("tool_calls") or []:
                fn = call.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except Exception:
                    args = {}
                names[call.get("id")] = (fn.get("name", "?"), args)
            content = message.get("content") or ""
            if content:
                printer = MarkdownPrinter(enabled=sys.stdout.isatty())
                printer.feed(content)
                printer.flush()
        elif message.get("role") == "tool":
            name, args = names.get(message.get("tool_call_id"), ("tool", {}))
            glyph = TOOL_GLYPH.get(name, "●")
            label = tool_label(name, args)
            line = "  " + dim(glyph) + "  " + bold(name)
            if label:
                line += "  " + dim(short_path(label))
            print(line.rstrip())
            if name == "edit":
                print_diff(args)
            else:
                summary = tool_summary(name, message.get("content", ""))
                if summary:
                    print(dim("     └─ ") + dim(summary))
