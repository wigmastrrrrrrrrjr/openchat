"""Session and scratch-directory persistence."""

import json
import shutil
import time
from pathlib import Path

from .config import (
    HOME,
    MAX_HISTORY_CHARS,
    PROJECTS_DIR,
    SESSIONS,
    TMP_DIR,
    TMP_TTL_DAYS,
)

def session_file(session_id):
    return SESSIONS / f"{session_id}.json"


def save_session(session):
    SESSIONS.mkdir(parents=True, exist_ok=True)
    session["updated"] = int(time.time())
    session_file(session["id"]).write_text(json.dumps(session, indent=1))


def load_session(session_id):
    path = session_file(session_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def project_dir(session_id):
    return PROJECTS_DIR / session_id


def tmp_dir(session_id=None):
    return TMP_DIR / session_id if session_id else TMP_DIR


def ensure_dirs(session):
    """Make sure the session's project and scratch folders exist."""
    try:
        project_dir(session["id"]).mkdir(parents=True, exist_ok=True)
        tmp_dir(session["id"]).mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    session.setdefault("project", str(project_dir(session["id"])))
    session.setdefault("tmp", str(tmp_dir(session["id"])))


def purge_tmp(max_age_days=TMP_TTL_DAYS):
    """Delete scratch entries older than max_age_days. Returns count removed."""
    if not TMP_DIR.exists():
        return 0
    cutoff = time.time() - max_age_days * 86400
    removed = 0
    for entry in TMP_DIR.iterdir():
        try:
            if entry.stat().st_mtime < cutoff:
                if entry.is_dir():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def new_session(cwd, model):
    base = time.strftime("%Y%m%d-%H%M%S")
    session_id = base
    n = 2
    while session_file(session_id).exists():
        session_id = f"{base}-{n}"
        n += 1
    session = {
        "id": session_id,
        "cwd": str(cwd),
        "model": model,
        "project": str(project_dir(session_id)),
        "tmp": str(tmp_dir(session_id)),
        "messages": [],
        "created": int(time.time()),
    }
    ensure_dirs(session)
    return session


def latest_session(cwd=None):
    if not SESSIONS.exists():
        return None
    files = sorted(SESSIONS.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not cwd:
        return files[0].stem if files else None
    target = str(Path(cwd).resolve())
    for path in files:
        try:
            data = json.loads(path.read_text())
        except Exception:
            continue
        if str(Path(data.get("cwd", "")).resolve()) == target:
            return path.stem
    return None


def session_title(session):
    title = (session.get("title") or "").strip()
    if title:
        return title
    for message in session.get("messages", []):
        if message.get("role") == "user" and isinstance(message.get("content"), str):
            first = message["content"].replace("\n", " ").strip()
            if first.startswith("[conversation summary"):
                continue
            return first[:60]
    return "(empty)"


def search_sessions(query, limit=12):
    """Yield (session, snippet) for text matches across stored sessions."""
    needle = query.lower().strip()
    if not needle:
        return []
    hits = []
    if not SESSIONS.exists():
        return hits
    files = sorted(SESSIONS.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in files:
        try:
            data = json.loads(path.read_text())
        except Exception:
            continue
        for message in data.get("messages", []):
            content = message.get("content")
            if not isinstance(content, str):
                continue
            index = content.lower().find(needle)
            if index < 0:
                continue
            start = max(0, index - 40)
            snippet = content[start:start + 120].replace("\n", " ").strip()
            hits.append((data, snippet))
            break
        if len(hits) >= limit:
            break
    return hits


def describe_session(session):
    last = ""
    for message in reversed(session.get("messages", [])):
        if message.get("role") == "user" and isinstance(message.get("content"), str):
            last = message["content"].replace("\n", " ")
            break
    updated = session.get("updated") or session.get("created") or 0
    ago = ""
    if updated:
        delta = max(0, int(time.time()) - int(updated))
        if delta < 3600:
            ago = f"{delta // 60}m ago"
        elif delta < 86400:
            ago = f"{delta // 3600}h ago"
        else:
            ago = f"{delta // 86400}d ago"
    title = session_title(session)
    return f"{session.get('id', '?')}  {ago:<7}  {title[:52]}  {session.get('cwd', '?')}"



def trim_history(messages):
    system = messages[0]
    rest = messages[1:]
    total = sum(len(json.dumps(m)) for m in rest)
    while total > MAX_HISTORY_CHARS and len(rest) > 3:
        removed = rest.pop(0)
        total -= len(json.dumps(removed))
    return [system] + rest

