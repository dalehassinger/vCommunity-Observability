"""Web search for Ask, two providers the chat model can call as tools. Each is its own
entry in the Sources menu; the model decides when to search; each result is one
numbered source.

- Ollama (scope "web:ollama"): Ollama's web search and web fetch APIs, tools web_search
  and web_fetch (https://docs.ollama.com/capabilities/web-search). Key: OLLAMA_API_KEY.
- Brave (scope "web:brave"): the Brave Search API, tool brave_search
  (https://api-dashboard.search.brave.com). Key: BRAVE_API_KEY.

Both are cloud services that need internet access and an API key in .env. Keys are never
stored in ask.db or shown in the UI. Each provider is off until its setting is switched
on, so dark sites are not affected."""

from __future__ import annotations

import html
import os
import re
import time
from typing import Any
from urllib.parse import urlparse

import requests

from . import webusage

SCOPE = "web:ollama"          # the value of the Sources menu entry (Ollama)
BRAVE_SCOPE = "web:brave"     # the value of the Sources menu entry (Brave)
SCOPES = (SCOPE, BRAVE_SCOPE)
SERVER = "web"                # tool index marker: rag.run_tool sends these here
DEFAULT_URL = "https://ollama.com"
BRAVE_DEFAULT_URL = "https://api.search.brave.com"
BRAVE_TOOL = "brave_search"

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


BRAVE_TOOLS = [
    {"name": BRAVE_TOOL,
     "description": ("Search the web with Brave Search. Use it for current information, product versions, release "
                     "notes, news, or anything the knowledge sources do not cover. Returns titles, URLs, a summary "
                     "and extra snippets of each page."),
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "The search query"},
         "max_results": {"type": "integer", "description": "How many results (1-10, default 5)"}},
         "required": ["query"]}},
]


def api_key() -> str:
    return os.environ.get("OLLAMA_API_KEY", "").strip()


def brave_key() -> str:
    return os.environ.get("BRAVE_API_KEY", "").strip()


def status(cfg: dict[str, Any]) -> dict[str, Any]:
    """For pages: is web search offered, and if not, why."""
    if not cfg.get("web_search"):
        return {"available": False, "why": "Web search is off (Settings)."}
    if not api_key():
        return {"available": False, "why": "OLLAMA_API_KEY is not set in .env."}
    return {"available": True, "why": ""}


def brave_status(cfg: dict[str, Any]) -> dict[str, Any]:
    """Same as status() for Brave."""
    if not cfg.get("brave_search"):
        return {"available": False, "why": "Brave web search is off (Settings)."}
    if not brave_key():
        return {"available": False, "why": "BRAVE_API_KEY is not set in .env."}
    return {"available": True, "why": ""}


def status_of(cfg: dict[str, Any], scope: str) -> dict[str, Any]:
    return brave_status(cfg) if scope == BRAVE_SCOPE else status(cfg)


def label(scope_or_tool: str) -> str:
    return "Brave web search" if scope_or_tool in (BRAVE_SCOPE, BRAVE_TOOL) else "Ollama web search"


def ollama_tools(scope: str = SCOPE) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Tools for /api/chat plus the name -> (server, tool) index rag.run_tool uses."""
    tools, index = [], {}
    for t in (BRAVE_TOOLS if scope == BRAVE_SCOPE else TOOLS):
        index[t["name"]] = (SERVER, {**t, "annotations": {"readOnlyHint": True}})
        tools.append({"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                        "parameters": t["inputSchema"]}})
    return tools, index


def _post(cfg: dict[str, Any], path: str, body: dict[str, Any]) -> dict[str, Any]:
    base = (cfg.get("web_search_url") or DEFAULT_URL).rstrip("/")
    _LAST["sent"] = True
    r = requests.post(f"{base}{path}", json=body, timeout=45,
                      headers={"Authorization": f"Bearer {api_key()}"})
    if r.status_code in (401, 403):
        raise RuntimeError("Ollama refused the API key (OLLAMA_API_KEY in .env): check it on ollama.com/settings/keys")
    if r.status_code == 429:
        raise RuntimeError("Ollama's web search limit was reached (429): try again later")
    if r.status_code >= 400:
        raise RuntimeError(f"Ollama web search returned {r.status_code}: {r.text[:300]}")
    return r.json()


def _strip(s: Any) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", str(s or ""))).strip()


def _brave(cfg: dict[str, Any], args: dict[str, Any]) -> tuple[str, list[dict[str, str]]]:
    query = str(args.get("query") or "").strip()
    if not query:
        raise RuntimeError("brave_search needs a query")
    try:
        n = int(args.get("max_results") or cfg.get("web_search_results") or 5)
    except (TypeError, ValueError):
        n = 5
    base = (cfg.get("brave_search_url") or BRAVE_DEFAULT_URL).rstrip("/")
    _LAST["sent"] = True
    r = requests.get(f"{base}/res/v1/web/search", timeout=30,
                     params={"q": query, "count": max(1, min(10, n)), "extra_snippets": "true", "text_decorations": "false"},
                     headers={"Accept": "application/json", "X-Subscription-Token": brave_key()})
    _LAST["quota"] = webusage.parse_quota(r.headers)
    if r.status_code in (401, 403) or (r.status_code == 422 and "token" in r.text.lower()):
        raise RuntimeError("Brave refused the API key (BRAVE_API_KEY in .env): check it on api-dashboard.search.brave.com")
    if r.status_code == 429:
        raise RuntimeError("Brave Search's rate or monthly limit was reached (429): try again later")
    if r.status_code >= 400:
        raise RuntimeError(f"Brave Search returned {r.status_code}: {r.text[:300]}")
    results = ((r.json().get("web") or {}).get("results") or [])[:max(1, min(10, n))]
    links = [{"title": _strip(x.get("title")) or x.get("url") or "", "url": x.get("url") or ""} for x in results]
    parts = [f"Web search results (Brave) for: {query}"]
    for i, x in enumerate(results, 1):
        extra = [_strip(e) for e in (x.get("extra_snippets") or []) if e]
        parts.append(f"\n--- Result {i}: {_strip(x.get('title'))}\nURL: {x.get('url') or ''}"
                     + (f"\nAge: {x['age']}" if x.get("age") else "") + f"\n{_strip(x.get('description'))}"
                     + ("\n" + "\n".join(f"- {e}" for e in extra) if extra else ""))
    if not results:
        parts.append("(no results)")
    return "\n".join(parts), links


_LAST: dict[str, Any] = {}


def call(cfg: dict[str, Any], tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """Run one web tool and count it for the Performance page (app/webusage.py). A call that
    never reached the provider (no key, bad arguments) is not counted."""
    _LAST.clear()
    _LAST["sent"] = False
    res = _call(cfg, tool, args)
    if webusage.RECORDER is not None and _LAST.get("sent"):
        provider = "brave" if tool == BRAVE_TOOL else "ollama"
        webusage.RECORDER(provider, tool, res["ok"], limited="429" in (res.get("error") or ""), ms=res.get("ms"),
                          quota=_LAST.get("quota"))
    return res


def _call(cfg: dict[str, Any], tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """{"ok", "text" (what the model reads), "result" (for the card), "links", "error", "ms"}."""
    t0 = time.time()
    if tool == BRAVE_TOOL:
        try:
            if not brave_key():
                raise RuntimeError("BRAVE_API_KEY is not set in .env")
            text, links = _brave(cfg, args)
        except requests.RequestException as e:
            return {"ok": False, "error": f"Cannot reach Brave Search: {e.__class__.__name__} (internet access?)",
                    "text": "", "links": [], "ms": int((time.time() - t0) * 1000)}
        except (RuntimeError, ValueError) as e:
            return {"ok": False, "error": str(e), "text": "", "links": [], "ms": int((time.time() - t0) * 1000)}
        return {"ok": True, "text": text, "links": [l for l in links if l["url"]], "error": "",
                "ms": int((time.time() - t0) * 1000)}
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
