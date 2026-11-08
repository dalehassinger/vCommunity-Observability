"""Web search for Ask: Ollama's web search and web fetch APIs, offered to the chat model
as two tools (web_search, web_fetch) when "Web search (Ollama)" is picked in the
Sources menu. The model decides when to search; each result is one numbered source.

The APIs are a cloud service of ollama.com (https://docs.ollama.com/capabilities/web-search):
they need an Ollama account API key in the OLLAMA_API_KEY environment variable (.env)
and internet access. The key is never stored in ask.db or shown in the UI. Web search is
off until the "Web search" setting is switched on, so dark sites are not affected."""

from __future__ import annotations

import os
import time
from typing import Any
from urllib.parse import urlparse

import requests

SCOPE = "web:ollama"          # the value of the Sources menu entry
SERVER = "web"                # what tool cards show as the "server"
DEFAULT_URL = "https://ollama.com"

TOOLS = [
    {"name": "web_search",
     "description": ("Search the web. Use it for current information, product versions, release notes, "
                     "news, or anything the knowledge sources do not cover. Returns titles, URLs and page text."),
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "The search query"},
         "max_results": {"type": "integer", "description": "How many results (1-10, default 5)"}},
         "required": ["query"]}},
    {"name": "web_fetch",
     "description": "Fetch one web page by URL (for example a search result) and return its text and links.",
     "inputSchema": {"type": "object", "properties": {
         "url": {"type": "string", "description": "The full http(s) URL"}},
         "required": ["url"]}},
]


def api_key() -> str:
    return os.environ.get("OLLAMA_API_KEY", "").strip()


def status(cfg: dict[str, Any]) -> dict[str, Any]:
    """For pages: is web search offered, and if not, why."""
    if not cfg.get("web_search"):
        return {"available": False, "why": "Web search is off (Settings)."}
    if not api_key():
        return {"available": False, "why": "OLLAMA_API_KEY is not set in .env."}
    return {"available": True, "why": ""}


def ollama_tools() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Tools for /api/chat plus the name -> (server, tool) index rag.run_tool uses."""
    tools, index = [], {}
    for t in TOOLS:
        index[t["name"]] = (SERVER, {**t, "annotations": {"readOnlyHint": True}})
        tools.append({"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                        "parameters": t["inputSchema"]}})
    return tools, index


def _post(cfg: dict[str, Any], path: str, body: dict[str, Any]) -> dict[str, Any]:
    base = (cfg.get("web_search_url") or DEFAULT_URL).rstrip("/")
    r = requests.post(f"{base}{path}", json=body, timeout=45,
                      headers={"Authorization": f"Bearer {api_key()}"})
    if r.status_code in (401, 403):
        raise RuntimeError("Ollama refused the API key (OLLAMA_API_KEY in .env): check it on ollama.com/settings/keys")
    if r.status_code == 429:
        raise RuntimeError("Ollama's web search limit was reached (429): try again later")
    if r.status_code >= 400:
        raise RuntimeError(f"Ollama web search returned {r.status_code}: {r.text[:300]}")
    return r.json()


def call(cfg: dict[str, Any], tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """{"ok", "text" (what the model reads), "result" (for the card), "links", "error", "ms"}."""
    t0 = time.time()
    try:
        if not api_key():
            raise RuntimeError("OLLAMA_API_KEY is not set in .env")
        if tool == "web_search":
            query = str(args.get("query") or "").strip()
            if not query:
                raise RuntimeError("web_search needs a query")
            try:
                n = int(args.get("max_results") or cfg.get("web_search_results") or 5)
            except (TypeError, ValueError):
                n = 5
            d = _post(cfg, "/api/web_search", {"query": query, "max_results": max(1, min(10, n))})
            results = d.get("results") or []
            links = [{"title": (x.get("title") or x.get("url") or "").strip(), "url": x.get("url") or ""} for x in results]
            parts = [f"Web search results for: {query}"]
            for i, x in enumerate(results, 1):
                parts.append(f"\n--- Result {i}: {(x.get('title') or '').strip()}\nURL: {x.get('url') or ''}\n"
                             f"{(x.get('content') or '').strip()}")
            if not results:
                parts.append("(no results)")
            text = "\n".join(parts)
        elif tool == "web_fetch":
            url = str(args.get("url") or "").strip()
            if urlparse(url).scheme not in ("http", "https"):
                raise RuntimeError("web_fetch needs a full http(s) URL")
            d = _post(cfg, "/api/web_fetch", {"url": url})
            links = [{"title": (d.get("title") or url).strip(), "url": url}]
            more = [l for l in (d.get("links") or []) if isinstance(l, str)][:30]
            text = (f"Page: {(d.get('title') or '').strip()}\nURL: {url}\n\n{(d.get('content') or '').strip()}"
                    + ("\n\nLinks on the page:\n" + "\n".join(more) if more else ""))
        else:
            raise RuntimeError(f"unknown web tool {tool}")
    except requests.RequestException as e:
        return {"ok": False, "error": f"Cannot reach Ollama web search: {e.__class__.__name__} (internet access?)",
                "text": "", "links": [], "ms": int((time.time() - t0) * 1000)}
    except (RuntimeError, ValueError) as e:
        return {"ok": False, "error": str(e), "text": "", "links": [], "ms": int((time.time() - t0) * 1000)}
    return {"ok": True, "text": text, "links": [l for l in links if l["url"]], "error": "",
            "ms": int((time.time() - t0) * 1000)}
