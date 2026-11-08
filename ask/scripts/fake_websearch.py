"""A stand-in for Ollama's web search API (POST /api/web_search, /api/web_fetch) and the
Brave Search API (GET /res/v1/web/search), for testing Ask's web search without internet
or an API key.

    python3 ask/scripts/fake_websearch.py          # listens on 127.0.0.1:11556

Then set the Web search API URL (Settings) or ASK_WEB_SEARCH_URL to
http://127.0.0.1:11556 and OLLAMA_API_KEY to any value except "bad". For Brave set the
Brave Search API URL (or ASK_BRAVE_SEARCH_URL) the same way and BRAVE_API_KEY to anything
except "bad"."""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, obj, headers=None):
        body = json.dumps(obj).encode()
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path != "/res/v1/web/search":
            return self.reply(404, {"error": "not found"})
        if self.headers.get("X-Subscription-Token", "") in ("", "bad"):
            return self.reply(422, {"type": "ErrorResponse", "error": {"code": "SUBSCRIPTION_TOKEN_INVALID"}})
        qs = parse_qs(u.query)
        q, n = qs.get("q", [""])[0], int(qs.get("count", ["5"])[0])
        return self.reply(200, {"type": "search", "web": {"results": [
            {"title": f"Brave <strong>result</strong> {i} for {q}", "url": f"https://brave.example.com/{i}",
             "description": f"Stand-in Brave summary {i} about <strong>{q}</strong>.", "age": "2 days ago",
             "extra_snippets": [f"Extra snippet {i}a.", f"Extra snippet {i}b."]}
            for i in range(1, min(n, 20) + 1)]}},
            {"X-RateLimit-Limit": "1, 2000", "X-RateLimit-Remaining": "0, 1987", "X-RateLimit-Reset": "1, 1900000"})

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
