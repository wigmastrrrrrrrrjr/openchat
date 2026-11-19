"""Whole-project snapshots so experiments are cheap to undo."""

import shutil
import time
import zipfile
from pathlib import Path

KEEP = 5
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".next", ".cache"}
MAX_FILE = 2 * 1024 * 1024


def snap_dir(tmp):
    return Path(tmp) / "snapshots"


def _files(root):
    for path in sorted(Path(root).rglob("*")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        try:
            if path.is_file() and path.stat().st_size <= MAX_FILE:
                yield path
        except OSError:
            continue


def create(tmp, cwd):
    folder = snap_dir(tmp)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = folder / f"snap-{stamp}.zip"
    count = 0
    root = Path(cwd)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in _files(root):
            try:
                zf.write(path, path.relative_to(root).as_posix())
                count += 1
            except OSError:
                continue
    for old in sorted(folder.glob("snap-*.zip"))[:-KEEP]:
        try:
            old.unlink()
        except OSError:
            pass
    size = target.stat().st_size if target.exists() else 0
    return {"path": str(target), "files": count, "size": size, "name": target.name}


def listing(tmp):
    folder = snap_dir(tmp)
    if not folder.is_dir():
        return []
    out = []
    for path in sorted(folder.glob("snap-*.zip")):
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        out.append({"path": str(path), "name": path.name, "size": size})
    return out


def restore(tmp, cwd, index=None):
    items = listing(tmp)
    if not items:
        return None
    item = items[-1] if index is None else items[max(0, min(index, len(items) - 1))]
    root = Path(cwd).resolve()
    restored = 0
    with zipfile.ZipFile(item["path"]) as zf:
        for member in zf.namelist():
            target = (root / member).resolve()
            if not target.is_relative_to(root):
                continue
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                restored += 1
            except OSError:
                continue
    return {"name": item["name"], "restored": restored}
