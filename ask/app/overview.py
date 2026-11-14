"""Whole-source questions: "How many posts are in this blog?", "Summarize this site",
"What does Brock write about the most?".

Normal answers see only the best few chunks (Chunks to retrieve), which can't count or
summarize a whole blog. For these questions Ask builds a catalog of EVERY document in the
selected sources instead: title, date and headings of each post, plus numbers Ask counts
itself (posts, date range, posts per year and month, the most frequent title words). The
catalog is source [1] (one per source); if room is left, the best matching chunks follow
so the model also has some post text to quote."""

from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any

from .db import DB

NOUN = r"(blog\s*posts?|blogs?|posts?|articles?|pages?|documents?|docs|entries|write-?ups)"
OVERVIEW = re.compile("|".join([
    rf"\bhow\s+many\s+(\w+\s+){{0,2}}{NOUN}\b",
    rf"\b(count|number\s+of)\s+(\w+\s+){{0,2}}{NOUN}\b",
    rf"\b(list|show|name|give\s+me)\s+(me\s+)?(all|every|each)\s+(\w+\s+){{0,2}}{NOUN}\b",
    rf"\b(all|every|each)\s+(of\s+)?(the|his|her|their|my|these|those)?\s*{NOUN}\b",
    r"\bsummar(y|ise|ize|ising|izing)\b.{0,60}\b(site|blog|blogs|source|sources|posts|articles|content|everything)\b",
    r"\b(overview|summary|recap)\s+of\s+(this|the|his|her|their|my|all)\b",
    r"\b(most|main|key|top|common|recurring|frequent|popular)\s+(\w+\s+)?(topics?|subjects?|themes?|categories|areas)\b",
    r"\b(topics?|subjects?|themes?)\b.{0,40}\b(written|writes?|blogged|blogs?|covered|covers?|posted|posts?)\b",
    r"\bwrit(es|ten|ing)\s+about\s+(the\s+)?most\b",
    rf"\b(first|latest|newest|oldest|most\s+recent|earliest|last)\s+(\w+\s+)?{NOUN}\b",
    rf"\b{NOUN}\s+(per|by|each|a)\s+(year|month|week)\b",
    rf"\b(when|how\s+often|how\s+frequently)\b.{{0,40}}\b(post|posts|blog|blogs|publish|published|write|writes)\b",
]), re.I)

# Listing pages a crawler also indexes; counted apart from the posts.
NOT_POST = re.compile(r"/(categories|category|tags?|page|author|authors|search|archives?|feed)(/|$)|^/?$", re.I)
STOP = set("""a an and are as at be but by can do does for from how i if in into is it its me my of on or our
should so that the their them then there these this to was what when where which who why will with you your
vs part using use new how-to guide step steps your about via up out get getting de la le""".split())
HEADING = re.compile(r"^#{2,3}\s+(.+?)\s*$", re.M)


def is_overview_question(question: str) -> bool:
    return bool(OVERVIEW.search(question or ""))


def _docs(db: DB, source: str) -> list[dict[str, Any]]:
    with db.conn() as c:
        rows = c.execute("SELECT id, doc_id, title, path, url, meta, text FROM documents WHERE source=?", (source,)).fetchall()
    out = []
    for r in rows:
        try:
            meta = json.loads(r["meta"] or "{}")
        except ValueError:
            meta = {}
        date = str(meta.get("published") or meta.get("date") or "")[:10]
        heads = [h.strip("# ").strip() for h in HEADING.findall(r["text"] or "")]
        heads = [h for h in heads if h and h.lower() not in ("metadata", "contents", "table of contents")]
        out.append({"row": r["id"], "doc_id": r["doc_id"], "title": (r["title"] or r["doc_id"]).strip(),
                    "path": r["path"] or "", "url": r["url"] or "", "date": date, "heads": heads,
                    "post": not NOT_POST.search((r["path"] or r["doc_id"] or "").split("?")[0])})
    out.sort(key=lambda d: (d["date"] or "0000", d["title"]), reverse=True)  # newest first
    return out


def _words(titles: list[str]) -> list[tuple[str, int]]:
    """Most frequent title words and word pairs: an exact count of posts whose title has them."""
    cnt: Counter[str] = Counter()
    for t in titles:
        ws = [w for w in re.findall(r"[a-z0-9][a-z0-9.+-]*[a-z0-9]|[a-z0-9]", t.lower()) if w not in STOP and len(w) > 2]
        terms = set(ws) | {f"{a} {b}" for a, b in zip(ws, ws[1:])}
        cnt.update(terms)
    # Drop a single word when a pair with it is just as common (e.g. "aria" vs "aria operations").
    top = [(w, n) for w, n in cnt.most_common(80) if n >= 2]
    keep = []
    for w, n in top:
        if " " not in w and any(" " in p and w in p.split() and m >= n for p, m in top):
            continue
        keep.append((w, n))
    return keep[:25]


def _line(d: dict[str, Any], level: int) -> str:
    s = f"- {d['date'] or 'undated'} | {d['title']}"
    if level >= 2 and d["heads"]:
        s += " — " + "; ".join(h[:60] for h in d["heads"][:5])
    if d.get("url"):
        s += f" | {d['url']}"
    return s


def build(db: DB, cfg: dict[str, Any], sources: list[str] | None) -> dict[str, Any] | None:
    """Catalog of every document in the selected (non-spreadsheet) sources, sized to the
    context window. None when no such source has documents."""
    srcs = [s for s in db.list_sources()
            if s["enabled"] and s["plugin"] != "spreadsheet" and (sources is None or s["name"] in sources)]
    cats = []
    for s in srcs:
        docs = _docs(db, s["name"])
        if not docs:
            continue
        posts = [d for d in docs if d["post"]]
        dated = sorted(d["date"] for d in posts if d["date"])
        years = Counter(d["date"][:4] for d in posts if d["date"])
        months = Counter(d["date"][:7] for d in posts if d["date"])
        opts = s.get("options") or {}
        if isinstance(opts, str):
            try:
                opts = json.loads(opts)
            except ValueError:
                opts = {}
        cats.append({"source": s["name"], "title": s["title"], "plugin": s["plugin"],
                     "site": opts.get("site_url") or opts.get("repo_url") or "",
                     "date_from": opts.get("date_from") or "", "date_to": opts.get("date_to") or "",
                     "docs": docs, "posts": posts, "other": len(docs) - len(posts),
                     "first": dated[0] if dated else "", "last": dated[-1] if dated else "",
                     "undated": sum(1 for d in posts if not d["date"]),
                     "years": sorted(years.items()), "months": sorted(months.items()),
                     "words": _words([d["title"] for d in posts])})
    if not cats:
        return None

    # Room for the catalog: most of the context window (about 2.5 characters per token),
    # never less than the normal context budget.
    budget = max(int(cfg["context_chars"]), int(cfg["num_ctx"]) * 5 // 2 - 8000)
    level, shown = 2, None
    for level in (2, 1):
        size = sum(len(_line(d, level)) + 1 for c in cats for d in c["posts"])
        if size + 1500 * len(cats) <= budget:
            shown = {c["source"]: len(c["posts"]) for c in cats}
            break
    if shown is None:   # even titles don't fit: newest first, shared out between the sources
        level = 1
        per_char = budget - 1500 * len(cats)
        total = sum(len(c["posts"]) for c in cats) or 1
        avg = max(40, sum(len(_line(d, 1)) + 1 for c in cats for d in c["posts"]) // total)
        cap = max(20, per_char // avg)
        shown = {c["source"]: min(len(c["posts"]), max(10, cap * len(c["posts"]) // total)) for c in cats}
    for c in cats:
        c["shown"] = shown[c["source"]]
        c["level"] = level
    used = sum(len(block(c, 0)) for c in cats)
    return {"catalogs": cats, "level": level, "budget": budget, "left": budget - used}


def block(c: dict[str, Any], n: int) -> str:
    """The catalog of one source as the model sees it (source [n])."""
    span = f"{c['first']} to {c['last']}" if c["first"] else "no dates"
    lines = [f"[{n}] id: catalog | {c['title']} — complete catalog of the indexed source"
             + (f" ({c['site']})" if c["site"] else ""),
             "These numbers were counted by Ask over every indexed document, so they are exact:",
             f"- Posts: {len(c['posts'])}" + (f" (plus {c['other']} category/tag/listing pages, not counted as posts)" if c["other"] else ""),
             f"- Dates: {span}" + (f"; {c['undated']} posts have no date" if c["undated"] else "")]
    if c["date_from"] or c["date_to"]:
        lines.append(f"- Indexed date range (source setting): {c['date_from'] or 'any'} to {c['date_to'] or 'any'}. "
                     "Posts outside it are not in the knowledge base.")
    if c["years"]:
        lines.append("- Posts per year: " + ", ".join(f"{y}: {k}" for y, k in c["years"]))
    if c["months"]:
        lines.append("- Posts per month: " + ", ".join(f"{m}: {k}" for m, k in c["months"][-24:]))
    if c["words"]:
        lines.append("- Most frequent title words (number of post titles containing them): "
                     + ", ".join(f"{w} ({k})" for w, k in c["words"]))
    head = "Every post, newest first (date | title" + (" — section headings" if c["level"] >= 2 else "") + " | url):"
    if c["shown"] < len(c["posts"]):
        head = f"The newest {c['shown']} of {len(c['posts'])} posts (the rest did not fit), newest first (date | title | url):"
    lines.append(head)
    lines += [_line(d, c["level"]) for d in c["posts"][: c["shown"]]]
    return "\n".join(lines)


def instructions(cat: dict[str, Any]) -> str:
    return ("The catalog source(s) above list every indexed post of the selected source(s), with exact counts "
            "made by Ask. Use those counts as given (do not recount the list). For topic and summary questions, "
            "group the posts into themes from their titles and headings and say roughly how many posts each "
            "theme has; give examples by title. Cite the catalog as its source number. Other numbered sources, "
            "if any, are excerpts of the best matching posts. If a post you would need is not in the catalog, "
            "say that the knowledge base does not include it.")


def card(c: dict[str, Any], n: int) -> dict[str, Any]:
    span = f"{c['first']} to {c['last']}" if c["first"] else ""
    return {"n": n, "kind": "catalog", "source": c["source"], "doc_id": "catalog", "doc_row": None,
            "title": f"All posts · {c['title']}", "heading": "", "url": c["site"], "status": "", "date": "",
            "via": "", "from_doc": "", "images": [],
            "snippet": f"{len(c['posts'])} posts" + (f", {span}" if span else "") + (f" (+{c['other']} other pages)" if c["other"] else ""),
            "posts": len(c["posts"]), "other": c["other"], "first": c["first"], "last": c["last"],
            "years": c["years"], "words": c["words"][:12], "shown": c["shown"], "level": c["level"],
            "items": [{"title": d["title"], "date": d["date"], "url": d["url"], "row": d["row"]} for d in c["posts"]]}


# A whole-source question that also reads like a data question ("list all posts" vs "list
# all hosts") goes to the catalog only when it names blog-type things.
TEXTY = re.compile(r"\b(blogs?|posts?|articles?|sites?|websites?|writes?|written|wrote|authors?|bloggers?|topics?)\b", re.I)
