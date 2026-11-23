"""draw.io diagrams for the draw.io Desktop app.

The draw.io MCP server (jgraph/drawio-mcp) answers create_diagram with a link to the public
editor, with the whole diagram packed after the # (`#R<compressed XML>` or `#create=<JSON>`).
Ask unpacks that diagram, saves it as a .drawio file under <data_dir>/diagrams and hands the
model and the tool card a link to the file (/diagrams/<name>.drawio) instead. In the chat,
a click on such a link asks the host agent on the Mac to open the file in draw.io Desktop
(POST /ui/diagrams/<name>/open -> agent POST /drawio/open); without the agent the browser
downloads the file, which draw.io Desktop opens too. Nothing goes to the internet.

Setting drawio_open: "editor" (default) gives the model the web editor link (pointed at the
local draw.io editor, drawio_editor_url, when set); "desktop" gives it the file link. The tool
card offers both either way."""

from __future__ import annotations

import base64
import json
import re
import time
import zlib
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

DIR: Path | None = None
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,120}\.drawio$")
LINK_RE = re.compile(r"https?://[^\s<>\"')\]]+#(?:R|create=)[^\s<>\"')\]]+")
KEEP = 500                       # newest files kept


def init(folder: Path) -> None:
    global DIR
    DIR = Path(folder)


def _b64(text: str) -> bytes:
    text = re.sub(r"\s", "", text).replace("-", "+").replace("_", "/")
    return base64.b64decode(text + "=" * (-len(text) % 4))


def _inflate(raw: bytes) -> bytes:
    for wbits in (-15, 15, 31):              # raw deflate (draw.io), zlib, gzip
        try:
            return zlib.decompress(raw, wbits)
        except zlib.error:
            continue
    raise ValueError("not compressed data")


def _text(raw: bytes) -> str:
    s = raw.decode("utf-8", "replace")
    if not s.lstrip().startswith("<") and "%3C" in s[:20].upper():
        s = unquote(s)                        # draw.io compresses encodeURIComponent(xml)
    return s


def _xml_from_compressed(data: str) -> str:
    return _text(_inflate(_b64(unquote(data))))


def from_link(url: str) -> dict[str, Any] | None:
    """{"xml": ..., "type": "xml"} for a link holding a draw.io diagram, {"type": "mermaid"|"csv"|...}
    for one holding another format (the editor converts those; a file can't), None otherwise."""
    if "#" not in url:
        return None
    frag = url.split("#", 1)[1]
    try:
        if frag.startswith("R"):
            xml = _xml_from_compressed(frag[1:])
            return {"type": "xml", "xml": xml} if xml.lstrip().startswith("<") else None
        if frag.startswith("create="):
            spec = json.loads(unquote(frag[7:]))
            kind = str(spec.get("type") or "xml").lower()
            data = str(spec.get("data") or "")
            if kind != "xml":
                return {"type": kind}
            xml = _xml_from_compressed(data) if spec.get("compressed") else data
            return {"type": "xml", "xml": xml} if xml.lstrip().startswith("<") else None
    except (ValueError, TypeError, AttributeError):
        return None
    return None


def as_file(xml: str, title: str = "Page-1") -> str:
    """A .drawio file: <mxfile> as is, a bare <mxGraphModel> wrapped in one."""
    xml = xml.strip()
    if xml.startswith("<?xml"):
        xml = xml.split("?>", 1)[1].strip()
    if xml.startswith("<mxfile"):
        return xml
    return (f'<mxfile host="vCommunity Ask" type="device"><diagram id="ask" name="{escape(title[:60])}">'
            f"{xml}</diagram></mxfile>")


def _slug(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", text or "").strip("-").lower()
    return s[:50].strip("-") or "diagram"


def save(xml: str, title: str = "") -> str:
    """Write the diagram, return its file name."""
    if DIR is None:
        raise RuntimeError("diagram folder not set")
    DIR.mkdir(parents=True, exist_ok=True)
    name = f"{time.strftime('%Y%m%d-%H%M%S')}-{_slug(title)}.drawio"
    n = 1
    while (DIR / name).exists():
        n += 1
        name = f"{time.strftime('%Y%m%d-%H%M%S')}-{_slug(title)}-{n}.drawio"
    (DIR / name).write_text(as_file(xml, title or "Page-1"), encoding="utf-8")
    old = sorted(DIR.glob("*.drawio"))
    for p in old[:-KEEP]:
        try:
            p.unlink()
        except OSError:
            pass
    return name


def path(name: str) -> Path | None:
    if DIR is None or not NAME_RE.match(name or ""):
        return None
    p = DIR / name
    return p if p.is_file() else None


def title_from(args: dict[str, Any], question: str = "") -> str:
    for k in ("title", "name"):
        if isinstance(args.get(k), str) and args[k].strip():
            return args[k].strip()
    return " ".join((question or "").split()[:8])


def compress(xml: str) -> str:
    """draw.io's compressed form (#R links): base64 of raw deflate of the URI-encoded XML."""
    z = zlib.compressobj(9, zlib.DEFLATED, -15)
    return base64.b64encode(z.compress(quote(xml, safe="").encode()) + z.flush()).decode()


def editor_link(name: str, editor: str, libs: list[str] | None = None, base: str = "") -> str | None:
    """The web editor link for a saved diagram: <editor>/?...#R<diagram>. libs (files served by
    Ask at <base>/drawio-libs/<file>) open in the editor's sidebar (clibs)."""
    f = path(name)
    if not f:
        return None
    editor = (editor or "").strip().rstrip("/") or "https://app.diagrams.net"
    q = "pv=0&grid=0"
    public = re.search(r"//([^/]+\.)?(diagrams\.net|draw\.io)(:\d+)?(/|$)", editor + "/")
    if libs and not public:
        # The local editor container (jgraph/drawio) only lets the page fetch from its own address
        # (Content-Security-Policy connect-src 'self'), so compose mounts ./drawio-libs into it at
        # /drawio-libs and the libraries load from there. Documented form: clibs=U<encodeURIComponent(url)>;...
        q += "&clibs=" + ";".join("U" + quote(f"{editor}/drawio-libs/{quote(x)}", safe="") for x in libs)
    elif libs and base:
        base = base.rstrip("/")
        q += "&clibs=" + ";".join("U" + quote(f"{base}/drawio-libs/{quote(x)}", safe="") for x in libs)
        q += "&cors=" + quote(re.escape(base), safe="")
    return f"{editor}/?{q}#R{quote(compress(f.read_text(encoding='utf-8')), safe='')}"


def to_files(text: str, title: str = "", mode: str = "editor",
             editor_title: str = "Open in draw.io (local web editor)") -> tuple[str, list[dict[str, str]], list[str]]:
    """Save every diagram link in a tool result as a file. Returns (text with the links replaced
    by a short Ask link, card links, notes for the model). mode "desktop": the link is the file
    (/diagrams/<name>, opened in draw.io Desktop); "editor": /diagrams/<name>/web, which sends
    the browser to the web editor with the diagram (and your shape libraries)."""
    links: list[dict[str, str]] = []
    notes: list[str] = []
    done: dict[str, str] = {}

    def swap(m: re.Match) -> str:
        url = m.group(0).rstrip(".,;:")
        tail = m.group(0)[len(url):]
        if url in done:
            return done[url] + tail
        d = from_link(url)
        if not d:
            return m.group(0)
        if d["type"] != "xml":
            if mode == "desktop":
                notes.append(f"This diagram is {d['type']}, which only the draw.io web editor converts; draw.io "
                             "Desktop needs draw.io XML. Call the tool again with the diagram as draw.io XML "
                             "(mxGraphModel) to get a file for draw.io Desktop.")
            return m.group(0)
        name = save(d["xml"], title)
        file_url, web_url = f"/diagrams/{name}", f"/diagrams/{name}/web"
        local = file_url if mode == "desktop" else web_url
        done[url] = local
        desk = {"url": file_url, "title": "Open in draw.io Desktop", "drawio": name}
        web = {"url": web_url, "title": editor_title}
        links.extend([desk, web] if mode == "desktop" else [web, desk])
        links.append({"url": file_url + "?download=1", "title": f"Download {name}"})
        return local + tail

    out = LINK_RE.sub(swap, text or "")
    if links:
        where = "opens the file in draw.io Desktop on their Mac" if mode == "desktop" else "opens it in the draw.io editor"
        notes.append(f"The diagram was saved. Give the user the link above as a Markdown link, for example "
                     f"[Open the diagram]({done[next(iter(done))]}) (it {where}); not in backticks, and no "
                     f"app.diagrams.net links. The chat also shows buttons for it under your answer.")
    return out, links, notes
