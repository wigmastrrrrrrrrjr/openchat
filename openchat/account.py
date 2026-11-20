"""Sign in to the hosted openchat account service."""

import base64
import hashlib
import http.server
import json
import os
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

from .config import AUTH_URL, HOME, version

ACCOUNT_PATH = HOME / "account.json"
CLIENT_ID = "openchat-cli"
SCOPES = "profile:read chat"
CALLBACK_PATH = "/callback"
DEFAULT_PORT = 53682


class AccountError(Exception):
    pass


def base_url(cfg=None):
    value = os.environ.get("OPENCHAT_AUTH")
    if not value and cfg:
        value = cfg.get("auth")
    return str(value or AUTH_URL).rstrip("/")


def load():
    try:
        data = json.loads(ACCOUNT_PATH.read_text())
    except Exception:
        return None
    if not data.get("refresh_token"):
        return None
    return data


def save(data):
    HOME.mkdir(parents=True, exist_ok=True)
    ACCOUNT_PATH.write_text(json.dumps(data, indent=1) + "\n")
    try:
        ACCOUNT_PATH.chmod(0o600)
    except OSError:
        pass


def clear():
    try:
        ACCOUNT_PATH.unlink()
    except OSError:
        pass


def signed_in():
    return load() is not None


def username():
    data = load()
    return data.get("username", "") if data else ""


def _request(url, data=None, headers=None, timeout=60):
    body = None
    if data is not None:
        body = json.dumps(data).encode() if isinstance(data, dict) else data
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "user-agent": f"openchat/{version()}",
            "accept": "application/json",
            **(headers or {}),
        },
        method="POST" if body is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode(errors="replace")
            return response.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {"error": raw or f"HTTP {exc.code}"}
        return exc.code, payload
    except Exception as exc:
        raise AccountError(f"cannot reach {url}: {exc}") from None


def _form(url, fields, headers=None, timeout=60):
    return _request(
        url,
        urllib.parse.urlencode(fields).encode(),
        {"content-type": "application/x-www-form-urlencoded", **(headers or {})},
        timeout,
    )


def _describe(payload):
    if not isinstance(payload, dict):
        return str(payload)
    return str(
        payload.get("error_description")
        or payload.get("error")
        or payload.get("message")
        or "the account service returned an error"
    )


def _remember(data, payload, base):
    data["auth"] = base
    data["access_token"] = payload.get("access_token", "")
    if payload.get("refresh_token"):
        data["refresh_token"] = payload["refresh_token"]
    data["scope"] = payload.get("scope") or data.get("scope") or SCOPES
    data["expires_at"] = int(time.time()) + int(payload.get("expires_in") or 3600) - 60
    save(data)
    return data


def refresh(cfg=None, data=None):
    data = data or load()
    if not data or not data.get("refresh_token"):
        raise AccountError("not signed in — run: openchat login")
    base = data.get("auth") or base_url(cfg)
    status, payload = _form(
        base + "/token",
        {
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "refresh_token": data["refresh_token"],
        },
    )
    if status != 200:
        raise AccountError(_describe(payload))
    return _remember(data, payload, base)


def access(cfg=None):
    data = load()
    if not data:
        raise AccountError("not signed in — run: openchat login")
    if int(data.get("expires_at", 0)) <= int(time.time()) + 30:
        data = refresh(cfg, data)
    return data


def me(cfg=None):
    data = access(cfg)
    base = data.get("auth") or base_url(cfg)
    status, payload = _request(
        base + "/api/me", headers={"authorization": f"Bearer {data['access_token']}"}
    )
    if status == 401:
        data = refresh(cfg, data)
        base = data.get("auth") or base_url(cfg)
        status, payload = _request(
            base + "/api/me",
            headers={"authorization": f"Bearer {data['access_token']}"},
        )
    if status != 200:
        raise AccountError(_describe(payload))
    return payload


def chat(cfg, messages, tools, model):
    data = access(cfg)
    body = {
        "messages": messages,
        "tools": tools,
        "model": model,
        "temperature": cfg.get("temperature", 0.3),
    }
    for attempt in range(2):
        base = data.get("auth") or base_url(cfg)
        status, payload = _request(
            base + "/v1/chat",
            body,
            {
                "authorization": f"Bearer {data['access_token']}",
                "content-type": "application/json",
            },
            timeout=600,
        )
        if status == 401 and attempt == 0:
            data = refresh(cfg, data)
            continue
        if status != 200:
            raise AccountError(_describe(payload))
        return payload
    raise AccountError("not signed in — run: openchat login")


def logout(cfg=None):
    data = load()
    if data and data.get("refresh_token"):
        base = data.get("auth") or base_url(cfg)
        try:
            _form(base + "/revoke", {"token": data["refresh_token"], "client_id": CLIENT_ID})
        except AccountError:
            pass
    clear()


def _pkce():
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def _authorize_url(base, redirect_uri, challenge, state):
    query = urllib.parse.urlencode(
        {
            "client_id": CLIENT_ID,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": SCOPES,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{base}/authorize?{query}"


def _finish(base, code, redirect_uri, verifier):
    status, payload = _form(
        base + "/token",
        {
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        },
    )
    if status != 200:
        raise AccountError(_describe(payload))
    data = _remember(
        {"client_id": CLIENT_ID, "username": "", "user_id": ""}, payload, base
    )
    status, profile = _request(
        base + "/api/me", headers={"authorization": f"Bearer {data['access_token']}"}
    )
    if status != 200:
        raise AccountError(_describe(profile))
    data["username"] = profile.get("username", "")
    data["user_id"] = profile.get("sub") or profile.get("id") or ""
    save(data)
    return data


def _callback_query(value):
    value = (value or "").strip()
    if not value:
        return {}
    if "?" not in value:
        return {"code": value}
    query = urllib.parse.urlparse(value).query or value.split("?", 1)[-1]
    return {key: item[0] for key, item in urllib.parse.parse_qs(query).items()}


def login(cfg=None, paste=False, open_browser=True, timeout=300):
    base = base_url(cfg)
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(24)
    result = {"query": {}, "done": threading.Event()}

    server = None
    if paste:
        port = int(os.environ.get("OPENCHAT_LOGIN_PORT") or DEFAULT_PORT)
        redirect_uri = f"http://127.0.0.1:{port}{CALLBACK_PATH}"
    else:
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path != CALLBACK_PATH:
                    self.send_response(404)
                    self.send_header("content-length", "0")
                    self.end_headers()
                    return
                result["query"] = {
                    key: item[0] for key, item in urllib.parse.parse_qs(parsed.query).items()
                }
                body = (
                    b"<!doctype html><meta charset=utf-8>"
                    b"<title>openchat</title>"
                    b"<body style=\"font:16px system-ui;background:#0b1117;color:#e6edf3;"
                    b"display:grid;place-items:center;height:100vh;margin:0\">"
                    b"<div style=\"text-align:center\"><h1>signed in</h1>"
                    b"<p>You can close this tab and return to the terminal.</p></div>"
                )
                self.send_response(200)
                self.send_header("content-type", "text/html; charset=utf-8")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                result["done"].set()

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        port = server.server_address[1]
        redirect_uri = f"http://127.0.0.1:{port}{CALLBACK_PATH}"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

    url = _authorize_url(base, redirect_uri, challenge, state)
    print()
    print("  sign in to openchat")
    print()
    print("  open this link in a browser:")
    print(f"  {url}")
    print()
    if open_browser and not paste:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    try:
        if paste or not sys.stdin.isatty():
            answer = input("  paste the callback URL (or just the code): ")
            result["query"] = _callback_query(answer)
        else:
            print("  waiting for the browser to finish… (Ctrl-C cancels)")
            if not result["done"].wait(timeout):
                raise AccountError("sign-in timed out")
    except (EOFError, KeyboardInterrupt):
        raise AccountError("sign-in cancelled") from None
    finally:
        if server:
            server.shutdown()
            server.server_close()

    query = result.get("query") or {}
    if query.get("error"):
        raise AccountError(query.get("error_description") or query.get("error"))
    if query.get("state") and query.get("state") != state:
        raise AccountError("sign-in state did not match — try again")
    code = query.get("code")
    if not code:
        raise AccountError("no authorization code returned — try again")

    data = _finish(base, code, redirect_uri, verifier)
    print(f"  signed in as @{data['username']}")
    return data


def status(cfg=None):
    data = load()
    if not data:
        return False, "not signed in — run: openchat login"
    try:
        profile = me(cfg)
        return True, "@" + profile.get("username", data.get("username", "?"))
    except AccountError as exc:
        return False, str(exc)
