"""Environment and connectivity checks for /doctor."""

import os
import shutil
import sys
import urllib.request
from pathlib import Path

from . import account
from .config import CONFIG_PATH, HOME, SESSIONS, version
from .render import accent, bold, err, muted, ok, warn


def _row(mark, label, detail=""):
    print("  " + mark + "  " + muted(f"{label:<12}") + detail)


def _check_accounts(cfg):
    url = account.base_url(cfg) + "/health"
    try:
        req = urllib.request.Request(url, headers={"user-agent": f"openchat/{version()}"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return True, f"HTTP {resp.status}  {url}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}  {url}"


def run(cfg, session):
    print("  " + bold(accent("doctor")))
    py = sys.version_info
    version_text = f"{py.major}.{py.minor}.{py.micro}"
    if py >= (3, 10):
        _row(ok("✓"), "python", version_text)
    else:
        _row(err("✗"), "python", version_text + " (need 3.10+)")

    if CONFIG_PATH.exists():
        mode = oct(CONFIG_PATH.stat().st_mode & 0o777)
        if CONFIG_PATH.stat().st_mode & 0o077:
            _row(warn("!"), "config", f"{CONFIG_PATH}  mode {mode} (should be 0o600)")
        else:
            _row(ok("✓"), "config", f"{CONFIG_PATH}  mode {mode}")
    else:
        _row(err("✗"), "config", f"missing: {CONFIG_PATH}")

    healthy, detail = _check_accounts(cfg)
    _row(ok("✓") if healthy else err("✗"), "accounts", detail)
    data = account.load()
    if not data:
        _row(muted("-"), "account", "not signed in — run: openchat login")
    else:
        try:
            profile = account.me(cfg)
            _row(ok("✓"), "account", f"@{profile.get('username', '?')}  token valid")
        except account.AccountError as exc:
            _row(warn("!"), "account", f"@{data.get('username', '?')}  {exc} — run: openchat login")

    git = shutil.which("git")
    _row(ok("✓") if git else muted("-"), "git", git or "not installed")

    cwd = Path(session.get("cwd", "."))
    writable = os.access(cwd, os.W_OK)
    _row(ok("✓") if writable else err("✗"), "cwd", f"{cwd}  {'writable' if writable else 'read-only'}")
    sessions_ok = os.access(SESSIONS if SESSIONS.exists() else HOME, os.W_OK)
    _row(ok("✓") if sessions_ok else err("✗"), "sessions", str(SESSIONS))
    _row(ok("✓"), "update", f"openchat {version()}  ·  {cfg.get('theme', 'cyan')} theme")
