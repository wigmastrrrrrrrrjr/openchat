"""Paths, limits, and configuration loading."""

import json
import os
from pathlib import Path


HOME = Path(os.path.expanduser("~/.openchat"))
SESSIONS = HOME / "sessions"
CONFIG_PATH = HOME / "config.json"
TMP_DIR = Path(os.environ.get("OPENCHAT_TMP") or HOME / "tmp")
PROJECTS_DIR = Path(os.environ.get("OPENCHAT_PROJECTS") or HOME / "projects")
TMP_TTL_DAYS = 30

DEFAULT_CONFIG = {
    # No provider keys live in source. Either point at the hosted worker
    # (worker + worker_token) or set base/api_key locally / via env.
    "base": "",
    "api_key": "",
    "provider": "worker",
    "approvals": True,
    "theme": "cyan",
    "model": "glm-5.3-flash",
    "models": ["glm-5.3-flash", "deepseek-v4.1-flash"],
    "retries": 3,
    "thinking": True,
    "clear": True,
    "temperature": 0.3,
    "auto_compact": 0,
    "bell": True,
    "worker": "https://discord-ai-bot.csomeone301.workers.dev",
    "worker_token": "",
    "update_url": "https://github.com/wigmastrrrrrrrrjr/openchat/releases/latest/download/openchat.zip",
    "install_dir": "",
}

MAX_TOOL_OUTPUT = 30_000
MAX_HISTORY_CHARS = 140_000
BASH_TIMEOUT = 180


def version():
    try:
        return (Path(__file__).resolve().parent.parent / "VERSION").read_text().strip()
    except Exception:
        return "0.0.0"


def save_config(cfg):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    keep = {
        key: value
        for key, value in cfg.items()
        if key in DEFAULT_CONFIG and value != DEFAULT_CONFIG.get(key)
    }
    CONFIG_PATH.write_text(json.dumps(keep, indent=1) + "\n")
    try:
        CONFIG_PATH.chmod(0o600)
    except OSError:
        pass
    return CONFIG_PATH


def install_dir(cfg=None):
    """Where the installed app lives (main.py + this package)."""
    env = os.environ.get("OPENCHAT_APP_DIR")
    if env:
        return Path(env)
    if cfg and cfg.get("install_dir"):
        return Path(cfg["install_dir"])
    return HOME / "app"


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text()))
        except Exception:
            pass
    if os.environ.get("OPENCHAT_BASE"):
        cfg["base"] = os.environ["OPENCHAT_BASE"]
    if os.environ.get("OPENCHAT_API_KEY"):
        cfg["api_key"] = os.environ["OPENCHAT_API_KEY"]
    if os.environ.get("OPENCHAT_MODEL"):
        cfg["model"] = os.environ["OPENCHAT_MODEL"]
    if os.environ.get("OPENCHAT_TOKEN"):
        cfg["worker_token"] = os.environ["OPENCHAT_TOKEN"]
    return cfg

