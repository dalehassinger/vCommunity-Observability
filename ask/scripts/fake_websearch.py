"""A stand-in for Ollama's web search API (POST /api/web_search, /api/web_fetch), for
testing Ask's web search without internet or an API key.

    python3 ask/scripts/fake_websearch.py          # listens on 127.0.0.1:11556

Then set the Web search API URL (Settings) or ASK_WEB_SEARCH_URL to
http://127.0.0.1:11556 and OLLAMA_API_KEY to any value except "bad"."""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.headers.get("Authorization", "") in ("", "Bearer ", "Bearer bad"):
            return self.reply(401, {"error": "unauthorized"})
        if self.path == "/api/web_search":
            q = body.get("query", "")
            n = int(body.get("max_results") or 5)
            return self.reply(200, {"results": [
                {"title": f"Result {i} for {q}", "url": f"https://example.com/{i}",
                 "content": f"Stand-in page text {i} about {q}. PowerCLI 13.4 was released in 2026."}
                for i in range(1, min(n, 10) + 1)]})
        if self.path == "/api/web_fetch":
            return self.reply(200, {"title": "Fetched page", "content": f"Text of {body.get('url')}",
                                    "links": ["https://example.com/a", "https://example.com/b"]})
        self.reply(404, {"error": "not found"})


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 11556
    print(f"fake web search on 127.0.0.1:{port}", file=sys.stderr)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
