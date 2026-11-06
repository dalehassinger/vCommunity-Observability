"""MCP tools: Ask calls the tools of MCP servers that run on the host, through the Ask
host agent (ask/agent, `/mcp` endpoints, same token as the Performance page).

- servers(): what the agent offers (cached for a short while so pages stay fast);
- the tool policy: every tool is "allow", "ask" (a Run / Cancel card in the chat first)
  or "off". Saved as JSON in the `mcp_tools` setting; a tool that is not in it gets
  "allow" when the server marks it read-only (annotations.readOnlyHint) and "ask" otherwise;
- ollama_tools(): the picked servers' tools in Ollama's function-calling format;
- call(): run one tool and return the text the model reads;
- approvals: a tool set to "ask" waits here until the chat page answers Run or Cancel."""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from typing import Any

import requests

CACHE_SECONDS = 20
POLICIES = ("allow", "ask", "off")
_cache: dict[str, Any] = {"at": 0.0, "url": "", "data": None}
_lock = threading.Lock()


def _headers() -> dict[str, str]:
    tok = os.environ.get("ASK_AGENT_TOKEN", "")
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def servers(url: str, *, fresh: bool = False, timeout: float = 60, wait: bool = True) -> dict[str, Any]:
    """{"ok", "error", "servers": [{name, running, error, tools, stderr, ...}]}. A failure keeps
    the last good list (marked stale) so the chat page still shows the tools menu."""
    url = (url or "").rstrip("/")
    if not url:
        return {"ok": False, "error": "No host agent URL (Settings).", "servers": []}
    with _lock:
        if not fresh and _cache["data"] and _cache["url"] == url and time.time() - _cache["at"] < CACHE_SECONDS:
            return _cache["data"]
    try:
        r = requests.get(f"{url}/mcp" + ("" if wait else "?wait=0"), headers=_headers(), timeout=timeout)
        if r.status_code == 401:
            raise RuntimeError("The host agent refused the token: check ASK_AGENT_TOKEN in .env.")
        if r.status_code == 404:
            raise RuntimeError("The host agent has no MCP bridge: install the new ask/agent (version 0.2.0 or later).")
        r.raise_for_status()
        d = r.json()
        data = {"ok": True, "error": d.get("error") or "", "config": d.get("config", ""), "servers": d.get("servers") or []}
    except (requests.RequestException, ValueError, RuntimeError) as e:
        msg = str(e) if isinstance(e, RuntimeError) else f"Cannot reach the host agent at {url}: {e.__class__.__name__}"
        with _lock:
            old = _cache["data"] if _cache["url"] == url else None
        return {"ok": False, "error": msg, "stale": bool(old), "servers": (old or {}).get("servers", [])}
    with _lock:
        _cache.update(at=time.time(), url=url, data=data)
    return data


def quick_servers(url: str) -> list[dict[str, Any]]:
    """For page renders: the cached list, or a short request (never blocks a page long)."""
    url = (url or "").rstrip("/")
    with _lock:
        if _cache["data"] and _cache["url"] == url:
            return _cache["data"]["servers"]
        if _cache.get("failed_url") == url and time.time() - _cache.get("failed_at", 0) < 60:
            return []
    if not url:
        return []
    d = servers(url, timeout=3, wait=False)
    if not d["ok"]:
        with _lock:
            _cache.update(failed_url=url, failed_at=time.time())
    return d["servers"]


def reload(url: str, name: str) -> dict[str, Any]:
    r = requests.post(f"{url.rstrip('/')}/mcp/{name}/reload", headers=_headers(), timeout=120)
    r.raise_for_status()
    with _lock:
        _cache["at"] = 0.0
    return r.json()


def load_policy(raw: Any) -> dict[str, str]:
    try:
        d = json.loads(raw) if isinstance(raw, str) and raw.strip() else (raw or {})
    except ValueError:
        return {}
    return {str(k): v for k, v in d.items() if v in POLICIES} if isinstance(d, dict) else {}


def read_only(tool: dict[str, Any]) -> bool:
    return bool((tool.get("annotations") or {}).get("readOnlyHint"))


def policy_of(policy: dict[str, str], server: str, tool: dict[str, Any]) -> str:
    return policy.get(f"{server}/{tool['name']}") or ("allow" if read_only(tool) else "ask")


def _schema(s: Any) -> Any:
    """JSON schema without the keys small models trip over ($schema)."""
    if isinstance(s, dict):
        return {k: _schema(v) for k, v in s.items() if k not in ("$schema",)}
    if isinstance(s, list):
        return [_schema(v) for v in s]
    return s


def ollama_tools(srv_list: list[dict[str, Any]], picked: list[str], policy: dict[str, str]) -> tuple[list, dict]:
    """Tools for /api/chat and a map from the name the model sees to (server, tool). Tools
    set to "off" are left out. Two servers with the same tool name get "server__tool"."""
    tools, index = [], {}
    names = [t["name"] for s in srv_list if s["name"] in picked for t in s.get("tools") or []]
    for s in srv_list:
        if s["name"] not in picked:
            continue
        for t in s.get("tools") or []:
            if policy_of(policy, s["name"], t) == "off":
                continue
            fname = t["name"] if names.count(t["name"]) == 1 else f"{s['name']}__{t['name']}"
            index[fname] = (s["name"], t)
            tools.append({"type": "function", "function": {
                "name": fname, "description": (t.get("description") or t["name"]).strip()[:1024],
                "parameters": _schema(t.get("inputSchema") or {"type": "object", "properties": {}})}})
    return tools, index


def call(url: str, server: str, tool: str, args: dict[str, Any], timeout: float = 180) -> dict[str, Any]:
    """{"ok", "text" (what the model reads), "error", "ms"}."""
    try:
        r = requests.post(f"{url.rstrip('/')}/mcp/{server}/call", headers=_headers(),
                          json={"tool": tool, "arguments": args, "timeout": timeout}, timeout=timeout + 15)
        d = r.json()
    except (requests.RequestException, ValueError) as e:
        return {"ok": False, "error": f"Cannot reach the host agent: {e.__class__.__name__}: {e}", "text": "", "ms": 0}
    if r.status_code >= 400 or not d.get("ok"):
        return {"ok": False, "error": d.get("error") or f"host agent returned {r.status_code}", "text": "", "ms": d.get("ms", 0)}
    res = d.get("result") or {}
    parts = []
    for c in res.get("content") or []:
        if c.get("type") == "text":
            parts.append(c.get("text") or "")
        else:
            parts.append(f"({c.get('type')} content left out)")
    text = "\n".join(parts).strip()
    if not text and res.get("structuredContent") is not None:
        text = json.dumps(res["structuredContent"], default=str)
    if res.get("isError"):
        return {"ok": False, "error": text or "the tool failed", "text": "", "ms": d.get("ms", 0)}
    return {"ok": True, "text": text, "error": "", "ms": d.get("ms", 0)}


def pretty(text: str) -> str:
    """Tool output for people: indented JSON when it is JSON (PowerShell MCP results are
    {"data": ...}: the data part is shown)."""
    try:
        v = json.loads(text)
    except (ValueError, TypeError):
        return text
    if isinstance(v, dict) and set(v) == {"data"}:
        v = v["data"]
    return json.dumps(v, indent=2, default=str)


# -- Run / Cancel for tools set to "ask" ----------------------------------------------
class Approvals:
    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}
        self.lock = threading.Lock()

    def new(self, chat_id: str) -> str:
        key = secrets.token_urlsafe(16)
        with self.lock:
            self.items[key] = {"event": threading.Event(), "run": None, "chat_id": chat_id}
        return key

    def answer(self, key: str, run: bool) -> bool:
        with self.lock:
            it = self.items.get(key)
        if not it:
            return False
        it["run"] = bool(run)
        it["event"].set()
        return True

    def wait(self, key: str, seconds: float) -> bool | None:
        """True / False once answered; None while still waiting after `seconds`."""
        it = self.items.get(key)
        if not it:
            return False
        return it["run"] if it["event"].wait(seconds) else None

    def drop(self, key: str) -> None:
        with self.lock:
            self.items.pop(key, None)


APPROVALS = Approvals()
