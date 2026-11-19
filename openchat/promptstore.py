"""Reusable prompt snippets stored under ~/.openchat/prompts."""

from pathlib import Path


def folder(home):
    return Path(home) / "prompts"


def _path(home, name):
    safe = "".join(ch for ch in name if ch.isalnum() or ch in "-_.").strip(".")
    if not safe:
        return None
    return folder(home) / f"{safe}.md"


def save(home, name, text):
    path = _path(home, name)
    if not path:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.strip() + "\n")
    return path


def load(home, name):
    path = _path(home, name)
    if not path or not path.is_file():
        return None
    try:
        return path.read_text().strip()
    except OSError:
        return None


def listing(home):
    directory = folder(home)
    if not directory.is_dir():
        return []
    return sorted(p.stem for p in directory.glob("*.md"))


def remove(home, name):
    path = _path(home, name)
    if path and path.exists():
        try:
            path.unlink()
            return True
        except OSError:
            return False
    return False
