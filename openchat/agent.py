"""Providers, the tool loop, and history trimming."""

import json
import sys
import time
from pathlib import Path
import urllib.error
import urllib.request

from . import account
from .config import TMP_DIR, version
from .interrupt import Interrupted, check
from .prompts import SYSTEM_PROMPT
from .render import Spinner, ThinkingPrinter
from .store import trim_history
from .tools import TOOLS, run_tool

def chat_url(base):
    base = base.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return base + "/chat/completions"
    return base + "/v1/chat/completions"


class ApiError(Exception):
    pass


def http_json(url, payload, headers, timeout=300):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "content-type": "application/json",
            "user-agent": f"openchat/{version()}",
            **headers,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:400]
        raise ApiError(f"HTTP {exc.code}: {body}") from None
    except Exception as exc:
        raise ApiError(str(exc)) from None



def stream_completion(cfg, messages, tools, model, printer, thinking=False):
    payload = {
        "model": model,
        "messages": messages,
        "temperature": cfg.get("temperature", 0.3),
        "stream": True,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    req = urllib.request.Request(
        chat_url(cfg["base"]),
        data=json.dumps(payload).encode(),
        headers={
            "content-type": "application/json",
            "authorization": f"Bearer {cfg['api_key']}",
        },
        method="POST",
    )
    clock = time.time()
    show_thinking = bool(thinking) and sys.stdout.isatty()
    think_printer = ThinkingPrinter(show_thinking)
    spinner = Spinner("thinking" if show_thinking else "working")
    spinner.start()
    try:
        resp = urllib.request.urlopen(req, timeout=600)
    except urllib.error.HTTPError as exc:
        spinner.stop()
        body = exc.read().decode(errors="replace")[:400]
        raise ApiError(f"HTTP {exc.code}: {body}") from None
    except Exception as exc:
        spinner.stop()
        raise ApiError(str(exc)) from None

    content = ""
    reasoning = ""
    calls = {}
    started = False
    first_output = False
    try:
        for raw in resp:
            check()
            line = raw.decode(errors="replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except Exception:
                continue
            if chunk.get("error"):
                raise ApiError(str(chunk["error"])[:400])
            choice = (chunk.get("choices") or [{}])[0]
            delta = choice.get("delta") or choice.get("message") or {}
            text = delta.get("content")
            if isinstance(text, list):
                text = "".join(part.get("text", "") for part in text if isinstance(part, dict))
            if text:
                if not first_output:
                    spinner.stop()
                    think_printer.close()
                    first_output = True
                if not started:
                    started = True
                content += text
                if printer:
                    printer.feed(text)
            think = delta.get("reasoning_content") or delta.get("reasoning")
            if think:
                if not first_output:
                    spinner.stop()
                    first_output = True
                reasoning += think
                think_printer.feed(think)
            for call in delta.get("tool_calls") or []:
                if not first_output:
                    spinner.stop()
                    think_printer.close()
                    first_output = True
                idx = call.get("index", 0)
                slot = calls.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                if call.get("id"):
                    slot["id"] = call["id"]
                fn = call.get("function") or {}
                if fn.get("name"):
                    slot["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["arguments"] += fn["arguments"]
            if choice.get("finish_reason") and not started and not content:
                pass
    except Interrupted as stop:
        spinner.stop()
        think_printer.close()
        if printer:
            printer.flush()
        raise Interrupted(content) from None
    spinner.stop()
    think_printer.close()
    if printer:
        printer.flush()
        if started:
            sys.stdout.write("\n")
            sys.stdout.flush()

    tool_calls = []
    for idx in sorted(calls):
        slot = calls[idx]
        if not slot["name"]:
            continue
        tool_calls.append(
            {
                "id": slot["id"] or f"call_{idx}",
                "type": "function",
                "function": {"name": slot["name"], "arguments": slot["arguments"] or "{}"},
            }
        )
    if not content and not tool_calls:
        raise ApiError("empty response")
    return {
        "content": content,
        "reasoning": reasoning,
        "toolCalls": tool_calls,
        "elapsed": round(time.time() - clock, 1),
    }


def worker_completion(cfg, messages, tools, model):
    check()
    clock = time.time()
    try:
        data = account.chat(cfg, messages, tools, model)
    except account.AccountError as exc:
        raise ApiError(str(exc)) from None
    check()
    if not data.get("content") and not data.get("toolCalls"):
        raise ApiError("empty response")
    data.setdefault("elapsed", round(time.time() - clock, 1))
    return data


def complete(cfg, messages, tools, provider="auto", printer=None, thinking=False):
    models = []
    if cfg.get("model"):
        models.append(cfg["model"])
    for m in cfg.get("models", []):
        if m not in models:
            models.append(m)

    direct = bool(cfg.get("base") and cfg.get("api_key"))
    if provider == "worker" or (provider == "auto" and not direct):
        reply = worker_completion(cfg, messages, tools, cfg["model"])
        if printer and reply.get("content"):
            printer.feed(reply["content"])
            printer.flush()
            sys.stdout.write("\n")
        return reply, "account", cfg["model"]

    last = None
    attempts = max(1, int(cfg.get("retries", 3)))
    for model in models:
        for _ in range(attempts):
            try:
                return (
                    stream_completion(cfg, messages, tools, model, printer, thinking),
                    "direct",
                    model,
                )
            except ApiError as exc:
                last = exc
                if "empty response" in str(exc):
                    break
                time.sleep(0.6)
    if provider == "auto" and account.signed_in():
        try:
            reply = worker_completion(cfg, messages, tools, cfg["model"])
            if printer and reply.get("content"):
                printer.feed(reply["content"])
                printer.flush()
                sys.stdout.write("\n")
            return reply, "account", cfg["model"]
        except ApiError as exc:
            last = exc
    raise last or ApiError("no provider configured")



def agent_run(
    cfg,
    cwd,
    history,
    prompt,
    provider="auto",
    printer=None,
    on_tool=None,
    on_tool_result=None,
    approve=None,
    project=None,
    tmp=None,
    thinking=False,
    tools=None,
    pins=None,
    todos=None,
    plan=False,
):
    """Run the tool loop against an arbitrary message list.

    Returns {content, messages, provider, model, error}. No console output
    except what the optional printer renders.
    """
    messages = [dict(m) for m in history]
    if not messages or messages[0].get("role") != "system":
        content = SYSTEM_PROMPT.format(
            cwd=cwd,
            project=project or "(none — work in the working directory)",
            tmp=tmp or str(TMP_DIR),
            platform=sys.platform,
        )
        memory = Path(cwd) / "OPENCHAT.md"
        try:
            if memory.is_file():
                notes = memory.read_text(errors="replace").strip()
                if notes:
                    content += "\nProject memory (OPENCHAT.md — follow it):\n" + notes[:8000] + "\n"
        except OSError:
            pass
        if plan:
            content += (
                "\nMode: PLAN ONLY. Do not edit files, do not run commands that change "
                "state. Read, search, and then propose a concrete step-by-step plan.\n"
            )
        if todos:
            content += "\nSession task list:\n"
            for number, task in enumerate(todos, 1):
                mark = "x" if task.get("done") else " "
                content += f"{number}. [{mark}] {task.get('text', '')}\n"
        if pins:
            budget = 12000
            content += "\nPinned files (current contents; keep them in mind):\n"
            for pin in pins:
                if budget <= 0:
                    break
                raw = pin if Path(pin).is_absolute() else Path(cwd) / pin
                try:
                    path = Path(raw)
                    if not path.is_file():
                        continue
                    body = path.read_text(errors="replace")
                except OSError:
                    continue
                body = body[: min(4000, budget)]
                budget -= len(body)
                content += f"\n--- {pin} ---\n{body}\n"
        messages.insert(0, {"role": "system", "content": content})
    if prompt:
        messages.append({"role": "user", "content": prompt})

    model_used = cfg.get("model", "")
    use_tools = TOOLS if tools is None else tools
    if plan and use_tools:
        allowed = {"read", "ls", "glob", "grep", "tree", "fetch"}
        use_tools = [t for t in use_tools if t["function"]["name"] in allowed]
    round_index = 0
    while True:
        messages[:] = trim_history(messages)
        try:
            check()
            reply, provider_used, model_used = complete(
                cfg, messages, use_tools, provider, printer, thinking
            )
        except Interrupted as stop:
            if stop.partial:
                messages.append({"role": "assistant", "content": stop.partial})
            return {
                "content": stop.partial,
                "messages": messages,
                "provider": "",
                "model": model_used,
                "interrupted": True,
            }
        except ApiError as exc:
            return {
                "content": "",
                "messages": messages,
                "provider": "",
                "model": model_used,
                "error": str(exc),
            }

        text = reply.get("content") or ""
        calls = reply.get("toolCalls") or []
        assistant = {"role": "assistant", "content": text}
        if calls:
            assistant["tool_calls"] = [
                {
                    "id": c["id"],
                    "type": "function",
                    "function": {
                        "name": c["function"]["name"],
                        "arguments": c["function"].get("arguments") or "{}",
                    },
                }
                for c in calls
            ]
        messages.append(assistant)

        if not calls:
            return {
                "content": text,
                "messages": messages,
                "provider": provider_used,
                "model": model_used,
                "elapsed": reply.get("elapsed"),
                "rounds": round_index + 1,
            }

        round_index += 1
        for call in calls:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
            except Exception:
                args = {}
            if on_tool:
                on_tool(name, args)
            if approve is not None and not approve(name, args):
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": "error: user rejected this action",
                    }
                )
                continue
            clock = time.time()
            try:
                check()
                result = run_tool(cwd, name, args, journal=tmp)
            except Interrupted:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": "(interrupted by user)",
                    }
                )
                return {
                    "content": "",
                    "messages": messages,
                    "provider": "",
                    "model": model_used,
                    "interrupted": True,
                }
            if on_tool_result:
                on_tool_result(name, args, result, round(time.time() - clock, 1))
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": result,
                }
            )

    return {
        "content": "",
        "messages": messages,
        "provider": "",
        "model": model_used,
        "error": "too many tool rounds",
    }

