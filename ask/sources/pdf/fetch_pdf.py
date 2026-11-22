"""pdf source plug-in: read PDF files (guides, design documents, runbooks, white
papers) from a folder or from the uploads of the Knowledge sources page, and print
one JSON document per PDF for the vCommunity Ask indexer.

Text comes from the PDF itself with pypdf (BSD). Pages with no text layer (scanned
pages, photographed documents) are read with Tesseract OCR: each page is rendered to
an image with pdftoppm (poppler-utils) and passed to the `tesseract` command. Both
come with the Ask image and run offline.

Each page becomes a "## Page N" section, so answers can name the page and the source
card links straight to it (/files/<source>/<file>.pdf#page=N).

"Split into documents" (option split) cuts a big PDF into several documents: one per
bookmark (chapter) at the chosen bookmark level, or every N pages. Answers take at most
2 sections from one document, so a 9,000-page manual as one document could only ever
give 2 pages to an answer. Page numbers stay those of the whole PDF.

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
JUNK_TITLES = {"untitled", "untitled document", "title", "document", "none", "pdf", "microsoft word"}


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


def read_outline(reader: Any, max_level: int = 4) -> list[list[Any]]:
    """The PDF's bookmarks as [[level, page (0-based), title], ...] in reading order."""
    out: list[list[Any]] = []

    def walk(items: Any, level: int) -> None:
        for item in items:
            if isinstance(item, list):
                if level < max_level:
                    walk(item, level + 1)
                continue
            try:
                page = reader.get_destination_page_number(item)
            except Exception:  # noqa: BLE001 - a broken bookmark is just skipped
                continue
            if page is None or page < 0:
                continue
            title = re.sub(r"\s+", " ", str(getattr(item, "title", "") or "")).strip()
            out.append([level, int(page), title])

    try:
        walk(reader.outline, 1)
    except Exception as e:  # noqa: BLE001
        log(f"  bookmarks could not be read ({e.__class__.__name__}), splitting by pages")
    return out


def split_ranges(n_pages: int, mode: str, outline: list[list[Any]], level: int,
                 per_doc: int) -> list[tuple[int, int, str, str]]:
    """Page ranges [(first, last, name, part)], 1-based and inclusive, covering pages 1..n_pages.
    part is "part 2 of 5" when a chapter is longer than per_doc pages, else ""."""
    per_doc = max(1, per_doc)
    starts: list[tuple[int, str]] = []
    if mode == "chapters":
        seen: set[int] = set()
        for lvl, page, title in sorted((b for b in outline if b[0] <= level), key=lambda b: (b[1], b[0])):
            if page < n_pages and page not in seen:     # first bookmark on a page names it
                seen.add(page)
                starts.append((page + 1, title))
    if not starts:
        starts = [(1, "")]
    elif starts[0][0] > 1:
        starts.insert(0, (1, "Front matter"))
    out: list[tuple[int, int, str, str]] = []
    for i, (first, name) in enumerate(starts):
        last = (starts[i + 1][0] - 1) if i + 1 < len(starts) else n_pages
        parts = list(range(first, last + 1, per_doc))
        for j, a in enumerate(parts):
            b = min(a + per_doc - 1, last)
            out.append((a, b, name, f"part {j + 1} of {len(parts)}" if name and len(parts) > 1 else ""))
    return out


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
    max_pages = int(float(opts.get("max_pages_per_file") or 10000))
    split = (opts.get("split") or "off").strip()
    split_level = max(1, int(float(opts.get("split_level") or 1)))
    split_pages = max(1, int(float(opts.get("split_pages") or 50)))

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
    # Max pages is not part of the key: raising it reads only the new pages, lowering it
    # uses fewer of the cached ones. Caches from before (key ending in |<max pages>) still count.
    ocr_key = f"{ocr_mode}|{ocr.langs}|{ocr.dpi}"
    docs: list[dict[str, Any]] = []
    stats = {"files": 0, "pages": 0, "ocr_pages": 0, "cached_files": 0, "skipped": 0, "unfinished": 0}
    out_of_time = False
    pdfs = 0                                        # PDFs that gave at least one document

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
        key = str(cache.get("ocr_key") or "")
        if cache.get("v") != CACHE_VERSION or (key != ocr_key and not key.startswith(ocr_key + "|")):
            cache = {}
        elif key != ocr_key:
            cache["ocr_key"] = ocr_key
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
        if len(pages) >= n_pages:
            stats["cached_files"] += 1
            if not cache.get("complete"):
                cache["complete"] = True
                cpath.write_text(json.dumps(cache))
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
        pages, ocr_flags = pages[:n_pages], ocr_flags[:n_pages]     # Max pages lowered: use fewer
        complete = len(pages) >= n_pages
        if not complete:
            stats["unfinished"] += 1
        stats["files"] += 1
        stats["pages"] += len(pages)
        stats["ocr_pages"] += sum(1 for f in ocr_flags if f)

        title = cache.get("title") or ""
        if title.lower() in JUNK_TITLES or len(title) < 3:     # "untitled", "Microsoft Word - ..."
            title = path.stem.replace("_", " ").replace("-", " ").strip()
        if not any(pages):
            if complete:
                log(f"  {rel}: no text found" + ("" if ocr_mode != "off" else " (scanned? turn OCR on)"))
                stats["skipped"] += 1
            continue
        doc_id = re.sub(r"\.pdf$", "", rel, flags=re.I)
        made = 0

        def make_doc(did: str, dtitle: str, first: int, last: int) -> dict[str, Any] | None:
            parts = [f"# {dtitle}", ""]
            for i in range(first, last + 1):
                t = pages[i - 1]
                if t:
                    parts += [f"## Page {i}" + (" (OCR)" if ocr_flags[i - 1] else ""), "", t, ""]
            if len(parts) == 2:
                return None                       # no text on these pages
            text = "\n".join(parts).strip()
            meta = {"file": rel, "pages": n_pages, "ocr_pages": sum(1 for f in ocr_flags[first - 1:last] if f),
                    "complete": complete}
            if (first, last) != (1, n_pages):
                meta.update({"first_page": first, "last_page": last, "book": title})
            return {"id": did, "title": dtitle, "path": rel, "url": f"/files/{name}/{rel}", "text": text,
                    "meta": meta, "hash": hashlib.sha256(text.encode()).hexdigest()}

        if split in ("chapters", "pages"):
            if split == "chapters" and "outline" not in cache:
                cache["outline"] = read_outline(reader)
                cpath.write_text(json.dumps(cache))
            ranges = split_ranges(n_pages, split, cache.get("outline") or [], split_level, split_pages)
            if split == "chapters" and not cache.get("outline"):
                log(f"  {rel}: no bookmarks, split every {split_pages} pages")
            for first, last, label, part in ranges:
                if first > len(pages):
                    break                          # not read yet (time limit): next sync
                last = min(last, len(pages))
                span = ", ".join(x for x in (part, f"p. {first}" if first == last else f"pp. {first}-{last}") if x)
                dtitle = f"{title}: {label} ({span})" if label else f"{title} ({span})"
                d = make_doc(f"{doc_id}@p{first:05d}", dtitle, first, last)
                if d:
                    docs.append(d)
                    made += 1
            stats["parts"] = stats.get("parts", 0) + made
        else:
            d = make_doc(doc_id, title, 1, len(pages))
            if d:
                docs.append(d)
                made = 1
        pdfs += 1 if made else 0

    # Past the time limit every later file only uses what is cached (the page loop
    # reads nothing new), so finished files stay indexed and the rest follow next sync.
    partial = stats["unfinished"] > 0
    version = f"{pdfs} PDFs" + (f" in {len(docs)} documents" if split in ("chapters", "pages") else "") + f", {stats['pages']} pages" + (f", {stats['ocr_pages']} by OCR" if stats["ocr_pages"] else "")
    if partial:
        version += f" ({stats['unfinished']} to finish next sync)"
    log(version + "; " + ", ".join(f"{k} {v}" for k, v in stats.items() if v))
    new_state = {"signature": signature, "version": version, "partial": partial, "options": opts_hash}
    print(json.dumps({"version": version, "root": str(root), "unchanged": False,
                      "state": new_state, "documents": docs}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
