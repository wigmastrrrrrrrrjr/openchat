"""First-run configuration so the CLI works on any device."""

import json
import os
import sys
from getpass import getpass

from .config import CONFIG_PATH, DEFAULT_CONFIG, HOME, save_config

SAVED_KEYS = tuple(DEFAULT_CONFIG)


def _is_configured(cfg):
    return bool(cfg.get("worker_token") or (cfg.get("base") and cfg.get("api_key")))


def _instructions():
    print(
        "openchat is not configured yet.\n"
        "\n"
        "Set one of these and run again:\n"
        "  OPENCHAT_TOKEN=...                       use the hosted worker\n"
        "  OPENCHAT_BASE=... OPENCHAT_API_KEY=...   use your own endpoint\n"
        "\n"
        "Or run openchat in a terminal to answer two quick questions.",
        file=sys.stderr,
    )


def ensure_configured(cfg, interactive=True):
    """Return True when a provider is usable, prompting on first run."""
    if _is_configured(cfg):
        return True

    if not (interactive and sys.stdin.isatty() and sys.stdout.isatty()):
        _instructions()
        return False

    print()
    print("  First run — how should openchat reach a model?")
    print()
    print("    1) hosted worker      needs a token from the maintainer")
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
            token = getpass("  token: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        if not token:
            print("  no token entered", file=sys.stderr)
            return False
        cfg["worker_token"] = token
        cfg["provider"] = "worker"

    HOME.mkdir(parents=True, exist_ok=True)
    save_config(cfg)
    try:
        CONFIG_PATH.chmod(0o600)
    except OSError:
        pass
    print(f"  saved to {CONFIG_PATH}")
    print()
    return True
