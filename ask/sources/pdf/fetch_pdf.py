"""pdf source plug-in: read PDF files (guides, design documents, runbooks, white
papers) from a folder or from the uploads of the Knowledge sources page, and print
one JSON document per PDF for the vCommunity Ask indexer.

Text comes from the PDF itself with pypdf (BSD). Pages with no text layer (scanned
pages, photographed documents) are read with Tesseract OCR: each page is rendered to
an image with pdftoppm (poppler-utils) and passed to the `tesseract` command. Both
come with the Ask image and run offline.

Each page becomes a "## Page N" section, so answers can name the page and the source
card links straight to it (/files/<source>/<file>.pdf#page=N).

Text and OCR results are cached per file (by content hash) in the work folder, so a
re-sync only reads new or changed PDFs. OCR is slow (a few seconds per page): the
plug-in stops before the core's time limit (ASK_SOURCE_TIME_LIMIT), indexes what it
has and carries on with the next sync."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

MIN_TEXT = 25           # a page with fewer characters of text is treated as scanned
CACHE_VERSION = 1


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def as_list(v: Any) -> list[str]:
    if v is None or v == "":
        return []
    if isinstance(v, str):
        return [s.strip() for s in v.splitlines() if s.strip()]
    return [str(s).strip() for s in v if str(s).strip()]


def clean(text: str) -> str:
    text = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)          # words split over two lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    # A line starting with '#' would become a markdown heading and split the chunk.
    text = re.sub(r"(?m)^(#+)", r"\\\1", text)
    return text.strip()


def file_sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


class Ocr:
    def __init__(self, langs: str, dpi: int):
        self.langs = langs or "eng"
        self.dpi = dpi
        self.tesseract = shutil.which("tesseract")
        self.pdftoppm = shutil.which("pdftoppm")
        self.available = bool(self.tesseract and self.pdftoppm)
        self.warned = False

    def page(self, pdf: Path, number: int) -> str:
        """OCR one page (1-based). Empty string on failure."""
        if not self.available:
            if not self.warned:
                log("OCR is not available (tesseract or pdftoppm missing): scanned pages are skipped")
                self.warned = True
            return ""
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp) / "page"
            r = subprocess.run([self.pdftoppm, "-r", str(self.dpi), "-f", str(number), "-l", str(number),
                                "-gray", "-png", "-singlefile", str(pdf), str(prefix)],
                               capture_output=True, text=True, timeout=300)
            img = prefix.with_suffix(".png")
            if r.returncode != 0 or not img.exists():
                log(f"  {pdf.name} page {number}: could not render ({r.stderr.strip()[:200]})")
                return ""
            r = subprocess.run([self.tesseract, str(img), "stdout", "-l", self.langs],
                               capture_output=True, text=True, timeout=600)
            if r.returncode != 0:
                msg = r.stderr.strip().splitlines()[-1:] or [""]
                log(f"  {pdf.name} page {number}: tesseract failed: {msg[0][:200]}")
                if "Failed loading language" in r.stderr or "Error opening data file" in r.stderr:
                    log(f"  install the language data for '{self.langs}' (see the README) or change OCR languages")
                    self.available = False
                return ""
            return r.stdout


def main() -> int:
    opts = json.loads(os.environ.get("ASK_SOURCE_OPTIONS") or "{}")
    work = Path(os.environ.get("ASK_SOURCE_WORKDIR") or "work")
    state = json.loads(os.environ.get("ASK_SOURCE_STATE") or "{}")
    full = bool(os.environ.get("ASK_SOURCE_FULL"))
    name = os.environ.get("ASK_SOURCE_NAME") or "pdf"
    limit = float(os.environ.get("ASK_SOURCE_TIME_LIMIT") or 0)
    deadline = time.time() + max(limit * 0.85, limit - 300) if limit > 0 else 0.0

    location = (opts.get("location") or "upload").strip()
    if location == "upload":
        root = Path(os.environ.get("ASK_SOURCE_UPLOAD_DIR") or work / "uploads")
        patterns = ["*.pdf"]
    else:
        root = Path((opts.get("path") or "/knowledge").strip())
        patterns = as_list(opts.get("pattern")) or ["**/*.pdf"]
    ocr_mode = (opts.get("ocr") or "auto").strip()
    ocr = Ocr((opts.get("ocr_languages") or "eng").strip(), int(float(opts.get("ocr_dpi") or 200)))
    max_pages = int(float(opts.get("max_pages_per_file") or 2000))

    if not root.is_dir():
        if location == "upload":
            files: list[Path] = []
        else:
            raise SystemExit(f"Folder {root} does not exist in the container (mount it, e.g. ./ask-knowledge -> /knowledge)")
    else:
        files = sorted({p for pat in patterns for p in root.glob(pat)
                        if p.is_file() and p.suffix.lower() == ".pdf" and not p.name.startswith(".")})
    log(f"{len(files)} PDF file(s) in {root}" + (f" (OCR: {ocr_mode}, languages {ocr.langs})" if ocr_mode != "off" else " (OCR off)"))

    opts_hash = hashlib.sha256(json.dumps(opts, sort_keys=True).encode()).hexdigest()[:16]
    signature = hashlib.sha256(json.dumps([opts_hash, [(str(p.relative_to(root)), p.stat().st_size, p.stat().st_mtime_ns)
                                                       for p in files]]).encode()).hexdigest()[:24]
    if not full and files and state.get("signature") == signature and not state.get("partial"):
        log("no change since last sync")
        print(json.dumps({"version": state.get("version", ""), "root": str(root), "unchanged": True,
                          "state": state, "documents": []}))
        return 0

    from pypdf import PdfReader  # imported here so "unchanged" answers stay fast

    cache_dir = work / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    ocr_key = f"{ocr_mode}|{ocr.langs}|{ocr.dpi}|{max_pages}"
    docs: list[dict[str, Any]] = []
    stats = {"files": 0, "pages": 0, "ocr_pages": 0, "cached_files": 0, "skipped": 0, "unfinished": 0}
    out_of_time = False

    for path in files:
        rel = str(path.relative_to(root))
        sha = file_sha(path)
        cpath = cache_dir / f"{sha}.json"
        cache: dict[str, Any] = {}
        if not full:
            try:
                cache = json.loads(cpath.read_text())
            except (OSError, ValueError):
                cache = {}
        if cache.get("v") != CACHE_VERSION or cache.get("ocr_key") != ocr_key:
            cache = {}
        try:
            reader = PdfReader(str(path))
            if reader.is_encrypted and not reader.decrypt(""):
                log(f"  {rel}: password protected, skipped")
                stats["skipped"] += 1
                continue
            n_pages = len(reader.pages)
        except Exception as e:  # noqa: BLE001 - a broken file must not stop the others
            log(f"  {rel}: cannot read ({e.__class__.__name__}: {str(e)[:200]}), skipped")
            stats["skipped"] += 1
            continue
        if n_pages > max_pages:
            log(f"  {rel}: {n_pages} pages, only the first {max_pages} are read (Max pages per file)")
            n_pages = max_pages
        pages: list[str] = cache.get("pages") or []
        ocr_flags: list[bool] = cache.get("ocr") or []
        if len(pages) >= n_pages and cache.get("complete"):
            stats["cached_files"] += 1
        else:
            if not cache:
                title = ""
                try:
                    title = ((reader.metadata or {}).get("/Title") or "").strip()
                except Exception:  # noqa: BLE001
                    pass
                cache = {"v": CACHE_VERSION, "ocr_key": ocr_key, "title": title, "pages": [], "ocr": []}
                pages, ocr_flags = cache["pages"], cache["ocr"]
            last_save = time.time()
            for i in range(len(pages), n_pages):
                if deadline and time.time() > deadline:
                    out_of_time = True
                    break
                try:
                    text = reader.pages[i].extract_text() or ""
                except Exception as e:  # noqa: BLE001
                    log(f"  {rel} page {i + 1}: text extraction failed ({e.__class__.__name__})")
                    text = ""
                did_ocr = False
                if ocr_mode == "always" or (ocr_mode == "auto" and len(text.strip()) < MIN_TEXT):
                    o = ocr.page(path, i + 1)
                    if o.strip() and (ocr_mode == "always" or len(o.strip()) > len(text.strip())):
                        text, did_ocr = o, True
                pages.append(clean(text))
                ocr_flags.append(did_ocr)
                if time.time() - last_save > 30:      # keep OCR work if the run is stopped
                    cpath.write_text(json.dumps(cache))
                    last_save = time.time()
            cache["complete"] = len(pages) >= n_pages
            cpath.write_text(json.dumps(cache))
            if out_of_time and not cache["complete"]:
                log(f"  {rel}: time limit almost reached, page {len(pages) + 1} on is read on the next sync")
        if not cache.get("complete"):
            stats["unfinished"] += 1
        stats["files"] += 1
        stats["pages"] += len(pages)
        stats["ocr_pages"] += sum(1 for f in ocr_flags if f)

        title = cache.get("title") or path.stem.replace("_", " ").replace("-", " ").strip()
        parts = [f"# {title}", ""]
        for i, t in enumerate(pages, 1):
            if t:
                parts += [f"## Page {i}" + (" (OCR)" if ocr_flags[i - 1] else ""), "", t, ""]
        text = "\n".join(parts).strip()
        if not any(pages):
            if cache.get("complete"):
                log(f"  {rel}: no text found" + ("" if ocr_mode != "off" else " (scanned? turn OCR on)"))
                stats["skipped"] += 1
            continue
        doc_id = re.sub(r"\.pdf$", "", rel, flags=re.I)
        docs.append({
            "id": doc_id, "title": title, "path": rel,
            "url": f"/files/{name}/{rel}",
            "text": text,
            "meta": {"file": rel, "pages": n_pages, "ocr_pages": sum(1 for f in ocr_flags if f),
                     "complete": bool(cache.get("complete"))},
            "hash": hashlib.sha256(text.encode()).hexdigest(),
        })

    # Past the time limit every later file only uses what is cached (the page loop
    # reads nothing new), so finished files stay indexed and the rest follow next sync.
    partial = stats["unfinished"] > 0
    version = f"{len(docs)} PDFs, {stats['pages']} pages" + (f", {stats['ocr_pages']} by OCR" if stats["ocr_pages"] else "")
    if partial:
        version += f" ({stats['unfinished']} to finish next sync)"
    log(version + "; " + ", ".join(f"{k} {v}" for k, v in stats.items() if v))
    new_state = {"signature": signature, "version": version, "partial": partial, "options": opts_hash}
    print(json.dumps({"version": version, "root": str(root), "unchanged": False,
                      "state": new_state, "documents": docs}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
