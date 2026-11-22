"""A stand-in Streamable HTTP MCP server, shaped like the draw.io MCP container
(jgraph/drawio-mcp): POST /mcp, an Mcp-Session-Id from initialize, answers as an event
stream (SSE) or, with ?json=1 in the URL, plain JSON.

    python ask/scripts/fake_mcp_http.py 13001      # http://127.0.0.1:13001/mcp

Tools: create_diagram (answers with an "Open in draw.io" link), search_shapes.
POST /_forget drops every session (a restarted server), to test that Ask starts a new one."""

from __future__ import annotations

import base64
import json
import secrets
import zlib
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote

SESSIONS: set[str] = set()
CALLS: list[dict] = []
TOOLS = [
    {"name": "create_diagram", "description": "Create a draw.io diagram from Mermaid or draw.io XML.",
     "inputSchema": {"type": "object", "properties": {"content": {"type": "string"}, "format": {"type": "string"}},
                     "required": ["content"]}},
    {"name": "search_shapes", "description": "Search draw.io shapes.", "annotations": {"readOnlyHint": True},
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}}},
]


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, msg, sse, headers=None, code=200):
        body = (b"event: message\ndata: " + json.dumps(msg).encode() + b"\n\n") if sse else json.dumps(msg).encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/event-stream" if sse else "application/json")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path == "/_forget":
            SESSIONS.clear()
            self.send_response(204)
            self.end_headers()
            return
        if not self.path.startswith("/mcp"):
            self.send_response(404)
            self.end_headers()
            return
        sse = "json=1" not in self.path
        if "text/event-stream" not in (self.headers.get("Accept") or ""):
            self.send_response(406)
            self.end_headers()
            return
        msg = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        method, rid = msg.get("method"), msg.get("id")
        if method == "initialize":
            sid = secrets.token_hex(8)
            SESSIONS.add(sid)
            return self._send({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                "serverInfo": {"name": "drawio-mcp", "version": "1.0.3"}}}, sse, {"Mcp-Session-Id": sid})
        if self.headers.get("Mcp-Session-Id") not in SESSIONS:
            self.send_response(404)
            self.end_headers()
            return
        if rid is None:                      # a notification
            self.send_response(202)
            self.end_headers()
            return
        if method == "tools/list":
            return self._send({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}, sse)
        if method == "tools/call":
            p = msg.get("params") or {}
            CALLS.append(p)
            if p.get("name") == "create_diagram":
                content = (p.get("arguments") or {}).get("content", "")
                if "syntax error" in content:
                    return self._send({"jsonrpc": "2.0", "id": rid, "result": {
                        "isError": True, "content": [{"type": "text", "text": "Mermaid syntax error on line 1"}]}}, sse)
                if content.lstrip().startswith("<"):        # draw.io XML: #R<compressed>, as the real server does
                    z = zlib.compressobj(9, zlib.DEFLATED, -15)
                    packed = base64.b64encode(z.compress(quote(content, safe="").encode()) + z.flush()).decode()
                    link = "https://app.diagrams.net/?pv=0&grid=0#R" + quote(packed, safe="")
                else:
                    link = "https://app.diagrams.net/#create=" + quote(json.dumps({"type": "mermaid", "data": content}))
                return self._send({"jsonrpc": "2.0", "id": rid, "result": {"content": [
                    {"type": "text", "text": f"Diagram created. Open in draw.io: {link}"},
                    {"type": "resource", "resource": {"uri": "ui://drawio/viewer", "mimeType": "text/html", "text": "<html>viewer</html>"}},
                ]}}, sse)
            if p.get("name") == "search_shapes":
                return self._send({"jsonrpc": "2.0", "id": rid, "result": {"content": [
                    {"type": "text", "text": "shape=mxgraph.aws4.ec2;"}]}}, sse)
            return self._send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": "Unknown tool"}}, sse)
        self._send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "Method not found"}}, sse)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 13001
    print(f"fake HTTP MCP on http://127.0.0.1:{port}/mcp")
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
