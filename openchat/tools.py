"""Tool definitions and local execution."""

import json
import os
import re
import subprocess
import time
from pathlib import Path

from .config import BASH_TIMEOUT, MAX_TOOL_OUTPUT
from .interrupt import check

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


def run_tool(cwd, name, args):
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
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(args.get("content", ""))
            return f"wrote {len(args.get('content', ''))} bytes to {args.get('path')}"

        if name == "edit":
            path = safe_path(cwd, args.get("path", ""))
            if not path.is_file():
                return f"error: not a file: {args.get('path')}"
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

