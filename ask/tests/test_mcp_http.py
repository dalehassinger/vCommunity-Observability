"""HTTP MCP servers (ask/app/mcp_http.py), for example the draw.io MCP container: setting
parsing, session start, tools list, calls with SSE and JSON answers, a forgotten session,
and the merge with the host agent's servers in mcp.servers / mcp.call.

Uses the stand-in server scripts/fake_mcp_http.py in a thread (skipped in the image, where
scripts/ is not copied).   Run from the ask/ folder:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from app import mcp, mcp_http, rag      # noqa: E402

try:
    import fake_mcp_http                # noqa: E402
except ImportError:
    fake_mcp_http = None


class Parse(unittest.TestCase):
    def test_lines_commas_headers_and_errors(self):
        ok, bad = mcp_http.parse("drawio = http://drawio-mcp:3001/mcp\n# comment\nx=https://h/mcp | Authorization: Bearer t")
        self.assertEqual([s["name"] for s in ok], ["drawio", "x"])
        self.assertEqual(ok[1]["headers"], {"Authorization": "Bearer t"})
        self.assertFalse(bad)
        ok, bad = mcp_http.parse("a=b, bad name=http://x, c=ftp://x, a=http://y")
        self.assertEqual([s["url"] for s in ok], ["http://y"])     # a=b is a bad URL, so a=http://y is the first a
        self.assertEqual(len(bad), 3)

    def test_local_drawio_editor(self):
        text = "Open in draw.io: https://app.diagrams.net/?pv=0&grid=0#R5ZpN and https://example.com/x"
        out = rag.local_drawio(text, "http://localhost:8082/")
        self.assertIn("http://localhost:8082/?pv=0&grid=0#R5ZpN", out)
        self.assertIn("https://example.com/x", out)
        self.assertEqual(rag.local_drawio(text, ""), text)                 # empty: public editor kept
        self.assertEqual(rag.local_drawio(text, "localhost:8082"), text)   # not a URL: kept
        self.assertEqual(rag.tool_links(out)[0]["title"], "Open in draw.io")

    def test_tool_links(self):
        links = rag.tool_links("Done. Open in draw.io: https://app.diagrams.net/#create=abc. Also https://example.com/x")
        self.assertEqual(links[0], {"url": "https://app.diagrams.net/#create=abc", "title": "Open in draw.io"})
        self.assertEqual(links[1]["url"], "https://example.com/x")


@unittest.skipIf(fake_mcp_http is None, "scripts/fake_mcp_http.py not found")
class Live(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), fake_mcp_http.H)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        mcp_http._servers.clear()

    def test_sse_list_and_call(self):
        http = f"drawio = {self.base}/mcp"
        info = mcp.servers("", http=http)
        self.assertTrue(info["ok"], info)
        row = info["servers"][0]
        self.assertEqual((row["name"], row["transport"], row["running"]), ("drawio", "http", True))
        self.assertEqual([t["name"] for t in row["tools"]], ["create_diagram", "search_shapes"])
        self.assertEqual(row["server"]["name"], "drawio-mcp")
        res = mcp.call("", "drawio", "create_diagram", {"content": "graph TD; A-->B"}, http=http)
        self.assertTrue(res["ok"], res)
        self.assertIn("https://app.diagrams.net/#create=", res["text"])
        self.assertNotIn("<html>", res["text"])                       # the viewer page is left out
        self.assertIn("(resource content left out)", res["text"])

    def test_json_answers_and_tool_error(self):
        http = f"d = {self.base}/mcp?json=1"
        self.assertEqual(len(mcp.servers("", http=http)["servers"][0]["tools"]), 2)
        res = mcp.call("", "d", "create_diagram", {"content": "syntax error"}, http=http)
        self.assertFalse(res["ok"])
        self.assertIn("syntax error", res["error"])

    def test_forgotten_session_is_restarted(self):
        http = f"drawio = {self.base}/mcp"
        mcp.servers("", http=http)
        requests.post(self.base + "/_forget", timeout=5)
        res = mcp.call("", "drawio", "search_shapes", {"query": "ec2"}, http=http)
        self.assertTrue(res["ok"], res)

    def test_unreachable_server_shows_error(self):
        info = mcp.servers("", http="gone = http://127.0.0.1:9/mcp")
        self.assertFalse(info["servers"][0]["running"])
        self.assertIn("Cannot reach the MCP server gone", info["servers"][0]["error"])

    def test_tools_for_the_model(self):
        http = f"drawio = {self.base}/mcp"
        info = mcp.servers("", http=http)
        tools, index = mcp.ollama_tools(info["servers"], ["drawio"], {})
        self.assertEqual(sorted(index), ["create_diagram", "search_shapes"])
        # create_diagram is not marked read-only: "Ask first" by default; search_shapes is allowed.
        self.assertEqual(mcp.policy_of({}, "drawio", index["create_diagram"][1]), "ask")
        self.assertEqual(mcp.policy_of({}, "drawio", index["search_shapes"][1]), "allow")


if __name__ == "__main__":
    unittest.main()
