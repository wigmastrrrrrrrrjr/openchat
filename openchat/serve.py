"""Headless HTTP server (OpenAI-compatible + simple JSON API)."""

import argparse
import json
import os
import secrets
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .agent import agent_run
from .config import HOME, PROJECTS_DIR, TMP_DIR, load_config
from .render import bold, dim, red
from .store import project_dir, purge_tmp, tmp_dir
from .tools import TOOLS

SERVE_SESSIONS = {}
SERVE_STORE = HOME / "serve-sessions.json"


def load_serve_store():
    if not SERVE_STORE.exists():
        return {}
    try:
        data = json.loads(SERVE_STORE.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_serve_store(store):
    try:
        HOME.mkdir(parents=True, exist_ok=True)
        tmp = SERVE_STORE.with_suffix(".tmp")
        tmp.write_text(json.dumps(store))
        tmp.replace(SERVE_STORE)
    except Exception:
        pass


def sse_event(payload):
    return f"data: {json.dumps(payload)}\n\n"


class SsePrinter:
    def __init__(self, write, model):
        self.write = write
        self.model = model
        self.cid = "chatcmpl-" + uuid.uuid4().hex[:24]

    def feed(self, text):
        self.write(
            sse_event(
                {
                    "id": self.cid,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": self.model,
                    "choices": [
                        {"index": 0, "delta": {"content": text}, "finish_reason": None}
                    ],
                }
            ).encode()
        )

    def flush(self):
        pass


class ServeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write(dim(f"{self.address_string()} {fmt % args}") + "\n")

    def cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header(
            "Access-Control-Allow-Headers", "authorization, content-type, x-session-id"
        )
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def json_out(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.cors()
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        token = getattr(self.server, "token", "")
        if not token:
            return True
        header = self.headers.get("authorization", "")
        return header == f"Bearer {token}"

    def do_OPTIONS(self):
        self.send_response(204)
        self.cors()
        self.send_header("content-length", "0")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            self.json_out(
                200,
                {
                    "ok": True,
                    "cwd": self.server.cwd,
                    "model": self.server.cfg.get("model"),
                    "tools": [t["function"]["name"] for t in TOOLS],
                },
            )
            return
        if not self.authorized():
            self.json_out(401, {"error": "unauthorized"})
            return
        if path == "/api/sessions":
            self.json_out(
                200,
                {
                    "sessions": [
                        {
                            "id": sid,
                            "updated": entry.get("updated", 0),
                            "messages": len(entry.get("messages", [])),
                        }
                        for sid, entry in sorted(
                            SERVE_SESSIONS.items(),
                            key=lambda kv: kv[1].get("updated", 0),
                            reverse=True,
                        )
                    ]
                },
            )
            return
        if path == "/v1/models":
            ids = [self.server.cfg.get("model")] + list(self.server.cfg.get("models", []))
            seen = []
            for model in ids:
                if model and model not in seen:
                    seen.append(model)
            self.json_out(
                200,
                {
                    "object": "list",
                    "data": [
                        {"id": m, "object": "model", "owned_by": "openchat"} for m in seen
                    ],
                },
            )
            return
        self.json_out(404, {"error": "not found"})

    def do_POST(self):
        if not self.authorized():
            self.json_out(401, {"error": "unauthorized"})
            return
        path = urlparse(self.path).path
        try:
            length = int(self.headers.get("content-length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            body = json.loads(raw or b"{}")
        except Exception:
            self.json_out(400, {"error": "invalid json"})
            return

        if path == "/api/chat":
            self.handle_simple(body)
            return
        if path in ("/v1/chat/completions", "/chat/completions"):
            self.handle_openai(body)
            return
        self.json_out(404, {"error": "not found"})

    def make_cfg(self, model):
        cfg = dict(self.server.cfg)
        if model:
            cfg["model"] = model
        return cfg

    def handle_simple(self, body):
        prompt = body.get("message") or ""
        if not prompt and isinstance(body.get("messages"), list):
            prompt = ""
        session_id = body.get("session_id") or "default"
        entry = SERVE_SESSIONS.get(session_id) or {}
        history = entry.get("messages", [])
        if body.get("messages"):
            history = body["messages"][:-1] if body["messages"][-1].get("role") == "user" else body["messages"]
            prompt = body["messages"][-1].get("content", "") if body["messages"][-1].get("role") == "user" else prompt
        if not prompt:
            self.json_out(400, {"error": "message required"})
            return
        tools_used = []
        dirs = {
            "project": str(project_dir(session_id)),
            "tmp": str(tmp_dir(session_id)),
        }
        try:
            project_dir(session_id).mkdir(parents=True, exist_ok=True)
            tmp_dir(session_id).mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        result = agent_run(
            self.make_cfg(body.get("model")),
            body.get("cwd") or self.server.cwd,
            history,
            prompt,
            "auto",
            on_tool=lambda name, args: tools_used.append({"name": name, "args": args}),
            project=dirs["project"],
            tmp=dirs["tmp"],
        )
        if result.get("error"):
            self.json_out(502, {"error": result["error"], "session_id": session_id})
            return
        SERVE_SESSIONS[session_id] = {
            "messages": result["messages"],
            "updated": int(time.time()),
        }
        while len(SERVE_SESSIONS) > 200:
            SERVE_SESSIONS.pop(next(iter(SERVE_SESSIONS)))
        save_serve_store(SERVE_SESSIONS)
        self.json_out(
            200,
            {
                "content": result["content"],
                "session_id": session_id,
                "provider": result["provider"],
                "model": result["model"],
                "tool_calls": tools_used,
            },
        )

    def handle_openai(self, body):
        messages = body.get("messages") or []
        if not isinstance(messages, list) or not messages:
            self.json_out(400, {"error": "messages required"})
            return
        model = body.get("model") or self.server.cfg.get("model")
        cfg = self.make_cfg(model)
        stream = bool(body.get("stream"))

        if not stream:
            result = agent_run(cfg, self.server.cwd, messages, None, "auto")
            if result.get("error"):
                self.json_out(502, {"error": result["error"]})
                return
            self.json_out(
                200,
                {
                    "id": "chatcmpl-" + uuid.uuid4().hex[:24],
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": result["model"] or model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": result["content"],
                            },
                            "finish_reason": "stop",
                        }
                    ],
                },
            )
            return

        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("connection", "close")
        self.cors()
        self.end_headers()
        self.close_connection = True
        printer = SsePrinter(self.wfile.write, model)

        def run():
            result = agent_run(cfg, self.server.cwd, messages, None, "auto", printer)
            if result.get("error"):
                self.wfile.write(
                    sse_event({"error": {"message": result["error"], "type": "upstream"}}).encode()
                )
            else:
                self.wfile.write(
                    sse_event(
                        {
                            "id": printer.cid,
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "model": result["model"] or model,
                            "choices": [
                                {"index": 0, "delta": {}, "finish_reason": "stop"}
                            ],
                        }
                    ).encode()
                )
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

        try:
            run()
        except (BrokenPipeError, ConnectionResetError):
            pass


def serve_main(argv):
    parser = argparse.ArgumentParser(prog="openchat serve")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--token", default=os.environ.get("OPENCHAT_SERVE_TOKEN", ""))
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("-m", "--model")
    args = parser.parse_args(argv)

    cfg = load_config()
    if args.model:
        cfg["model"] = args.model

    from .setup import ensure_configured

    if not ensure_configured(cfg, interactive=False):
        sys.exit(1)
    cwd_path = Path(args.cwd).expanduser().resolve()
    if not cwd_path.exists():
        try:
            cwd_path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(red(f"cannot create {cwd_path}: {exc}"), file=sys.stderr)
            sys.exit(1)
    elif not cwd_path.is_dir():
        print(red(f"not a directory: {cwd_path}"), file=sys.stderr)
        sys.exit(1)
    cwd = str(cwd_path)
    token = args.token
    loopback = args.host in ("127.0.0.1", "localhost", "::1")
    if not loopback and not token:
        token = secrets.token_urlsafe(24)
        print(dim("no --token given for a public bind, generated one:") + " " + bold(token))

    SERVE_SESSIONS.clear()
    SERVE_SESSIONS.update(load_serve_store())
    try:
        PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        TMP_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    purge_tmp()

    server = ThreadingHTTPServer((args.host, args.port), ServeHandler)
    server.cfg = cfg
    server.cwd = cwd
    server.token = token

    print(dim(f"openchat serve · http://{args.host}:{args.port}"), flush=True)
    print(dim(f"cwd {cwd} · model {cfg['model']} · tools {len(TOOLS)}"), flush=True)
    print(dim(f"tmp {TMP_DIR} · projects {PROJECTS_DIR}"), flush=True)
    print(dim("endpoints: POST /v1/chat/completions · POST /api/chat · GET /api/sessions · GET /v1/models · GET /health"), flush=True)
    if token:
        print(dim("auth: authorization: Bearer <token>"), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print(dim("\nbye"))

