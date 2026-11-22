"""Your own draw.io shape libraries (ask/app/drawio_libs.py): reading <mxlibrary> files,
the shape list for the model, and swapping cells with style lib=<name> for the real shapes.
Run from the ask/ folder:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from html import escape
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import drawio, drawio_libs     # noqa: E402

ICON = ('<mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>'
        '<object id="vc" label="vCenter Server" concept="vCenter"><mxCell style="shape=stencil(abc);fillColor=#EDF8F1;" '
        'vertex="1" parent="1"><mxGeometry x="33" y="0" width="64" height="64" as="geometry"/></mxCell></object></root></mxGraphModel>')
CONTAINER = ('<mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>'
             '<mxCell id="md" value="Management Domain" style="swimlane;startSize=46;" vertex="1" parent="1">'
             '<mxGeometry width="380" height="300" as="geometry"/></mxCell>'
             '<mxCell id="mdi" value="" style="shape=stencil(xyz);" vertex="1" parent="md">'
             '<mxGeometry x="11" y="8" width="30" height="30" as="geometry"/></mxCell></root></mxGraphModel>')


def write_lib(folder: Path) -> None:
    items = [{"title": "vCenter Server", "w": 130, "h": 128, "xml": ICON},
             {"title": "Management Domain — Container", "w": 380, "h": 300, "xml": CONTAINER},
             {"title": "Logo", "w": 40, "h": 40, "data": "data:image/png;base64,AAAA"}]
    (folder / "VCF Library.xml").write_text("<mxlibrary>" + escape(json.dumps(items), quote=False) + "</mxlibrary>")
    (folder / "broken.xml").write_text("<notalibrary/>")


class Libs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        drawio_libs.DIR = Path(self.tmp.name)
        drawio_libs.TIDY = False             # tests of the swap itself; TidyLayout tests the grid
        drawio_libs._cache.update(key=None, libs=[])
        write_lib(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_and_note(self):
        s = {x["file"]: x for x in drawio_libs.summary()}
        self.assertEqual(s["VCF Library.xml"]["shapes"], 3)
        self.assertIn("not a draw.io library", s["broken.xml"]["error"])
        self.assertEqual(drawio_libs.files(), ["VCF Library.xml"])
        note = drawio_libs.note()
        self.assertIn("lib=<name>", note)
        self.assertIn("Management Domain — Container", note)
        self.assertIsNotNone(drawio_libs.find("management domain - container"))   # dashes and case don't matter

    def test_expand_icon_container_and_children(self):
        model = ('<mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>'
                 '<mxCell id="m1" value="Mgmt Domain A" style="lib=Management Domain — Container;" vertex="1" parent="1">'
                 '<mxGeometry x="100" y="50" width="400" height="320" as="geometry"/></mxCell>'
                 '<mxCell id="v1" value="vCenter A" style="lib=vCenter Server;fontSize=14;" vertex="1" parent="m1">'
                 '<mxGeometry x="20" y="60" width="64" height="64" as="geometry"/></mxCell>'
                 '<mxCell id="x1" value="?" style="lib=No Such Shape;" vertex="1" parent="1">'
                 '<mxGeometry x="0" y="0" width="10" height="10" as="geometry"/></mxCell>'
                 '</root></mxGraphModel>')
        out, used, unknown = drawio_libs.expand(model)
        self.assertEqual(used, ["Management Domain — Container", "vCenter Server"])
        self.assertEqual(unknown, ["No Such Shape"])
        root = ET.fromstring(out)
        cells = {(e.get("id")): e for e in root.iter() if e.get("id")}
        m1 = cells["m1"]
        self.assertTrue(m1.get("style").startswith("swimlane"))
        self.assertEqual(m1.get("value"), "Mgmt Domain A")
        g = m1.find("mxGeometry")
        self.assertEqual((g.get("x"), g.get("y"), g.get("width"), g.get("height")), ("100", "50", "400", "320"))
        self.assertEqual(cells["m1-1"].get("parent"), "m1")                   # header icon inside the container
        v1 = cells["v1"]                                                      # <object> kept, label = model's
        self.assertEqual((v1.tag, v1.get("label")), ("object", "vCenter A"))
        self.assertEqual(v1.find("mxCell").get("parent"), "m1")
        self.assertIn("shape=stencil(abc)", v1.find("mxCell").get("style"))
        self.assertNotIn("lib=", cells["x1"].get("style"))

    def test_cells_on_a_container_header_move_below_it(self):
        model = ('<mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>'
                 '<mxCell id="m" style="lib=Management Domain — Container;" vertex="1" parent="1"><mxGeometry width="300" height="300" as="geometry"/></mxCell>'
                 '<mxCell id="v" style="lib=vCenter Server;" vertex="1" parent="m"><mxGeometry x="20" y="10" width="64" height="64" as="geometry"/></mxCell>'
                 '</root></mxGraphModel>')
        root = ET.fromstring(drawio_libs.expand(model)[0])
        geo = {e.get("id"): (e if e.tag == "mxCell" else e.find("mxCell")).find("mxGeometry") for e in root.find("root")}
        self.assertEqual(geo["v"].get("y"), "54")          # header 46 + 8
        self.assertEqual(geo["m-1"].get("y"), "8")         # the container's own header icon stays

    def test_expand_args_and_untouched_xml(self):
        args = {"content": '<mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/><mxCell id="a" style="lib=Logo;" vertex="1" parent="1"><mxGeometry x="5" y="5" as="geometry"/></mxCell></root></mxGraphModel>',
                "format": "xml"}
        out, used, _ = drawio_libs.expand_args(args)
        self.assertIn("shape=image;image=data:image/png;base64,AAAA", out["content"])
        self.assertEqual(out["format"], "xml")
        self.assertEqual(drawio_libs.expand("not xml lib=x")[0], "not xml lib=x")
        plain = "<mxGraphModel><root/></mxGraphModel>"
        self.assertEqual(drawio_libs.expand(plain)[0], plain)


class TidyLayout(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        drawio_libs.DIR = Path(self.tmp.name)
        drawio_libs._cache.update(key=None, libs=[])
        drawio_libs.TIDY = True
        write_lib(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_rows_grid_and_container_fit(self):
        # Sloppy model layout: icons resized, rows 30 px apart, container too small.
        model = ('<mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>'
                 '<mxCell id="m" value="Management Domain" style="lib=Management Domain — Container;" vertex="1" parent="1"><mxGeometry x="10" y="10" width="200" height="150" as="geometry"/></mxCell>'
                 '<mxCell id="a" style="lib=vCenter Server;" vertex="1" parent="m"><mxGeometry x="20" y="60" width="80" height="80" as="geometry"/></mxCell>'
                 '<mxCell id="b" style="lib=vCenter Server;" vertex="1" parent="m"><mxGeometry x="110" y="70" width="80" height="80" as="geometry"/></mxCell>'
                 '<mxCell id="c" style="lib=vCenter Server;" vertex="1" parent="m"><mxGeometry x="20" y="130" width="80" height="80" as="geometry"/></mxCell>'
                 '</root></mxGraphModel>')
        root = ET.fromstring(drawio_libs.expand(model)[0])
        geo = {e.get("id"): (e if e.tag == "mxCell" else e.find("mxCell")).find("mxGeometry") for e in root.find("root")}
        xy = {k: (float(geo[k].get("x")), float(geo[k].get("y")), float(geo[k].get("width"))) for k in "abc"}
        self.assertEqual(xy["a"][1], xy["b"][1])                     # a and b share the first row
        self.assertGreaterEqual(xy["b"][0] - xy["a"][0], drawio_libs.SLOT)   # room for the names
        self.assertGreaterEqual(xy["c"][1] - xy["a"][1], drawio_libs.ICON + drawio_libs.LABEL)
        self.assertEqual(xy["a"][2], drawio_libs.ICON)                # icons back to 64
        self.assertGreaterEqual(float(geo["m"].get("height")), xy["c"][1] + drawio_libs.ICON + drawio_libs.LABEL)
        self.assertGreaterEqual(float(geo["m"].get("width")), xy["b"][0] + drawio_libs.ICON)


if __name__ == "__main__":
    unittest.main()
