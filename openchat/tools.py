"""Tool definitions and local execution."""

import json
import os
import re
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

from .config import BASH_TIMEOUT, MAX_TOOL_OUTPUT, version
from .interrupt import check
from .journal import record
from .render import strip_html

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read",
            "description": "Read a text file, optionally a line range (1-indexed).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start": {"type": "integer"},
                    "end": {"type": "integer"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write",
            "description": "Create or overwrite a file with full contents.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit",
            "description": "Replace exactly one occurrence of old with new in a file. Fails if old is missing or ambiguous.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old": {"type": "string"},
                    "new": {"type": "string"},
                },
                "required": ["path", "old", "new"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a shell command in the project directory.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ls",
            "description": "List a directory.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob",
            "description": "Find files by glob pattern, e.g. 'src/**/*.ts'.",
            "parameters": {
                "type": "object",
                "properties": {"pattern": {"type": "string"}},
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search file contents with a regular expression.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "include": {"type": "string", "description": "Glob filter, e.g. '*.py'"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "multi_edit",
            "description": "Apply several exact old→new replacements to one file atomically. Each old must be unique.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "edits": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "old": {"type": "string"},
                                "new": {"type": "string"},
                            },
                            "required": ["old", "new"],
                        },
                    },
                },
                "required": ["path", "edits"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mkdir",
            "description": "Create a directory (parents included).",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move",
            "description": "Move or rename a file or directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "to": {"type": "string"},
                },
                "required": ["path", "to"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete",
            "description": "Delete a file or directory. Always requires approval.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tree",
            "description": "Show a compact directory tree (depth 1-4).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "depth": {"type": "integer"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch",
            "description": "Fetch a URL over http(s) and return readable text. Requires approval.",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        },
    },
]


def clip(text, limit=MAX_TOOL_OUTPUT):
    text = text if isinstance(text, str) else str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[truncated {len(text) - limit} chars]"


def safe_path(cwd, raw):
    path = Path(raw)
    if not path.is_absolute():
        path = Path(cwd) / path
    return path


def run_tool(cwd, name, args, journal=None):
    try:
        if name == "read":
            path = safe_path(cwd, args.get("path", ""))
            if not path.is_file():
                return f"error: not a file: {args.get('path')}"
            lines = path.read_text(errors="replace").splitlines()
            start = int(args.get("start") or 1)
            end = int(args.get("end") or len(lines))
            start = max(1, start)
            end = min(len(lines), end)
            body = "\n".join(
                f"{i}\t{lines[i - 1]}" for i in range(start, end + 1)
            )
            return clip(body) or "(empty file)"

        if name == "write":
            path = safe_path(cwd, args.get("path", ""))
            if journal:
                try:
                    existed = path.is_file()
                    record(journal, path, existed, path.read_bytes() if existed else None)
                except OSError:
                    pass
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(args.get("content", ""))
            return f"wrote {len(args.get('content', ''))} bytes to {args.get('path')}"

        if name == "edit":
            path = safe_path(cwd, args.get("path", ""))
            if not path.is_file():
                return f"error: not a file: {args.get('path')}"
            if journal:
                try:
                    record(journal, path, True, path.read_bytes())
                except OSError:
                    pass
            text = path.read_text()
            old = args.get("old", "")
            new = args.get("new", "")
            count = text.count(old)
            if count == 0:
                return "error: old text not found"
            if count > 1:
                return f"error: old text appears {count} times; make it unique"
            path.write_text(text.replace(old, new, 1))
            return f"edited {args.get('path')}"

        if name == "bash":
            command = args.get("command", "")
            proc = subprocess.Popen(
                command,
                shell=True,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            deadline = time.time() + BASH_TIMEOUT
            try:
                while proc.poll() is None:
                    check()
                    if time.time() > deadline:
                        proc.kill()
                        proc.wait()
                        return f"error: command timed out after {BASH_TIMEOUT}s"
                    time.sleep(0.05)
            except BaseException:
                proc.kill()
                proc.wait()
                raise
            out = proc.stdout.read() if proc.stdout else ""
            return clip(f"exit={proc.returncode}\n{out}" if proc.returncode else out or "(no output)")

        if name == "multi_edit":
            path = safe_path(cwd, args.get("path", ""))
            if not path.is_file():
                return f"error: not a file: {args.get('path')}"
            edits = args.get("edits") or []
            if not isinstance(edits, list) or not edits:
                return "error: edits must be a non-empty list"
            text = path.read_text()
            for number, edit in enumerate(edits, 1):
                old = str(edit.get("old", ""))
                new = str(edit.get("new", ""))
                if not old:
                    return f"error: edit {number}: old text is empty"
                count = text.count(old)
                if count == 0:
                    return f"error: edit {number}: old text not found (nothing written)"
                if count > 1:
                    return f"error: edit {number}: old text appears {count} times (nothing written)"
                text = text.replace(old, new, 1)
            if journal:
                try:
                    record(journal, path, True, path.read_bytes())
                except OSError:
                    pass
            path.write_text(text)
            return f"applied {len(edits)} edits to {args.get('path')}"

        if name == "mkdir":
            path = safe_path(cwd, args.get("path", ""))
            path.mkdir(parents=True, exist_ok=True)
            return f"created {args.get('path')}"

        if name == "move":
            source = safe_path(cwd, args.get("path", ""))
            target = safe_path(cwd, args.get("to", ""))
            if not source.exists():
                return f"error: no such path: {args.get('path')}"
            if journal:
                try:
                    if target.is_file():
                        record(journal, target, True, target.read_bytes())
                    else:
                        record(journal, target, False, None)
                    if source.is_file():
                        record(journal, source, True, source.read_bytes())
                except OSError:
                    pass
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(target))
            return f"moved {args.get('path')} → {args.get('to')}"

        if name == "delete":
            path = safe_path(cwd, args.get("path", ""))
            if not path.exists():
                return f"error: no such path: {args.get('path')}"
            if path.is_dir():
                files = [p for p in sorted(path.rglob("*")) if p.is_file()]
                if len(files) > 300:
                    return "error: directory has more than 300 files; delete it with bash after review"
                if journal:
                    for entry in files:
                        try:
                            record(journal, entry, True, entry.read_bytes())
                        except OSError:
                            continue
                shutil.rmtree(path)
                return f"deleted directory {args.get('path')} ({len(files)} files)"
            if journal:
                try:
                    record(journal, path, True, path.read_bytes())
                except OSError:
                    pass
            path.unlink()
            return f"deleted {args.get('path')}"

        if name == "tree":
            root = safe_path(cwd, args.get("path") or ".")
            if not root.is_dir():
                return f"error: not a directory: {args.get('path')}"
            depth = max(1, min(int(args.get("depth") or 2), 4))
            skip = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}
            lines = []

            def walk(folder, level):
                if level >= depth or len(lines) >= 300:
                    return
                try:
                    entries = sorted(folder.iterdir(), key=lambda p: (p.is_file(), p.name))
                except OSError:
                    return
                for entry in entries:
                    if entry.name in skip:
                        continue
                    lines.append("  " * level + entry.name + ("/" if entry.is_dir() else ""))
                    if entry.is_dir():
                        walk(entry, level + 1)

            walk(root, 0)
            return "\n".join(lines[:300]) or "(empty)"

        if name == "fetch":
            url = str(args.get("url", "")).strip()
            if not re.match(r"^https?://", url):
                return "error: only http(s) URLs are supported"
            request = urllib.request.Request(
                url, headers={"user-agent": f"openchat/{version()}"}
            )
            try:
                with urllib.request.urlopen(request, timeout=15) as response:
                    raw = response.read(1_000_000)
                    charset = response.headers.get_content_charset() or "utf-8"
                    body = raw.decode(charset, errors="replace")
                    kind = response.headers.get_content_type() or ""
                    if "html" in kind:
                        body = strip_html(body)
                    body = re.sub(r"\n{3,}", "\n\n", body).strip()
                    return clip(f"HTTP {response.status}  {url}\n\n{body}")
            except Exception as exc:
                return f"error: fetch failed: {exc}"

        if name == "ls":
            path = safe_path(cwd, args.get("path") or ".")
            if not path.is_dir():
                return f"error: not a directory: {args.get('path')}"
            entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name))
            return "\n".join(e.name + ("/" if e.is_dir() else "") for e in entries) or "(empty)"

        if name == "glob":
            pattern = args.get("pattern", "*")
            matches = sorted(
                str(p.relative_to(cwd))
                for p in Path(cwd).glob(pattern)
                if ".git/" not in str(p)
            )
            return "\n".join(matches[:400]) or "(no matches)"

        if name == "grep":
            pattern = args.get("pattern", "")
            include = args.get("include") or "*"
            try:
                rx = re.compile(pattern)
            except re.error as exc:
                return f"error: bad regex: {exc}"
            hits = []
            for path in Path(cwd).rglob(include):
                if not path.is_file() or ".git/" in str(path) or "node_modules/" in str(path):
                    continue
                try:
                    for num, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
                        if rx.search(line):
                            hits.append(f"{path.relative_to(cwd)}:{num}: {line.strip()[:200]}")
                except OSError:
                    continue
                if len(hits) >= 200:
                    break
            return "\n".join(hits) or "(no matches)"

        return f"error: unknown tool {name}"
    except subprocess.TimeoutExpired:
        return f"error: command timed out after {BASH_TIMEOUT}s"
    except Exception as exc:
        return f"error: {exc}"

