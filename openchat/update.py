"""Self-update: download openchat.zip, verify it, reinstall, relink the command."""

import importlib.util
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from .config import HOME, install_dir, version

DEFAULT_UPDATE_URL = (
    "https://github.com/wigmastrrrrrrrrjr/openchat/releases/latest/download/openchat.zip"
)


class UpdateError(Exception):
    pass


def _download(url, dest):
    request = urllib.request.Request(
        url,
        headers={
            "user-agent": f"openchat/{version()}",
            "cache-control": "no-cache",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as resp:
            dest.write_bytes(resp.read())
    except Exception as exc:
        raise UpdateError(f"download failed: {exc}") from None


def _extract(archive, dest):
    try:
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(dest)
    except Exception as exc:
        raise UpdateError(f"unzip failed: {exc}") from None


def _module_name(requirement):
    name = requirement.split("#", 1)[0].strip()
    for sep in (">=", "<=", "==", "~=", "!=", ">", "<", "[", ";", " "):
        name = name.split(sep, 1)[0]
    return name.strip().replace("-", "_")


def _missing_libraries(root):
    path = Path(root) / "libraries.txt"
    if not path.exists():
        return []
    missing = []
    for raw in path.read_text().splitlines():
        requirement = raw.split("#", 1)[0].strip()
        if not requirement:
            continue
        try:
            found = importlib.util.find_spec(_module_name(requirement)) is not None
        except Exception:
            found = False
        if not found:
            missing.append(requirement)
    return missing


def _pip_install(requirement):
    attempts = [
        [sys.executable, "-m", "pip", "install", "--quiet", requirement],
        [sys.executable, "-m", "pip", "install", "--quiet", "--user", requirement],
        [sys.executable, "-m", "pip", "install", "--quiet",
         "--break-system-packages", requirement],
    ]
    for command in attempts:
        if subprocess.run(command, capture_output=True).returncode == 0:
            return True
    return False


def _install_libraries(requirements):
    for requirement in requirements:
        if not _pip_install(requirement):
            raise UpdateError(f"could not install {requirement}")


def _command_path():
    override = os.environ.get("OPENCHAT_BIN_DIR")
    if override:
        return Path(override) / "openchat"
    for candidate in (Path("/usr/local/bin"), Path.home() / ".local" / "bin"):
        if candidate.is_dir() and os.access(candidate, os.W_OK):
            return candidate / "openchat"
    return Path.home() / ".local" / "bin" / "openchat"


def _write_command(app_dir):
    target = _command_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "#!/usr/bin/env sh\n"
        f'exec "${{OPENCHAT_PYTHON:-python3}}" "{Path(app_dir) / "main.py"}" "$@"\n'
    )
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return target


def _legacy_files(cfg):
    candidates = [HOME / "openchat.py", HOME / "openchat" / "openchat.py"]
    if cfg.get("legacy_entry"):
        candidates.append(Path(cfg["legacy_entry"]))
    return [p for p in candidates if p.is_file()]


def install_tree(staging, app_dir, cfg=None):
    """Replace app_dir with staging, install libs, relink the command."""
    staging = Path(staging)
    app_dir = Path(app_dir)
    if not (staging / "main.py").exists():
        raise UpdateError("archive has no main.py")
    if not (staging / "openchat" / "cli.py").exists():
        raise UpdateError("archive is missing the openchat package")
    app_dir.mkdir(parents=True, exist_ok=True)
    for child in app_dir.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    for item in staging.iterdir():
        if item.name == "openchat.zip":
            continue
        if item.is_dir():
            shutil.copytree(item, app_dir / item.name)
        else:
            shutil.copy2(item, app_dir / item.name)
    _install_libraries(_missing_libraries(app_dir))
    command = _write_command(app_dir)
    for legacy in _legacy_files(cfg or {}):
        try:
            legacy.unlink()
        except OSError:
            pass
    return command


def perform_update(cfg):
    """Download the configured release and install it. Returns a summary dict."""
    url = cfg.get("update_url") or DEFAULT_UPDATE_URL
    app_dir = install_dir(cfg)
    previous = version()
    with tempfile.TemporaryDirectory(prefix="openchat-update-") as tmp:
        archive = Path(tmp) / "openchat.zip"
        _download(url, archive)
        staging = Path(tmp) / "staging"
        staging.mkdir()
        _extract(archive, staging)
        version_file = staging / "VERSION"
        new_version = version_file.read_text().strip() if version_file.exists() else "unknown"
        command = install_tree(staging, app_dir, cfg)
    return {
        "version": new_version,
        "previous": previous,
        "dir": str(app_dir),
        "command": str(command),
        "url": url,
    }
