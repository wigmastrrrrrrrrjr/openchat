"""Undo journal: snapshots files before the tools change them."""

import base64
import json
import time
from pathlib import Path

MAX_ENTRIES = 80


def journal_dir(tmp):
    return Path(tmp) / "journal"


def record(tmp, path, existed, content):
    """Store the pre-change bytes for `path` under the session's tmp dir."""
    if not tmp:
        return
    folder = journal_dir(tmp)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        entry = {
            "path": str(path),
            "existed": bool(existed),
            "content": base64.b64encode(content).decode() if content is not None else None,
            "time": int(time.time()),
        }
        name = f"{time.time_ns()}.json"
        (folder / name).write_text(json.dumps(entry))
        files = sorted(folder.glob("*.json"))
        for old in files[:-MAX_ENTRIES]:
            try:
                old.unlink()
            except OSError:
                pass
    except OSError:
        return


def entries(tmp):
    folder = journal_dir(tmp)
    if not folder.is_dir():
        return []
    out = []
    for path in sorted(folder.glob("*.json")):
        try:
            out.append((path, json.loads(path.read_text())))
        except Exception:
            continue
    return out


def undo(tmp):
    """Revert the newest change. Returns {path, restored} or None."""
    items = entries(tmp)
    if not items:
        return None
    item, entry = items[-1]
    target = Path(entry.get("path", ""))
    if not str(target):
        return None
    raw = entry.get("content")
    content = base64.b64decode(raw) if raw is not None else b""
    if entry.get("existed"):
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        except OSError:
            return None
    else:
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            return None
    try:
        item.unlink()
    except OSError:
        pass
    return {"path": str(target), "restored": bool(entry.get("existed"))}


def changed(tmp):
    """{path: number-of-changes} for this session."""
    seen = {}
    for _, entry in entries(tmp):
        path = entry.get("path", "")
        seen[path] = seen.get(path, 0) + 1
    return seen
