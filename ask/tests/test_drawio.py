"""draw.io diagrams (ask/app/drawio.py): links of the draw.io MCP server turned into .drawio
files for draw.io Desktop, the choice between the local web editor and the app
(setting drawio_open) in rag.run_tool, and the host agent's POST /drawio/open.
Run from the ask/ folder:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import base64
import importlib.util
import json
import sys
import tempfile
import threading
import unittest
import zlib
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

import requests

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "scripts"))

from app import drawio, mcp_http, rag      # noqa: E402

try:
    import fake_mcp_http                   # noqa: E402
except ImportError:
    fake_mcp_http = None

XML = '<mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/><mxCell id="2" value="vCenter" vertex="1" parent="1"/></root></mxGraphModel>'


def packed(xml: str) -> str:
    z = zlib.compressobj(9, zlib.DEFLATED, -15)
    return quote(base64.b64encode(z.compress(quote(xml, safe="").encode()) + z.flush()).decode(), safe="")


class Decode(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        drawio.init(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_r_link_and_create_links(self):
        self.assertEqual(drawio.from_link(f"https://app.diagrams.net/?pv=0&grid=0#R{packed(XML)}")["xml"], XML)
        spec = quote(json.dumps({"type": "xml", "data": XML}))
        self.assertEqual(drawio.from_link(f"https://app.diagrams.net/#create={spec}")["xml"], XML)
        mer = quote(json.dumps({"type": "mermaid", "data": "graph TD; A-->B"}))
        self.assertEqual(drawio.from_link(f"https://app.diagrams.net/#create={mer}"), {"type": "mermaid"})
        self.assertIsNone(drawio.from_link("https://app.diagrams.net/#Rnot-base64!!"))
        self.assertIsNone(drawio.from_link("https://example.com/page"))

    def test_to_files_saves_and_replaces(self):
        text = f"Diagram created. Open in draw.io: https://app.diagrams.net/?pv=0&grid=0#R{packed(XML)}."
        out, links, notes = drawio.to_files(text, "VCF management domain", "desktop")
        name = links[0]["drawio"]
        self.assertTrue(name.endswith("-vcf-management-domain.drawio"), name)
        self.assertIn(f"/diagrams/{name}.", out)
        self.assertNotIn("app.diagrams.net", out)
        body = drawio.path(name).read_text()
        self.assertTrue(body.startswith("<mxfile") and XML in body and 'name="VCF management domain"' in body)
        self.assertEqual(links[1]["url"], f"/diagrams/{name}/web")
        self.assertEqual(links[2]["url"], f"/diagrams/{name}?download=1")
        # editor mode: the short web link for the model, web link first on the card
        out2, links2, _ = drawio.to_files(text, "x", "editor")
        self.assertIn("/web.", out2)
        self.assertTrue(links2[0]["url"].endswith("/web"))
        # the web editor link holds the same diagram, and the libraries for the sidebar
        link = drawio.editor_link(name, "http://localhost:8082/", ["My Lib.xml"], "http://localhost:8081")
        self.assertTrue(link.startswith("http://localhost:8082/?pv=0&grid=0&clibs=Uhttp%3A%2F%2Flocalhost%3A8081%2Fdrawio-libs%2FMy%2520Lib.xml"), link)
        self.assertIn(XML, drawio.from_link(link)["xml"])
        self.assertTrue(drawio.editor_link(name, "", None).startswith("https://app.diagrams.net/?pv=0&grid=0#R"))
        self.assertIn("[Open the diagram](/diagrams/" + name + ")", notes[0])
        self.assertIsNone(drawio.path("../x.drawio"))

    def test_mermaid_link_kept_with_a_note(self):
        mer = quote(json.dumps({"type": "mermaid", "data": "graph TD; A-->B"}))
        text = f"Open in draw.io: https://app.diagrams.net/#create={mer}"
        out, links, notes = drawio.to_files(text, "", "desktop")
        self.assertEqual((out, links), (text, []))
        self.assertIn("draw.io XML", notes[0])


@unittest.skipIf(fake_mcp_http is None, "scripts/fake_mcp_http.py not found")
class RunTool(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), fake_mcp_http.H)
        cls.http = f"drawio = http://127.0.0.1:{cls.srv.server_address[1]}/mcp"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        mcp_http._servers.clear()
        self.tmp = tempfile.TemporaryDirectory()
        drawio.init(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def run_tool(self, mode: str) -> dict:
        cfg = {"agent_url": "", "mcp_http_servers": self.http, "mcp_tools": json.dumps({"drawio/create_diagram": "allow"}),
               "drawio_editor_url": "http://localhost:8082", "drawio_open": mode, "mcp_result_chars": 16000}
        tool = {"name": "create_diagram", "annotations": {}}
        call = {"function": {"name": "create_diagram", "arguments": {"content": XML, "title": "Mgmt domain"}}}
        card: dict = {"n": 1, "status": "pending"}
        list(rag.run_tool(cfg, "c1", call, card, {"create_diagram": ("drawio", tool)}, True))
        return card

    def test_editor_mode_links_local_editor_and_offers_desktop(self):
        card = self.run_tool("editor")
        self.assertEqual(card["status"], "ok", card)
        self.assertIn("/web", card["_for_model"])
        self.assertNotIn("#R", card["_for_model"])
        titles = [l["title"] for l in card["links"]]
        self.assertEqual(titles[:2], ["Open in draw.io (local web editor)", "Open in draw.io Desktop"])

    def test_desktop_mode_gives_the_model_the_file(self):
        card = self.run_tool("desktop")
        self.assertIn("/diagrams/", card["_for_model"])
        self.assertNotIn("#R", card["_for_model"])
        self.assertEqual(card["links"][0]["title"], "Open in draw.io Desktop")
        self.assertIn("Open in draw.io (local web editor)", [l["title"] for l in card["links"]])


    def test_library_shapes_are_expanded_before_the_call(self):
        from app import drawio_libs
        import test_drawio_libs
        lib = tempfile.TemporaryDirectory()
        old = drawio_libs.DIR
        try:
            drawio_libs.DIR = Path(lib.name)
            drawio_libs._cache.update(key=None, libs=[])
            test_drawio_libs.write_lib(Path(lib.name))
            cfg = {"agent_url": "", "mcp_http_servers": self.http, "mcp_tools": json.dumps({"drawio/create_diagram": "allow"}),
                   "drawio_editor_url": "", "drawio_open": "editor", "mcp_result_chars": 16000}
            content = ('<mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/><mxCell id="v" value="VC" '
                       'style="lib=vCenter Server;" vertex="1" parent="1"><mxGeometry x="10" y="10" as="geometry"/></mxCell>'
                       '<mxCell id="q" style="lib=Nope;" vertex="1" parent="1"><mxGeometry as="geometry"/></mxCell></root></mxGraphModel>')
            call = {"function": {"name": "create_diagram", "arguments": {"content": content}}}
            card: dict = {"n": 1, "status": "pending"}
            list(rag.run_tool(cfg, "c1", call, card, {"create_diagram": ("drawio", {"name": "create_diagram"})}, True))
            sent = fake_mcp_http.CALLS[-1]["arguments"]["content"]
            self.assertIn("shape=stencil(abc)", sent)
            self.assertEqual(card["arguments"]["content"], content)          # the card shows what the model wrote
            self.assertIn("Your draw.io shapes used: 1", card["note"])
            self.assertIn("Not in the shape library", card["_for_model"])
            self.assertIn("shape library", rag.tool_messages({"system_prompt": "s", "history_turns": 0}, [], "q", [], ["drawio"])[0]["content"])
        finally:
            drawio_libs.DIR = old
            drawio_libs._cache.update(key=None, libs=[])
            lib.cleanup()


class Agent(unittest.TestCase):
    def test_drawio_open_saves_the_file(self):
        spec = importlib.util.spec_from_file_location("agent", HERE / "agent" / "ask-host-agent.py")
        agent = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(agent)
        with tempfile.TemporaryDirectory() as tmp:
            h = agent.make_handler(agent.Collector(60), "t" * 20, None, tmp, "draw.io-not-installed")
            srv = ThreadingHTTPServer(("127.0.0.1", 0), h)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            url = f"http://127.0.0.1:{srv.server_address[1]}/drawio/open"
            try:
                self.assertEqual(requests.post(url, json={"name": "a.drawio", "xml": XML}, timeout=5).status_code, 401)
                hdr = {"Authorization": "Bearer " + "t" * 20}
                self.assertEqual(requests.post(url, json={"name": "../a.drawio", "xml": XML}, headers=hdr, timeout=5).status_code, 400)
                d = requests.post(url, json={"name": "a.drawio", "xml": XML}, headers=hdr, timeout=30).json()
                self.assertEqual(Path(d["path"]).read_text(), XML)
                if sys.platform != "darwin":
                    self.assertFalse(d["ok"])
            finally:
                srv.shutdown()


if __name__ == "__main__":
    unittest.main()
