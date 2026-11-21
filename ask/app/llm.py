"""Model providers for Ask: Ollama (native API) and LM Studio (its OpenAI-compatible API).

Every caller asks `client(cfg)` for a `Models` object. It has the same methods as the Ollama
client (models, embed, chat, chat_stream) and sends each call to the provider that owns the
model name:

  - LM Studio model names carry the prefix `lmstudio:` (for example
    `lmstudio:google/gemma-4-31b`), so a saved chat model, embedding model, saved prompt or
    scheduled prompt always knows where its model lives.
  - Every other name is an Ollama model, as before (old settings keep working).

LM Studio's streamed answer is turned into the same objects Ollama streams
({"message": {"content", "thinking", "tool_calls"}, "done", "eval_count", ...}), so the
answer loop, tool calls and stats work the same for both.

Each provider can be turned off in Settings (`ollama_enabled`, `lmstudio_enabled`). A model
of a provider that is off is not listed, and asking it fails with a clear message.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Iterator
from urllib.parse import urlsplit, urlunsplit

import requests

from .ollama import Ollama, OllamaError

PREFIX = "lmstudio:"
LMSTUDIO = "lmstudio"
OLLAMA = "ollama"
LABELS = {OLLAMA: "Ollama", LMSTUDIO: "LM Studio"}
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1", "[::1]")


class LLMError(OllamaError):
    """Any provider failure. A subclass of OllamaError, so every existing `except OllamaError` catches it."""


def provider_of(model: str) -> str:
    return LMSTUDIO if str(model or "").startswith(PREFIX) else OLLAMA


def bare(model: str) -> str:
    """The provider's own model id (without the `lmstudio:` prefix)."""
    return model[len(PREFIX):] if str(model or "").startswith(PREFIX) else model


def in_container() -> bool:
    return os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv") or bool(os.environ.get("ASK_IN_CONTAINER"))


def reach(url: str) -> str:
    """Inside a container, 127.0.0.1 / localhost is the container itself, not the Mac. An URL
    that names it is sent to host.docker.internal instead (the Mac, on Rancher and Docker Desktop),
    so `http://127.0.0.1:1234` as LM Studio shows it works as typed. Set ASK_HOST_ALIAS to use
    another name (for example host.lima.internal with containerd), or `off` to keep the URL."""
    url = (url or "").strip().rstrip("/")
    alias = os.environ.get("ASK_HOST_ALIAS", "host.docker.internal").strip()
    if not url or alias.lower() == "off" or not in_container():
        return url
    p = urlsplit(url)
    host = (p.hostname or "").lower()
    if host not in LOCAL_HOSTS and host != "::1":
        return url
    netloc = alias + (f":{p.port}" if p.port else "")
    return urlunsplit((p.scheme, netloc, p.path, p.query, p.fragment))


def keep_alive_seconds(keep_alive: str) -> int | None:
    """Ollama's keep_alive ('10m', '1h', '30s', '0', '-1') as seconds for LM Studio's `ttl`.
    None = leave it to LM Studio (forever, or 0 meaning unload at once, which LM Studio lacks)."""
    m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*([smhd]?)\s*", str(keep_alive or ""))
    if not m:
        return None
    n = float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
    return int(n) if n >= 1 else None


# ---------------------------------------------------------------------------------------------
# LM Studio


class LMStudio:
    """LM Studio's local server: model list (native /api/v0/models, else /v1/models), embeddings
    (/v1/embeddings) and streamed chat with tools (/v1/chat/completions)."""

    def __init__(self, url: str, timeout: float = 600, api_key: str = ""):
        self.url = reach(url)
        self.timeout = timeout
        self.headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    def _err(self, what: str, e: Exception) -> LLMError:
        hint = ""
        if isinstance(e, requests.ConnectionError) and in_container():
            hint = (" (LM Studio must accept connections from the network: Developer tab, Settings,"
                    " turn on 'Serve on Local Network', then start the server)")
        return LLMError(f"Cannot reach LM Studio at {self.url}: {what}: {e.__class__.__name__}: {e}{hint}")

    def _post(self, path: str, body: dict[str, Any], **kw: Any) -> requests.Response:
        try:
            r = requests.post(f"{self.url}{path}", json=body, headers=self.headers,
                              timeout=kw.pop("timeout", self.timeout), **kw)
        except requests.RequestException as e:
            raise self._err(path, e) from e
        if r.status_code >= 400:
            try:
                err = r.json().get("error")
                msg = err.get("message") if isinstance(err, dict) else (err or r.text)
            except ValueError:
                msg = r.text
            raise LLMError(f"LM Studio {path} returned {r.status_code}: {str(msg)[:400]}")
        return r

    def version(self) -> str:
        try:
            r = requests.get(f"{self.url}/v1/models", headers=self.headers, timeout=5)
            r.raise_for_status()
            return "server"
        except requests.RequestException as e:
            raise self._err("/v1/models", e) from e

    def models(self) -> list[dict[str, Any]]:
        """Downloaded models as Ask's model rows (name with the lmstudio: prefix)."""
        items: list[dict[str, Any]] = []
        native = True
        try:
            r = requests.get(f"{self.url}/api/v0/models", headers=self.headers, timeout=10)
            if r.status_code == 404:
                native = False
            else:
                r.raise_for_status()
                items = r.json().get("data") or []
        except ValueError:
            native = False
        except requests.RequestException as e:
            raise self._err("/api/v0/models", e) from e
        if not native:
            try:
                r = requests.get(f"{self.url}/v1/models", headers=self.headers, timeout=10)
                r.raise_for_status()
                items = r.json().get("data") or []
            except (requests.RequestException, ValueError) as e:
                raise self._err("/v1/models", e) from e
        out = []
        for m in items:
            mid = m.get("id") or ""
            if not mid:
                continue
            typ = (m.get("type") or ("embeddings" if "embed" in mid.lower() else "llm")).lower()
            caps: list[str]
            if typ.startswith("embed"):
                caps, kind = ["embedding"], "embedding"
            else:
                kind = "chat"
                caps = ["completion"]
                have = m.get("capabilities")
                # Older LM Studio builds don't list capabilities: assume tool use works (most
                # current models support it), as the server accepts `tools` for any model.
                if have is None or "tool_use" in have or "tools" in have:
                    caps.append("tools")
                if typ == "vlm" or (have and "vision" in have):
                    caps.append("vision")
            out.append({"name": PREFIX + mid, "label": mid, "provider": LMSTUDIO, "size": 0, "kind": kind,
                        "capabilities": caps, "parameter_size": m.get("quantization") or "",
                        "loaded": m.get("state") == "loaded" if "state" in m else None,
                        "context": m.get("loaded_context_length") or m.get("max_context_length")})
        return sorted(out, key=lambda m: m["name"].lower())

    def loaded(self) -> list[dict[str, Any]]:
        """Models LM Studio has in memory now (Performance page). Empty when it can't tell."""
        r = requests.get(f"{self.url}/api/v0/models", headers=self.headers, timeout=3)
        r.raise_for_status()
        return [{"name": m.get("id"), "type": m.get("type"), "quant": m.get("quantization"),
                 "context": m.get("loaded_context_length") or m.get("max_context_length"),
                 "max_context": m.get("max_context_length"), "arch": m.get("arch"),
                 "size": m.get("size_bytes") or m.get("size")}
                for m in r.json().get("data") or [] if m.get("state") == "loaded"]

    def embed(self, model: str, texts: list[str], keep_alive: str = "10m") -> list[list[float]]:
        body: dict[str, Any] = {"model": bare(model), "input": texts}
        ttl = keep_alive_seconds(keep_alive)
        if ttl:
            body["ttl"] = ttl
        data = self._post("/v1/embeddings", body).json().get("data") or []
        vecs = [d.get("embedding") for d in sorted(data, key=lambda d: d.get("index", 0))]
        if len(vecs) != len(texts) or any(v is None for v in vecs):
            raise LLMError(f"LM Studio returned {len(vecs)} embeddings for {len(texts)} inputs")
        return vecs

    @staticmethod
    def to_openai(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Ollama-style messages to OpenAI ones: tool calls get ids and JSON-string arguments,
        and each tool result is matched to its call by order (Ollama names it, OpenAI needs the id)."""
        out: list[dict[str, Any]] = []
        pending: list[tuple[str, str]] = []          # (id, name) of calls not answered yet
        n = 0
        for m in messages:
            role = m.get("role")
            if role == "assistant" and m.get("tool_calls"):
                calls = []
                for c in m["tool_calls"]:
                    f = c.get("function") or {}
                    n += 1
                    cid = c.get("id") or f"call_{n}"
                    args = f.get("arguments")
                    calls.append({"id": cid, "type": "function",
                                  "function": {"name": f.get("name", ""),
                                               "arguments": args if isinstance(args, str) else json.dumps(args or {})}})
                    pending.append((cid, f.get("name", "")))
                out.append({"role": "assistant", "content": m.get("content") or "", "tool_calls": calls})
            elif role == "tool":
                name = m.get("tool_name") or m.get("name") or ""
                i = next((k for k, p in enumerate(pending) if p[1] == name), 0 if pending else -1)
                cid = pending.pop(i)[0] if i >= 0 else m.get("tool_call_id") or f"call_{n}"
                out.append({"role": "tool", "tool_call_id": cid, "content": str(m.get("content") or "")})
            else:
                out.append({"role": role, "content": m.get("content") or ""})
        return out

    def chat(self, model: str, messages: list[dict[str, str]], *, options: dict[str, Any],
             think: bool = False, keep_alive: str = "10m") -> str:
        return "".join((o.get("message") or {}).get("content") or ""
                       for o in self.chat_stream(model, messages, options=options, think=think, keep_alive=keep_alive))

    def chat_stream(self, model: str, messages: list[dict[str, Any]], *, options: dict[str, Any],
                    think: bool = False, keep_alive: str = "10m",
                    tools: list[dict[str, Any]] | None = None) -> Iterator[dict[str, Any]]:
        """Stream /v1/chat/completions, yielding Ollama-shaped objects. Reasoning (the
        `reasoning_content` / `reasoning` field, or a <think> block in the text) is sent as
        `thinking` when Show model thinking is on, and dropped when it is off. num_ctx is not
        sent: LM Studio sets the context length when it loads the model."""
        body: dict[str, Any] = {"model": bare(model), "messages": self.to_openai(messages), "stream": True,
                                "stream_options": {"include_usage": True}}
        if "temperature" in options:
            body["temperature"] = options["temperature"]
        if tools:
            body["tools"] = tools
        ttl = keep_alive_seconds(keep_alive)
        if ttl:
            body["ttl"] = ttl
        t0 = time.time()
        first: float | None = None
        usage: dict[str, Any] = {}
        calls: dict[int, dict[str, Any]] = {}
        in_think = False
        buf = ""                                     # text held back while a <think> tag may be starting
        finish = None

        def text_parts(chunk: str) -> list[tuple[str, str]]:
            """Split content into ('content'|'thinking', text) around <think> ... </think>."""
            nonlocal in_think, buf
            s, parts = buf + chunk, []
            buf = ""
            while s:
                tag = "</think>" if in_think else "<think>"
                i = s.find(tag)
                if i < 0:
                    # Keep a possible half tag at the end for the next chunk.
                    keep = next((k for k in range(min(len(tag) - 1, len(s)), 0, -1) if tag.startswith(s[-k:])), 0)
                    if keep:
                        s, buf = s[:-keep], s[-keep:]
                    if s:
                        parts.append(("thinking" if in_think else "content", s))
                    break
                if i:
                    parts.append(("thinking" if in_think else "content", s[:i]))
                s = s[i + len(tag):]
                in_think = not in_think
            return parts

        r = self._post("/v1/chat/completions", body, stream=True, timeout=(10, self.timeout))
        with r:
            for raw in r.iter_lines():
                if not raw:
                    continue
                line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except ValueError:
                    continue
                if obj.get("error"):
                    err = obj["error"]
                    raise LLMError("LM Studio: " + (err.get("message") if isinstance(err, dict) else str(err)))
                if obj.get("usage"):
                    usage = obj["usage"]
                for ch in obj.get("choices") or []:
                    d = ch.get("delta") or {}
                    msg: dict[str, Any] = {}
                    reasoning = d.get("reasoning_content") or d.get("reasoning")
                    if reasoning and think:
                        msg["thinking"] = reasoning
                    if d.get("content"):
                        for kind, txt in text_parts(d["content"]):
                            if kind == "content":
                                msg["content"] = msg.get("content", "") + txt
                            elif think:
                                msg["thinking"] = msg.get("thinking", "") + txt
                    for tc in d.get("tool_calls") or []:
                        c = calls.setdefault(tc.get("index", len(calls)), {"id": "", "name": "", "args": ""})
                        f = tc.get("function") or {}
                        c["id"] = tc.get("id") or c["id"]
                        c["name"] += f.get("name") or ""
                        c["args"] += f.get("arguments") or ""
                    if ch.get("finish_reason"):
                        finish = ch["finish_reason"]
                    if msg:
                        if first is None:
                            first = time.time()
                        yield {"message": msg, "done": False}
        if buf and not in_think:
            yield {"message": {"content": buf}, "done": False}
        end = time.time()
        final: dict[str, Any] = {"message": {"content": ""}, "done": True, "done_reason": finish or "stop"}
        if calls:
            out = []
            for _, c in sorted(calls.items()):
                try:
                    args = json.loads(c["args"]) if c["args"].strip() else {}
                except ValueError:
                    args = {}
                out.append({"id": c["id"], "function": {"name": c["name"], "arguments": args}})
            final["message"]["tool_calls"] = out
        ev = usage.get("completion_tokens") or 0
        final.update({"eval_count": ev, "prompt_eval_count": usage.get("prompt_tokens"),
                      "eval_duration": int((end - (first or t0)) * 1e9) if ev else 0,
                      "total_duration": int((end - t0) * 1e9)})
        yield final


# ---------------------------------------------------------------------------------------------
# The router


class Models:
    """One client for both providers; each call goes to the provider of its model."""

    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.on = {OLLAMA: bool(cfg.get("ollama_enabled", True)), LMSTUDIO: bool(cfg.get("lmstudio_enabled", False))}
        self._ollama: Ollama | None = None
        self._lms: LMStudio | None = None

    @property
    def ollama(self) -> Ollama:
        if self._ollama is None:
            self._ollama = Ollama(reach(self.cfg["ollama_url"]))
        return self._ollama

    @property
    def lmstudio(self) -> LMStudio:
        if self._lms is None:
            self._lms = LMStudio(self.cfg.get("lmstudio_url") or "http://127.0.0.1:1234",
                                 api_key=os.environ.get("ASK_LMSTUDIO_API_KEY", ""))
        return self._lms

    def _for(self, model: str) -> Any:
        p = provider_of(model)
        if not self.on[p]:
            other = LABELS[LMSTUDIO if p == OLLAMA else OLLAMA]
            raise LLMError(f"{LABELS[p]} is turned off in Settings, so the model {bare(model)} can't be used. "
                           f"Turn {LABELS[p]} on, or pick an {other} model.")
        return self.lmstudio if p == LMSTUDIO else self.ollama

    def models(self) -> dict[str, Any]:
        """{"ok", "models", "providers": {name: {"on", "ok", "url", "version"|"error", "count"}}}.
        ok = at least one provider that is on answered."""
        res: dict[str, Any] = {"ok": False, "models": [], "providers": {}}
        for p, cli in ((OLLAMA, lambda: self.ollama), (LMSTUDIO, lambda: self.lmstudio)):
            info: dict[str, Any] = {"on": self.on[p], "label": LABELS[p]}
            res["providers"][p] = info
            if not self.on[p]:
                continue
            c = cli()
            info["url"] = c.url
            try:
                info["version"] = c.version()
                ms = c.models()
                for m in ms:
                    m.setdefault("provider", p)
                res["models"] += ms
                info.update(ok=True, count=len(ms))
                res["ok"] = True
            except OllamaError as e:
                info.update(ok=False, error=str(e))
        if not any(self.on.values()):
            res["error"] = "Ollama and LM Studio are both turned off in Settings."
        elif not res["ok"]:
            res["error"] = " ".join(i.get("error", "") for i in res["providers"].values() if i["on"])
        return res

    def embed(self, model: str, texts: list[str], keep_alive: str = "10m") -> list[list[float]]:
        return self._for(model).embed(model, texts, keep_alive=keep_alive)

    def chat(self, model: str, messages: list[dict[str, str]], *, options: dict[str, Any],
             think: bool = False, keep_alive: str = "10m") -> str:
        return self._for(model).chat(model, messages, options=options, think=think, keep_alive=keep_alive)

    def chat_stream(self, model: str, messages: list[dict[str, Any]], *, options: dict[str, Any],
                    think: bool = False, keep_alive: str = "10m",
                    tools: list[dict[str, Any]] | None = None) -> Iterator[dict[str, Any]]:
        return self._for(model).chat_stream(model, messages, options=options, think=think,
                                            keep_alive=keep_alive, tools=tools)


def client(cfg: dict[str, Any]) -> Models:
    return Models(cfg)


def check(provider: str, url: str) -> dict[str, Any]:
    """Settings page Test connection for one provider at an URL (not saved yet)."""
    c: Any = LMStudio(url) if provider == LMSTUDIO else Ollama(reach(url))
    try:
        ms = c.models()
        for m in ms:
            m.setdefault("provider", provider)
        return {"ok": True, "provider": provider, "url": c.url, "version": c.version(), "models": ms}
    except OllamaError as e:
        return {"ok": False, "provider": provider, "url": c.url, "error": str(e), "models": []}
