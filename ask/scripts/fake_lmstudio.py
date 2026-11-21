"""A stand-in for LM Studio's local server, for tests without LM Studio.

    python ask/scripts/fake_lmstudio.py            # listens on 127.0.0.1:12345

Only what vCommunity Ask uses: GET /api/v0/models (native list), GET /v1/models,
POST /v1/embeddings and streamed POST /v1/chat/completions (Server-Sent Events) with
tool calls split over several chunks, `reasoning_content` and usage at the end.
Embeddings are the same hashed bag-of-words vectors as fake_ollama.py."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DIM = 256
MODELS = [
    {"id": "google/gemma-4-31b", "object": "model", "type": "llm", "arch": "gemma4", "quantization": "4bit",
     "state": "loaded", "max_context_length": 131072, "loaded_context_length": 32768, "capabilities": ["tool_use"]},
    {"id": "qwen/qwen3-8b", "object": "model", "type": "llm", "quantization": "Q4_K_M", "state": "not-loaded",
     "max_context_length": 40960},
    {"id": "text-embedding-nomic-embed-text-v1.5", "object": "model", "type": "embeddings",
     "quantization": "Q4_K_M", "state": "not-loaded", "max_context_length": 2048},
]
LAST: dict = {}          # the last chat request body (tests read it from GET /_last)


def embed(text: str) -> list[float]:
    v = [0.0] * DIM
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        h = int(hashlib.md5(w.encode()).hexdigest(), 16)
        v[h % DIM] += 1.0 if (h >> 8) % 2 else -1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/api/v0/models":
            return self._json({"object": "list", "data": MODELS})
        if self.path == "/v1/models":
            return self._json({"object": "list", "data": [{"id": m["id"], "object": "model"} for m in MODELS]})
        if self.path == "/_last":
            return self._json(LAST)
        self._json({"error": "not found"}, 404)

    def _sse(self, obj):
        self.wfile.write(b"data: " + json.dumps(obj).encode() + b"\n\n")
        self.wfile.flush()

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        ids = {m["id"] for m in MODELS}
        if body.get("model") not in ids:
            return self._json({"error": {"message": f"Model {body.get('model')!r} not found"}}, 404)
        if self.path == "/v1/embeddings":
            inp = body.get("input")
            inp = [inp] if isinstance(inp, str) else inp
            return self._json({"object": "list", "data": [{"object": "embedding", "index": i, "embedding": embed(t)}
                                                          for i, t in enumerate(inp)][::-1]})   # out of order on purpose
        if self.path != "/v1/chat/completions":
            return self._json({"error": "not found"}, 404)
        LAST.clear()
        LAST.update(body)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        msgs = body["messages"]
        q = next((m["content"] for m in reversed(msgs) if m["role"] == "user"), "").lower()
        base = {"id": "chatcmpl-1", "object": "chat.completion.chunk", "model": body["model"]}

        def delta(d, finish=None):
            self._sse({**base, "choices": [{"index": 0, "delta": d, "finish_reason": finish}]})

        if body.get("tools") and msgs[-1]["role"] != "tool":
            tools = [t["function"] for t in body["tools"]]
            hit = [t for t in tools if any(len(w) > 3 and w in q for w in re.findall(r"[a-z]+", t["name"].lower()))]
            t = (hit or tools[:1])[0]
            args = json.dumps({k: "x" for k in list((t.get("parameters") or {}).get("properties") or {})[:1]})
            delta({"role": "assistant", "content": "Let me check."})
            delta({"tool_calls": [{"index": 0, "id": "call_abc", "type": "function", "function": {"name": t["name"], "arguments": ""}}]})
            half = len(args) // 2
            delta({"tool_calls": [{"index": 0, "function": {"arguments": args[:half]}}]})
            delta({"tool_calls": [{"index": 0, "function": {"arguments": args[half:]}}]})
            delta({}, "tool_calls")
        else:
            delta({"role": "assistant", "reasoning_content": "Thinking about the sources."})
            if "thinktag" in q:
                for part in ["<thi", "nk>hidden reasoning</th", "ink>Visible ", "answer."]:
                    delta({"content": part})
            else:
                text = "**LM Studio stand-in answer.** " + ("Tool said: " + msgs[-1]["content"][:80] if msgs[-1]["role"] == "tool"
                                                         else "The question was: " + q[:80]) + " [1]"
                for i in range(0, len(text), 12):
                    delta({"content": text[i:i + 12]})
            delta({}, "stop")
        self._sse({**base, "choices": [], "usage": {"prompt_tokens": 321, "completion_tokens": 42, "total_tokens": 363}})
        self.wfile.write(b"data: [DONE]\n\n")


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 12345
    print(f"fake LM Studio on http://127.0.0.1:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
