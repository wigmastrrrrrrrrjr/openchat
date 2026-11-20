"""First-run configuration so the CLI works on any device."""

import sys
from getpass import getpass

from . import account
from .config import CONFIG_PATH, HOME, save_config


def _is_configured(cfg):
    return bool(account.signed_in() or (cfg.get("base") and cfg.get("api_key")))


def _instructions(cfg):
    print(
        "openchat needs an account before it can think.\n"
        "\n"
        "Sign in once with:\n"
        f"  openchat login                    sign in at {account.base_url(cfg)}\n"
        "\n"
        "Or bring your own OpenAI-compatible endpoint:\n"
        "  OPENCHAT_BASE=... OPENCHAT_API_KEY=... openchat\n"
        "\n"
        "Run openchat in a terminal to answer two quick questions.",
        file=sys.stderr,
    )


def ensure_configured(cfg, interactive=True):
    """Return True when a provider is usable, prompting on first run."""
    if _is_configured(cfg):
        return True

    if not (interactive and sys.stdin.isatty() and sys.stdout.isatty()):
        _instructions(cfg)
        return False

    print()
    print("  First run — openchat needs an account for thinking.")
    print()
    print("    1) sign in        create or use an account (recommended)")
    print("    2) my own endpoint    any OpenAI-compatible base URL + key")
    print()
    try:
        choice = input("  choice [1]: ").strip() or "1"
    except (EOFError, KeyboardInterrupt):
        print()
        return False

    if choice == "2":
        try:
            base = input("  base URL: ").strip()
            key = getpass("  api key: ").strip()
            model = input(f"  model [{cfg.get('model')}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        if not base or not key:
            print("  need both a base URL and a key", file=sys.stderr)
            return False
        cfg["base"] = base
        cfg["api_key"] = key
        cfg["provider"] = "novita"
        if model:
            cfg["model"] = model
            if model not in cfg.get("models", []):
                cfg["models"] = [model] + list(cfg.get("models", []))
    else:
        try:
            account.login(cfg)
        except account.AccountError as exc:
            print(f"  sign-in failed: {exc}", file=sys.stderr)
            print(f"  try again later with: openchat login", file=sys.stderr)
            return False

    HOME.mkdir(parents=True, exist_ok=True)
    save_config(cfg)
    try:
        CONFIG_PATH.chmod(0o600)
    except OSError:
        pass
    print(f"  saved to {CONFIG_PATH}")
    print()
    return True
