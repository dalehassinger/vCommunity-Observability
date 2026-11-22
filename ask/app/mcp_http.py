"""MCP servers Ask reaches itself over HTTP (MCP "Streamable HTTP" transport), next to the
host agent's stdio servers. For servers that run as containers, for example the draw.io MCP
server (jgraph/drawio-mcp, `drawio = http://drawio-mcp:3001/mcp` on the compose network).

Configured with the `mcp_http_servers` setting (MCP tools page) or ASK_MCP_HTTP_SERVERS:
one server per line (or comma-separated), `name = url`. Optional headers after the URL,
separated by `|`: `name = url | Authorization: Bearer xyz`.

Each server keeps one MCP session (Mcp-Session-Id). A session the server forgets (HTTP 404)
is started again once. Answers may be plain JSON or an event stream (SSE); both are read.
The rows look like the host agent's, so the MCP tools page, the Sources chips and the
answer loop treat both kinds the same."""

from __future__ import annotations

import json
import re
import threading
import time
from typing import Any

import requests

PROTOCOL = "2025-06-18"
CLIENT = {"name": "vcommunity-ask", "version": "1"}
CACHE_SECONDS = 20
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class McpHttpError(RuntimeError):
    pass


def parse(text: str) -> tuple[list[dict[str, str]], list[str]]:
    """The servers in the setting: ([{name, url, headers}], [problems])."""
    out: list[dict[str, Any]] = []
    bad: list[str] = []
    for raw in re.split(r"[\n,]", text or ""):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            bad.append(f"'{line}': use name = url")
            continue
        name, rest = (x.strip() for x in line.split("=", 1))
        parts = [p.strip() for p in rest.split("|")]
        url, headers = parts[0], {}
        for h in parts[1:]:
            if ":" in h:
                k, v = h.split(":", 1)
                headers[k.strip()] = v.strip()
        if not NAME_RE.match(name):
            bad.append(f"'{name}': letters, digits, - _ . only")
        elif not re.match(r"^https?://[^\s/]+", url):
            bad.append(f"'{name}': the URL must start with http:// or https://")
        elif any(s["name"] == name for s in out):
            bad.append(f"'{name}' is listed twice")
        else:
            out.append({"name": name, "url": url, "headers": headers})
    return out, bad


class Server:
    def __init__(self, name: str, url: str, headers: dict[str, str] | None = None):
        self.name, self.url, self.headers = name, url, dict(headers or {})
        self.session: str | None = None
        self.info: dict[str, Any] = {}
        self.tools: list[dict[str, Any]] = []
        self.error = ""
        self.at = 0.0
        self.lock = threading.Lock()
        self._id = 0

    # -- JSON-RPC over HTTP -------------------------------------------------------------------
    def _post(self, body: dict[str, Any], timeout: float) -> requests.Response:
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
             "MCP-Protocol-Version": PROTOCOL, **self.headers}
        if self.session:
            h["Mcp-Session-Id"] = self.session
        try:
            return requests.post(self.url, json=body, headers=h, timeout=timeout, stream=True)
        except requests.RequestException as e:
            raise McpHttpError(f"Cannot reach the MCP server {self.name} at {self.url}: {e.__class__.__name__}: {e}") from e

    @staticmethod
    def _read(r: requests.Response, want: int) -> dict[str, Any]:
        """The JSON-RPC answer with id `want`, from a JSON body or an event stream."""
        ctype = (r.headers.get("Content-Type") or "").lower()
        if "text/event-stream" in ctype:
            data: list[str] = []
            for raw in r.iter_lines(decode_unicode=True):
                if raw is None:
                    continue
                if raw == "":
                    if data:
                        try:
                            msg = json.loads("\n".join(data))
                        except ValueError:
                            msg = None
                        data = []
                        if isinstance(msg, dict) and msg.get("id") == want:
                            r.close()
                            return msg
                    continue
                if raw.startswith("data:"):
                    data.append(raw[5:].lstrip())
            if data:
                try:
                    msg = json.loads("\n".join(data))
                    if isinstance(msg, dict) and msg.get("id") == want:
                        return msg
                except ValueError:
                    pass
            raise McpHttpError("the server's event stream ended without an answer")
        try:
            msg = r.json()
        except ValueError as e:
            raise McpHttpError(f"the server did not answer with JSON (HTTP {r.status_code})") from e
        if isinstance(msg, list):                 # a batch: pick ours
            msg = next((m for m in msg if isinstance(m, dict) and m.get("id") == want), {})
        return msg

    def _rpc(self, method: str, params: dict[str, Any] | None, timeout: float, retry: bool = True) -> Any:
        self._id += 1
        rid = self._id
        body = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            body["params"] = params
        r = self._post(body, timeout)
        if r.status_code == 404 and self.session and retry:
            # The server forgot the session (restarted): start a new one, once.
            self.session = None
            self._initialize(timeout)
            return self._rpc(method, params, timeout, retry=False)
        if r.status_code >= 400:
            text = r.text[:300] if not (r.headers.get("Content-Type") or "").startswith("text/event-stream") else ""
            raise McpHttpError(f"{self.name} answered HTTP {r.status_code} to {method}" + (f": {text}" if text else ""))
        if method == "initialize" and r.headers.get("Mcp-Session-Id"):
            self.session = r.headers["Mcp-Session-Id"]
        msg = self._read(r, rid)
        if msg.get("error"):
            err = msg["error"]
            raise McpHttpError(f"{self.name}: {err.get('message') if isinstance(err, dict) else err}")
        return msg.get("result")

    def _notify(self, method: str, timeout: float) -> None:
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
             "MCP-Protocol-Version": PROTOCOL, **self.headers}
        if self.session:
            h["Mcp-Session-Id"] = self.session
        try:
            requests.post(self.url, json={"jsonrpc": "2.0", "method": method}, headers=h, timeout=timeout).close()
        except requests.RequestException:
            pass

    def _initialize(self, timeout: float) -> None:
        res = self._rpc("initialize", {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": CLIENT},
                        timeout, retry=False) or {}
        self.info = res.get("serverInfo") or {}
        self._notify("notifications/initialized", timeout)

    # -- what Ask uses ------------------------------------------------------------------------
    def refresh(self, fresh: bool = False, timeout: float = 15) -> None:
        with self.lock:
            if not fresh and self.at and time.time() - self.at < CACHE_SECONDS:
                return
            try:
                if fresh:
                    self.session = None
                if not self.session:
                    self._initialize(timeout)
                tools, cursor = [], None
                for _ in range(20):                       # pages
                    res = self._rpc("tools/list", {"cursor": cursor} if cursor else {}, timeout) or {}
                    tools += res.get("tools") or []
                    cursor = res.get("nextCursor")
                    if not cursor:
                        break
                self.tools, self.error = tools, ""
            except McpHttpError as e:
                self.error, self.session = str(e), None
            self.at = time.time()

    def row(self) -> dict[str, Any]:
        return {"name": self.name, "transport": "http", "url": self.url, "command": self.url,
                "running": not self.error and bool(self.at), "starting": False, "error": self.error,
                "tools": self.tools, "server": self.info, "stderr": []}

    def call(self, tool: str, args: dict[str, Any], timeout: float) -> dict[str, Any]:
        with self.lock:
            if not self.session:
                self._initialize(15)
            return self._rpc("tools/call", {"name": tool, "arguments": args}, timeout) or {}


_servers: dict[str, Server] = {}
_lock = threading.Lock()


def configured(text: str) -> list[Server]:
    """The Server objects for the setting (kept between calls, so sessions are reused)."""
    specs, _ = parse(text)
    with _lock:
        keep = {}
        for s in specs:
            old = _servers.get(s["name"])
            keep[s["name"]] = old if old and old.url == s["url"] and old.headers == s["headers"] else Server(**s)
        _servers.clear()
        _servers.update(keep)
        return list(keep.values())


def rows(text: str, fresh: bool = False, timeout: float = 15) -> list[dict[str, Any]]:
    out = []
    for s in configured(text):
        s.refresh(fresh, timeout)
        out.append(s.row())
    return out


def cached_rows(text: str) -> list[dict[str, Any]]:
    """For page renders: never waits on a server; a server not asked yet is asked briefly."""
    out = []
    for s in configured(text):
        if not s.at:
            s.refresh(False, 3)
        out.append(s.row())
    return out


def get(name: str) -> Server | None:
    with _lock:
        return _servers.get(name)
