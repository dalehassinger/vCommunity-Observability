"""Your own draw.io shape libraries for diagrams Ask makes with the draw.io MCP server.

Put draw.io library files (<mxlibrary>[{"title", "w", "h", "xml"}, ...]</mxlibrary>, as saved
by File > New Library / Export in draw.io) in the drawio-libs folder (compose mounts
./drawio-libs at /app/drawio-libs, ASK_DRAWIO_LIBS). Then:

- The model is told the shape names (note()) and asked to place one with a cell whose style
  is `lib=<exact name>`.
- Before create_diagram runs, expand() swaps each such cell for the library's real cells (one
  icon, a container with its header icon, or a whole starter layout), at the model's position
  and size, keeping the model's label and the children it put inside.
- The links Ask gives load the libraries in the draw.io editor's sidebar (served at
  /drawio-libs/<file>, see main.py), so you can drag more of your shapes in.

Read-only; files are read again when they change."""

from __future__ import annotations

import copy
import json
import os
import re
import threading
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from . import drawio

DIR = Path(os.environ.get("ASK_DRAWIO_LIBS") or "/app/drawio-libs")
FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.()-]{0,120}\.xml$")
_cache: dict[str, Any] = {"key": None, "libs": []}
_lock = threading.Lock()


def _norm(title: str) -> str:
    t = (title or "").lower().replace("—", "-").replace("–", "-")
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def _entry_xml(item: dict[str, Any]) -> str:
    x = str(item.get("xml") or "")
    if x and not x.lstrip().startswith("<"):
        x = drawio._xml_from_compressed(x)          # compressed library entries
    return x


def _parse(path: Path) -> dict[str, Any]:
    root = ET.parse(path).getroot()
    if root.tag != "mxlibrary":
        raise ValueError("not a draw.io library (<mxlibrary>)")
    items = json.loads(root.text or "[]")
    shapes = []
    for it in items:
        title = str(it.get("title") or "").strip()
        if not title:
            continue
        if it.get("xml"):
            shapes.append({"title": title, "w": it.get("w"), "h": it.get("h"), "xml": _entry_xml(it)})
        elif it.get("data"):             # an image entry
            style = f"shape=image;image={it['data']};aspect={it.get('aspect') or 'fixed'};verticalLabelPosition=bottom;verticalAlign=top;"
            shapes.append({"title": title, "w": it.get("w"), "h": it.get("h"),
                           "xml": f'<mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>'
                                  f'<mxCell id="img" value="" style="{style}" vertex="1" parent="1">'
                                  f'<mxGeometry width="{it.get("w") or 64}" height="{it.get("h") or 64}" as="geometry"/></mxCell></root></mxGraphModel>'})
    return {"file": path.name, "shapes": shapes, "error": ""}


def libraries() -> list[dict[str, Any]]:
    """[{"file", "shapes": [{"title", "w", "h", "xml"}], "error"}], read again when files change."""
    try:
        files = sorted(p for p in DIR.glob("*.xml") if p.is_file() and FILE_RE.match(p.name))
    except OSError:
        files = []
    key = tuple((p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in files)
    with _lock:
        if _cache["key"] == key:
            return _cache["libs"]
        libs = []
        for p in files:
            try:
                libs.append(_parse(p))
            except (OSError, ValueError, ET.ParseError) as e:
                libs.append({"file": p.name, "shapes": [], "error": f"{e.__class__.__name__}: {str(e)[:200]}"})
        _cache.update(key=key, libs=libs)
        return libs


def files() -> list[str]:
    return [l["file"] for l in libraries() if l["shapes"]]


def summary() -> list[dict[str, Any]]:
    return [{"file": l["file"], "shapes": len(l["shapes"]), "error": l["error"]} for l in libraries()]


def find(title: str) -> dict[str, Any] | None:
    want = _norm(title)
    for lib in libraries():
        for s in lib["shapes"]:
            if _norm(s["title"]) == want:
                return s
    return None


def note(limit: int = 400) -> str:
    """For the system prompt when the draw.io tools are on: the shape names and how to use them."""
    names = [s["title"] for l in libraries() for s in l["shapes"]]
    if not names:
        return ""
    shown = names[:limit]
    return ("draw.io shape library (the user's own shapes; prefer them over generic shapes): when you "
            "call create_diagram with draw.io XML, place one of these shapes with a vertex cell whose "
            "style is exactly lib=<name>; (for example style=\"lib=vCenter Server;\"), with the label "
            "in value and the position and size in mxGeometry. Ask replaces it with the real shape. "
            "Names ending in '— Container' are containers: put other cells inside with parent=<its id> "
            "(positions relative to it). Names starting with 'Starter —' are complete layouts. "
            "Spacing: icon shapes are about 64 x 64 with their name written under them (about 40 more "
            "pixels), so leave at least 150 pixels between icons side by side and 130 pixels from one "
            "row of icons to the next. Container headers are about 46 pixels tall: put the first row "
            "inside a container at y 70 or more, and make each container big enough for its rows. "
            "search_shapes only searches draw.io's built-in shapes, not these; don't search for them. "
            "Always finish by calling create_diagram; don't stop after describing the plan. "
            "Shapes: " + "; ".join(shown) + (f"; ... ({len(names) - limit} more)" if len(names) > limit else ""))


def _cells(entry_xml: str) -> list[tuple[ET.Element, ET.Element]]:
    """(element to copy, its mxCell) for the entry's cells, without the 0 and 1 cells."""
    root = ET.fromstring(entry_xml)
    r = root.find("root") if root.tag == "mxGraphModel" else root
    out = []
    for el in list(r if r is not None else []):
        cell = el if el.tag == "mxCell" else el.find("mxCell")
        if cell is None:
            continue
        cid = el.get("id") if el.tag != "mxCell" else cell.get("id")
        if cid in ("0", "1") or not (cell.get("vertex") or cell.get("edge")):
            continue
        out.append((el, cell))
    return out


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _num(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else f"{v:.2f}"


def _expand_one(placeholder: ET.Element, pcell: ET.Element, shape: dict[str, Any]) -> list[ET.Element]:
    pid = placeholder.get("id") if placeholder.tag != "mxCell" else pcell.get("id")
    label = placeholder.get("label") if placeholder.tag != "mxCell" else pcell.get("value")
    parent = pcell.get("parent") or "1"
    pg = pcell.find("mxGeometry")
    entries = _cells(shape["xml"])
    if not entries:
        return [placeholder]
    tops = [c for _, c in entries if (c.get("parent") or "1") == "1"]
    tg = tops[0].find("mxGeometry") if tops else None
    tx, ty = (_f(tg.get("x")), _f(tg.get("y"))) if tg is not None else (0.0, 0.0)
    mx, my = (_f(pg.get("x")), _f(pg.get("y"))) if pg is not None else (0.0, 0.0)
    ids: dict[str, str] = {}
    for n, (el, cell) in enumerate(entries):
        old = el.get("id") if el.tag != "mxCell" else cell.get("id")
        ids[old] = pid if n == 0 else f"{pid}-{n}"
    out = []
    for n, (el, cell) in enumerate(entries):
        el2 = copy.deepcopy(el)
        c2 = el2 if el2.tag == "mxCell" else el2.find("mxCell")
        old = el.get("id") if el.tag != "mxCell" else cell.get("id")
        if el2.tag == "mxCell":
            c2.set("id", ids[old])
        else:
            el2.set("id", ids[old])
            c2.attrib.pop("id", None)
        top = (cell.get("parent") or "1") == "1"
        c2.set("parent", parent if top else ids.get(cell.get("parent"), parent))
        for k in ("source", "target"):
            if c2.get(k):
                c2.set(k, ids.get(c2.get(k), c2.get(k)))
        g = c2.find("mxGeometry")
        if top and g is not None and not c2.get("edge"):
            if n == 0:
                g.set("x", _num(mx))
                g.set("y", _num(my))
                if pg is not None and _f(pg.get("width")) > 0 and _f(pg.get("height")) > 0:
                    g.set("width", pg.get("width"))
                    g.set("height", pg.get("height"))
            else:
                g.set("x", _num(_f(g.get("x")) - tx + mx))
                g.set("y", _num(_f(g.get("y")) - ty + my))
        if n == 0 and label:
            if el2.tag == "mxCell":
                c2.set("value", label)
            else:
                el2.set("label", label)
        out.append(el2)
    return out


LIB_RE = re.compile(r"(?:^|;)\s*lib=([^;]+)")


def expand(xml: str) -> tuple[str, list[str], list[str]]:
    """(xml with lib= cells replaced, names used, unknown names). Unchanged if it can't be parsed."""
    if "lib=" not in xml or not any(l["shapes"] for l in libraries()):
        return xml, [], []
    try:
        root = ET.fromstring(xml.strip())
    except ET.ParseError:
        return xml, [], []
    model = root if root.tag == "mxGraphModel" else root.find(".//mxGraphModel")
    r = model.find("root") if model is not None else None
    if r is None:
        return xml, [], []
    used, unknown = [], []
    for el in list(r):
        cell = el if el.tag == "mxCell" else el.find("mxCell")
        if cell is None or not cell.get("vertex"):
            continue
        m = LIB_RE.search(cell.get("style") or "")
        if not m:
            continue
        name = m.group(1).strip()
        shape = find(name)
        if not shape:
            unknown.append(name)
            cell.set("style", LIB_RE.sub("", cell.get("style") or "").lstrip(";") or "rounded=1;whiteSpace=wrap;")
            continue
        try:
            new = _expand_one(el, cell, shape)
        except (ET.ParseError, ValueError, KeyError):
            unknown.append(name)
            continue
        i = list(r).index(el)
        r.remove(el)
        for k, n in enumerate(new):
            r.insert(i + k, n)
        used.append(shape["title"])
    _clear_headers(r)
    if used and TIDY and not any(u.startswith("Starter") for u in used):   # starters are laid out already
        tidy(r)
    out = ET.tostring(root, encoding="unicode")
    return out, used, unknown


def _clear_headers(r: ET.Element) -> None:
    """Move cells the model put on a container's header band (swimlane startSize) below it."""
    cells: dict[str, ET.Element] = {}
    for el in r:
        c = el if el.tag == "mxCell" else el.find("mxCell")
        if c is not None:
            cells[el.get("id") if el.tag != "mxCell" else c.get("id")] = c
    head: dict[str, float] = {}
    for cid, c in cells.items():
        st = c.get("style") or ""
        if st.startswith("swimlane") or ";swimlane" in st:
            m = re.search(r"startSize=(\d+(?:\.\d+)?)", st)
            head[cid] = float(m.group(1)) if m else 23.0
    for cid, c in cells.items():
        p = c.get("parent")
        g = c.find("mxGeometry")
        if p not in head or g is None or not c.get("vertex"):
            continue
        st = c.get("style") or ""
        if "stencil(" in st and _f(g.get("width")) <= 34 and _f(g.get("y")) < head[p]:
            continue                       # the container's own header icon
        need = head[p] + 8
        if _f(g.get("y")) < need:
            g.set("y", _num(need))


def expand_args(args: dict[str, Any]) -> tuple[dict[str, Any], list[str], list[str]]:
    """Expand every string argument that holds draw.io XML with lib= cells."""
    used: list[str] = []
    unknown: list[str] = []
    out = dict(args)
    for k, v in args.items():
        if isinstance(v, str) and "lib=" in v and "<mx" in v:
            out[k], u, x = expand(v)
            used += u
            unknown += x
    return out, used, unknown


# -- tidy layout --------------------------------------------------------------------------
# Local models place shapes roughly: icons resized, rows too close, labels on top of the next
# row, containers too small. tidy() keeps the model's arrangement (which shapes share a row,
# their order) but redoes the geometry on a grid, deepest containers first, so every container
# fits what it holds and nothing overlaps. ASK_DRAWIO_TIDY=0 turns it off.
TIDY = os.environ.get("ASK_DRAWIO_TIDY", "1") not in ("0", "false", "off", "no")
ICON = 64            # icon size (the library's icons are 64 x 64)
SLOT = 140           # width taken by an icon with its name under it
LABEL = 46           # room for the name under an icon (two lines)
GAP = 20             # space between items
PAD = 20             # space inside a container's edges
ROW_TOLERANCE = 40   # items whose tops are this close share a row
TOP_GAP = 40         # between top-level shapes (sites side by side)


def _style(c: ET.Element) -> str:
    return c.get("style") or ""


def _is_container(c: ET.Element) -> bool:
    st = _style(c)
    return st.startswith("swimlane") or ";swimlane" in st or "container=1" in st


def _is_icon(c: ET.Element) -> bool:
    st = _style(c)
    if _is_container(c):
        return False
    # Picture shapes with the name under them (library icons are stencils or images).
    return ("verticalLabelPosition=bottom" in st or "stencil(" in st
            or "shape=image" in st or st.startswith("image;") or ";image=" in st)


def _header(c: ET.Element) -> float:
    m = re.search(r"startSize=(\d+(?:\.\d+)?)", _style(c))
    return float(m.group(1)) if m else (23.0 if _is_container(c) else 0.0)


def tidy(r: ET.Element) -> None:
    cells: dict[str, ET.Element] = {}
    order: list[str] = []
    for el in r:
        c = el if el.tag == "mxCell" else el.find("mxCell")
        if c is None:
            continue
        cid = el.get("id") if el.tag != "mxCell" else c.get("id")
        cells[cid] = c
        order.append(cid)
    kids: dict[str, list[str]] = {}
    for cid in order:
        c = cells[cid]
        if c.get("vertex") and c.find("mxGeometry") is not None:
            kids.setdefault(c.get("parent") or "1", []).append(cid)

    def depth(cid: str) -> int:
        d, p = 0, cells[cid].get("parent")
        while p in cells and d < 50:
            d, p = d + 1, cells[p].get("parent")
        return d

    def geo(cid: str) -> ET.Element:
        return cells[cid].find("mxGeometry")

    def item_w(cid: str) -> float:
        return SLOT if _is_icon(cells[cid]) else _f(geo(cid).get("width"), 120)

    def item_h(cid: str) -> float:
        return ICON + LABEL if _is_icon(cells[cid]) else _f(geo(cid).get("height"), 40)

    containers = [cid for cid in order if _is_container(cells[cid]) and cid in kids]
    for cid in sorted(containers, key=depth, reverse=True):
        c = cells[cid]
        # Header icon of a library container (small stencil on the header band) stays where it is.
        items = [k for k in kids[cid] if not ("stencil(" in _style(cells[k]) and _f(geo(k).get("width")) <= 34
                                              and _f(geo(k).get("y")) < _header(c))]
        if not items:
            continue
        for k in items:
            if _is_icon(cells[k]):
                g = geo(k)
                g.set("width", str(ICON))
                g.set("height", str(ICON))
        items.sort(key=lambda k: (_f(geo(k).get("y")), _f(geo(k).get("x"))))
        rows: list[list[str]] = []
        for k in items:
            if rows and _f(geo(k).get("y")) - _f(geo(rows[-1][0]).get("y")) <= ROW_TOLERANCE:
                rows[-1].append(k)
            else:
                rows.append([k])
        y = _header(c) + PAD
        width = 0.0
        for row in rows:
            row.sort(key=lambda k: _f(geo(k).get("x")))
            subs = [k for k in row if _is_container(cells[k])]
            if len(subs) > 1:          # containers side by side: same height
                hmax = max(_f(geo(k).get("height")) for k in subs)
                for k in subs:
                    geo(k).set("height", _num(hmax))
            x = PAD
            rh = 0.0
            for k in row:
                g = geo(k)
                w = item_w(k)
                g.set("x", _num(x + (w - ICON) / 2 if _is_icon(cells[k]) else x))
                g.set("y", _num(y))
                x += w + GAP
                rh = max(rh, item_h(k))
            width = max(width, x - GAP + PAD)
            y += rh + GAP
        label = c.get("value") or ""
        g = geo(cid)
        g.set("width", _num(max(width, 70 + 8 * len(label), 160)))
        g.set("height", _num(y - GAP + PAD))

    # Top level: containers grew, so put the top-level shapes on rows too (same order, no overlaps),
    # starting where the model's first shape was.
    top = [k for k in kids.get("1", []) if k in cells]
    if len(top) > 1:
        x0 = min(_f(geo(k).get("x")) for k in top)
        y = min(_f(geo(k).get("y")) for k in top)
        top.sort(key=lambda k: (_f(geo(k).get("y")), _f(geo(k).get("x"))))
        rows = []
        for k in top:
            if rows and _f(geo(k).get("y")) - _f(geo(rows[-1][0]).get("y")) <= ROW_TOLERANCE:
                rows[-1].append(k)
            else:
                rows.append([k])
        for row in rows:
            row.sort(key=lambda k: _f(geo(k).get("x")))
            x, rh = x0, 0.0
            for k in row:
                g, w = geo(k), item_w(k)
                g.set("x", _num(x + (w - ICON) / 2 if _is_icon(cells[k]) else x))
                g.set("y", _num(y))
                x += w + TOP_GAP
                rh = max(rh, item_h(k))
            y += rh + TOP_GAP
