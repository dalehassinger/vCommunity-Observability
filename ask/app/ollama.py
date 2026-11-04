"""Minimal Ollama client (native API, no SDK): version, models, embeddings and
streamed chat. Uses `requests`, which the first container already ships."""

from __future__ import annotations

import json
from typing import Any, Iterator

import requests


class OllamaError(RuntimeError):
    pass


class Ollama:
    def __init__(self, url: str, timeout: float = 600):
        self.url = url.rstrip("/")
        self.timeout = timeout

    def _post(self, path: str, body: dict[str, Any], **kw: Any) -> requests.Response:
        try:
            r = requests.post(f"{self.url}{path}", json=body, timeout=kw.pop("timeout", self.timeout), **kw)
        except requests.RequestException as e:
            raise OllamaError(f"Cannot reach Ollama at {self.url}: {e.__class__.__name__}: {e}") from e
        if r.status_code >= 400:
            try:
                msg = r.json().get("error") or r.text
            except ValueError:
                msg = r.text
            raise OllamaError(f"Ollama {path} returned {r.status_code}: {msg}")
        return r

    def version(self) -> str:
        try:
            r = requests.get(f"{self.url}/api/version", timeout=5)
            r.raise_for_status()
            return r.json().get("version", "?")
        except (requests.RequestException, ValueError) as e:
            raise OllamaError(f"Cannot reach Ollama at {self.url}: {e}") from e

    def models(self) -> list[dict[str, Any]]:
        """Installed models with a `kind` of 'embedding' or 'chat' (from Ollama's
        capabilities list when present, otherwise guessed from the name)."""
        try:
            r = requests.get(f"{self.url}/api/tags", timeout=10)
            r.raise_for_status()
            items = r.json().get("models", [])
        except (requests.RequestException, ValueError) as e:
            raise OllamaError(f"Cannot list models at {self.url}: {e}") from e
        out = []
        for m in items:
            caps = m.get("capabilities")
            name = m.get("name") or m.get("model")
            if caps is None:
                caps = ["embedding"] if "embed" in name else ["completion"]
            kind = "embedding" if "embedding" in caps else ("chat" if "completion" in caps else "other")
            out.append({"name": name, "size": m.get("size", 0), "kind": kind, "capabilities": caps,
                        "parameter_size": (m.get("details") or {}).get("parameter_size", "")})
        return sorted(out, key=lambda m: m["name"])

    def embed(self, model: str, texts: list[str], keep_alive: str = "10m") -> list[list[float]]:
        r = self._post("/api/embed", {"model": model, "input": texts, "truncate": True, "keep_alive": keep_alive})
        vecs = r.json().get("embeddings") or []
        if len(vecs) != len(texts):
            raise OllamaError(f"Ollama returned {len(vecs)} embeddings for {len(texts)} inputs")
        return vecs

    def chat(self, model: str, messages: list[dict[str, str]], *, options: dict[str, Any],
             think: bool = False, keep_alive: str = "10m") -> str:
        """Whole answer as one string (used for the short SQL-writing step of data questions)."""
        return "".join((o.get("message") or {}).get("content") or ""
                       for o in self.chat_stream(model, messages, options=options, think=think, keep_alive=keep_alive))

    def chat_stream(self, model: str, messages: list[dict[str, str]], *, options: dict[str, Any],
                    think: bool = False, keep_alive: str = "10m") -> Iterator[dict[str, Any]]:
        """Yield Ollama's streamed chat objects ({"message": {"content", "thinking"}, "done", ...})."""
        body: dict[str, Any] = {"model": model, "messages": messages, "stream": True,
                                "options": options, "keep_alive": keep_alive}
        # Always send it: thinking models (Qwen 3.x and others) otherwise think by default.
        body["think"] = bool(think)
        r = self._post("/api/chat", body, stream=True, timeout=(10, self.timeout))
        with r:
            for line in r.iter_lines():
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if obj.get("error"):
                    raise OllamaError(obj["error"])
                yield obj
