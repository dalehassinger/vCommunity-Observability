"""pdf source: "Split into documents" (sources/pdf/fetch_pdf.py split_ranges, read_outline
and the documents a split sync prints). Builds small PDFs with pypdf, no OCR, no Ollama.
Run from the ask/ folder:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
FETCH = HERE / "sources" / "pdf" / "fetch_pdf.py"
spec = importlib.util.spec_from_file_location("fetch_pdf", FETCH)
fetch_pdf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fetch_pdf)


def make_pdf(path: Path, pages: int, bookmarks: list[tuple[int, str, int | None]] = ()) -> None:
    """A PDF with `pages` blank pages, the page's text in its content stream, and bookmarks
    [(page 0-based, title, parent index or None)]."""
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    w = PdfWriter()
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    font_ref = w._add_object(font)
    for i in range(pages):
        page = w.add_blank_page(612, 792)
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 72 700 Td (Page text {i + 1} about vSAN and NSX) Tj ET".encode())
        page[NameObject("/Contents")] = w._add_object(stream)
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})})
    made = []
    for page, title, parent in bookmarks:
        made.append(w.add_outline_item(title, page, parent=made[parent] if parent is not None else None))
    with path.open("wb") as f:
        w.write(f)


class Ranges(unittest.TestCase):
    def test_off_and_pages(self):
        self.assertEqual(fetch_pdf.split_ranges(120, "pages", [], 1, 50),
                         [(1, 50, "", ""), (51, 100, "", ""), (101, 120, "", "")])
        self.assertEqual(fetch_pdf.split_ranges(10, "off", [[1, 3, "x"]], 1, 50), [(1, 10, "", "")])

    def test_chapters_front_matter_parts_and_levels(self):
        outline = [[1, 2, "Install"], [2, 5, "Hosts"], [1, 10, "Networking"], [1, 10, "Same page"], [1, 99, "Past end"]]
        r = fetch_pdf.split_ranges(30, "chapters", outline, 1, 15)
        self.assertEqual(r, [(1, 2, "Front matter", ""), (3, 10, "Install", ""),
                             (11, 25, "Networking", "part 1 of 2"), (26, 30, "Networking", "part 2 of 2")])
        r2 = fetch_pdf.split_ranges(30, "chapters", outline, 2, 100)
        self.assertEqual([x[:3] for x in r2], [(1, 2, "Front matter"), (3, 5, "Install"), (6, 10, "Hosts"),
                                               (11, 30, "Networking")])

    def test_chapters_without_bookmarks_use_pages(self):
        self.assertEqual(fetch_pdf.split_ranges(60, "chapters", [], 1, 25),
                         [(1, 25, "", ""), (26, 50, "", ""), (51, 60, "", "")])


class Sync(unittest.TestCase):
    def run_sync(self, folder: Path, work: Path, **opts) -> dict:
        env = dict(os.environ, ASK_SOURCE_OPTIONS=json.dumps({"location": "folder", "path": str(folder), "ocr": "off", **opts}),
                   ASK_SOURCE_WORKDIR=str(work), ASK_SOURCE_NAME="book")
        r = subprocess.run([sys.executable, str(FETCH)], env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_split_by_bookmarks_and_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            kn, work = Path(tmp) / "kn", Path(tmp) / "work"
            kn.mkdir()
            make_pdf(kn / "guide.pdf", 40, [(0, "Intro", None), (10, "Storage", None), (12, "vSAN", 1), (30, "Network", None)])
            whole = self.run_sync(kn, work)["documents"]
            self.assertEqual([d["id"] for d in whole], ["guide"])
            docs = self.run_sync(kn, work, split="chapters", split_pages=15)["documents"]
            self.assertEqual([d["id"] for d in docs], ["guide@p00001", "guide@p00011", "guide@p00026", "guide@p00031"])
            self.assertEqual(docs[1]["title"], "guide: Storage (part 1 of 2, pp. 11-25)")
            self.assertIn("## Page 11", docs[1]["text"])
            self.assertNotIn("## Page 26", docs[1]["text"])
            self.assertEqual((docs[3]["meta"]["first_page"], docs[3]["meta"]["last_page"]), (31, 40))
            self.assertEqual(docs[3]["url"], "/files/book/guide.pdf")
            # Turned off again: the one document, the same id and text as before.
            again = self.run_sync(kn, work, split="off")["documents"]
            self.assertEqual((again[0]["id"], again[0]["hash"]), ("guide", whole[0]["hash"]))

    def test_max_pages_raised_reads_on_from_the_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            kn, work = Path(tmp) / "kn", Path(tmp) / "work"
            kn.mkdir()
            make_pdf(kn / "m.pdf", 30)
            out = self.run_sync(kn, work, max_pages_per_file=10, split="pages", split_pages=10)
            self.assertEqual(len(out["documents"]), 1)
            out = self.run_sync(kn, work, max_pages_per_file=30, split="pages", split_pages=10)
            self.assertEqual([d["meta"]["first_page"] for d in out["documents"]], [1, 11, 21])


if __name__ == "__main__":
    unittest.main()
