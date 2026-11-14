"""Retrieval and answering.

1. Hybrid search: sqlite-vec similarity + FTS5 keyword (BM25), merged with
   reciprocal rank fusion, at most two chunks per document.
2. Link expansion: files named in same_name, internal_derive, same_meaning and
   related are added (same_name first: it stops the model mixing the VM and the
   ESXi meaning of the same metric label).
3. The numbered sources go to the Ollama chat model with the question and the
   last few turns of the chat; the answer streams back token by token.
4. MCP tools (when a "Tools" entry is picked in the Sources menu): the model may call
   the tools of MCP servers on the host (app/mcp.py, through the Ask host agent); each
   result becomes one more numbered source, and the model answers when it stops calling.
5. Web search (when "Web search (Ollama)" is picked): the same tool loop with two built-in
   tools, web_search and web_fetch (app/websearch.py, Ollama's cloud web search API)."""

from __future__ import annotations

import json
import math
import posixpath
import re
import time
from typing import Any, Iterator
from urllib.parse import quote

from . import data, mcp, overview, syslog_out, websearch
from . import reports as reports_mod
from .db import DB
from .indexer import embed_prefixes

log = __import__("logging").getLogger("ask.rag")
REPORTS = None   # app.reports.Reports, set by create_app; makes HTML reports for report questions
from .ollama import Ollama, OllamaError

RRF_K = 60
# Balanced retrieval: a source's best section counts as a match (and gets a guaranteed slot)
# when its keyword search found it, or its vector distance is within this much of the best
# distance over all picked sources. Keeps a source that has nothing to say out of the answer.
BALANCE_MARGIN = 0.10      # vector: within this cosine distance of the best section overall
BALANCE_BM25_RATIO = 0.5   # keyword: BM25 at least half as strong as the best real keyword hit overall
BALANCE_TERM_SHARE = 0.5   # keyword: a real hit has at least half the question's search words (and 2+)
LINK_ORDER = ("same_name", "internal_derive", "same_meaning", "related")
STOP = set("""a an and are as at be but by can do does for from how i if in into is it its me my of on or our
should so that the their them then there these this to was what when where which who why will with you your
vs versus about please explain tell show give""".split())


def fts_query(question: str) -> str:
    words = [w.lower() for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9_.%-]*", question)]
    words = [w.strip(".-") for w in words if w.lower() not in STOP and len(w) > 1]
    seen: list[str] = []
    for w in words:
        if w and w not in seen:
            seen.append(w)
    return " OR ".join(f'"{w}"' for w in seen[:24])


def file_url(source: str, path: str) -> str:
    return f"/files/{quote(source)}/{quote(path)}"


def retrieve(db: DB, cfg: dict[str, Any], question: str, sources: list[str] | None,
             info: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """The sections sent to the model. `info`, when given, gets the retrieval note
    (info["retrieval"]: how many sections were reserved, and from which sources)."""
    model = cfg["embed_model"]
    _, qprefix = embed_prefixes(model)
    ollama = Ollama(cfg["ollama_url"])
    qvec = ollama.embed(model, [qprefix + question], keep_alive=cfg["keep_alive"])[0]
    pool = max(30, cfg["top_k"] * 4)
    match = fts_query(question)
    vec = db.vector_search(qvec, pool, sources)
    kw = db.keyword_search(match, pool, sources)

    score: dict[int, float] = {}
    for hits in (vec, kw):
        for rank, (cid, _) in enumerate(hits):
            score[cid] = score.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)
    ranked = sorted(score, key=score.get, reverse=True)

    # Balanced retrieval: with 2 or more sources picked, a source that the shared ranking leaves
    # with fewer than "Sections per source" sections gets its own best matching sections reserved,
    # so one big source (a blog with hundreds of posts) can't take every slot. Only those
    # sources are searched again; the other slots go to the best sections overall, as before.
    top_k = int(cfg["top_k"])
    per_source = int(cfg.get("source_min_chunks") or 0)
    chunks = db.get_chunks(ranked[:pool])
    reserve: dict[str, list[int]] = {}
    spare: dict[str, list[int]] = {}
    base: set[int] = set()
    short: list[str] = []
    if per_source > 0 and sources and len(sources) > 1:
        base = set(cap_per_doc(ranked, chunks, top_k))
        have: dict[str, int] = {}
        for cid in base:
            have[chunks[cid]["source"]] = have.get(chunks[cid]["source"], 0) + 1
        short = [src for src in sources if have.get(src, 0) < per_source]
        if short:
            reserve, spare, extra = balanced_picks(db, qvec, match, short, have, per_source, top_k,
                                                   vec[0][1] if vec else None, kw, score)
            chunks.update(extra)

    picked: list[dict[str, Any]] = []
    per_doc: dict[int, int] = {}
    seen: set[int] = set()

    def take(cid: int, balanced: bool) -> bool:
        ch = chunks.get(cid)
        if cid in seen or not ch or per_doc.get(ch["doc_row"], 0) >= 2:
            return False
        seen.add(cid)
        per_doc[ch["doc_row"]] = per_doc.get(ch["doc_row"], 0) + 1
        item = {**ch, "kind": "hit", "score": round(score.get(cid, 0.0), 4)}
        if balanced:
            item["balanced"] = True
            if cid not in base:
                item["reserved"] = True      # it is here only because of the reservation
        picked.append(item)
        return True

    def next_spare(src: str, fits=lambda ch: True) -> int | None:
        for cid in spare.get(src, []):
            ch = chunks.get(cid)
            if cid not in seen and ch and per_doc.get(ch["doc_row"], 0) < 2 and fits(ch):
                return cid
        return None

    # Reserved sections first. One that can't be used (its document already has two sections)
    # is replaced by the next match from the same source, so the slot stays with that source.
    for src, ids in reserve.items():
        for cid in ids:
            while cid is not None and len(picked) < top_k and not take(cid, True):
                cid = next_spare(src)
    for cid in ranked:
        if len(picked) >= top_k:
            break
        take(cid, False)
    # Best first in the prompt and the cards (the guaranteed ones were taken first only so
    # that they are sure to get a slot).
    picked.sort(key=lambda it: -it["score"])

    linked: list[dict[str, Any]] = []
    if cfg["expand_links"] and picked:
        have = {p["doc_row"] for p in picked}
        for link_type in LINK_ORDER:
            for p in picked:
                ids = p["links"].get(link_type) or []
                for d in db.find_documents(p["source"], ids):
                    if d["id"] in have:
                        continue
                    have.add(d["id"])
                    first = db.first_chunks([d["id"]])
                    if first:
                        ch = db.get_chunks(first)[first[0]]
                        linked.append({**ch, "kind": "linked", "via": link_type, "from_doc": p["doc_id"]})
        linked = linked[: max(4, cfg["top_k"])]

    # Fit to the context budget: direct hits first, then linked files.
    # Balanced picks are fitted first so a small source's reserved section isn't the one cut; if
    # one is too big, the next match from the same source that fits takes its place. The order
    # sent to the model stays best first.
    budget = cfg["context_chars"]
    kept_hits: list[dict[str, Any]] = []
    kept_linked: list[dict[str, Any]] = []
    for item in sorted(picked, key=lambda it: not it.get("balanced")) + linked:
        size = len(item["text"]) + 200
        if budget - size < 0:
            alt = None
            if item.get("balanced"):
                per_doc[item["doc_row"]] -= 1          # dropped: its document has room again
                alt = next_spare(item["source"], lambda ch: len(ch["text"]) + 200 <= budget)
                if alt is not None and take(alt, True):
                    new = picked[-1]
                    budget -= len(new["text"]) + 200
                    kept_hits.append(new)
                    continue
                per_doc[item["doc_row"]] += 1
            if kept_hits or kept_linked:
                continue                                # no room (the first section is always kept)
        budget -= size
        (kept_linked if item.get("kind") == "linked" else kept_hits).append(item)
    out = sorted(kept_hits, key=lambda it: -it["score"]) + kept_linked
    if info is not None and base:
        reserved: dict[str, int] = {}
        for item in out:
            if item.get("reserved"):
                reserved[item["source"]] = reserved.get(item["source"], 0) + 1
        info["retrieval"] = {"reserved": sum(reserved.values()), "sources": reserved, "searched": short}
    for n, item in enumerate(out, 1):
        item["n"] = n
    return out


def cap_per_doc(ids: list[int], chunks: dict[int, dict[str, Any]], limit: int, per_doc: int = 2) -> list[int]:
    """The first `limit` ids, at most `per_doc` from one document (the same rule as the final pick)."""
    out: list[int] = []
    count: dict[int, int] = {}
    for cid in ids:
        ch = chunks.get(cid)
        if not ch or count.get(ch["doc_row"], 0) >= per_doc:
            continue
        count[ch["doc_row"]] = count.get(ch["doc_row"], 0) + 1
        out.append(cid)
        if len(out) >= limit:
            break
    return out


def balanced_picks(db: DB, qvec: list[float], match: str, short: list[str], have: dict[str, int],
                   per_source: int, top_k: int, best_distance: float | None, kw: list[tuple[int, float]],
                   score: dict[int, float]) -> tuple[dict[str, list[int]], dict[str, list[int]], dict[int, dict[str, Any]]]:
    """Reserved sections for the sources the shared ranking left short.

    A section qualifies only when
      - its vector distance is within BALANCE_MARGIN of the best section overall, or
      - it is a real keyword hit: it holds at least BALANCE_TERM_SHARE of the question's search
        words (never fewer than 2), and its BM25 score is at least BALANCE_BM25_RATIO of the best
        real keyword hit overall.
    A plain OR hit (one or two shared words, even rare ones) is not relevance.

    Each source's pick follows the two-sections-per-document rule. Sources are ranked by their
    own match quality: a source with nothing in the shared top first, then the closest best
    section (vector distance), then the source's own fusion score. With more short sources than
    slots, each gets fewer (at least 1) and the lowest ranked ones get none; at most top_k in all.

    Returns (reserved ids per source in rank order, spare matches per source for refills,
    the chunks looked up)."""
    max_dist = best_distance + BALANCE_MARGIN if best_distance is not None else None
    terms = re.findall(r'"([^"]+)"', match or "")
    need = min(len(terms), max(2, math.ceil(len(terms) * BALANCE_TERM_SHARE))) if terms else 0

    def covered(ids: list[int]) -> set[int]:
        counts = db.term_hits(terms, ids) if need else {}
        return {cid for cid, n in counts.items() if n >= need}

    # The bar for BM25 comes from the best global keyword hit that is itself a real match.
    top_kw = kw[:20]
    ok_global = covered([cid for cid, _ in top_kw])
    best_bm25 = next((sc for cid, sc in top_kw if cid in ok_global), None)
    # bm25() is negative, lower is better: a strong hit is at or below this.
    min_bm25 = best_bm25 * BALANCE_BM25_RATIO if best_bm25 is not None and best_bm25 < 0 else None
    looked: dict[int, dict[str, Any]] = {}
    found: list[tuple[tuple, str, list[int]]] = []
    for src in short:
        want = per_source * 4          # extra candidates for the per-document cap and refills
        v = db.vector_search(qvec, want, [src])
        k = db.keyword_search(match, want, [src]) if match else []
        dist = dict(v)
        missing = [cid for cid, _ in k if cid not in dist]
        if missing:
            dist.update(db.vector_distances(qvec, missing))   # keyword-only hits: check them by meaning too
        strong_kw = covered([cid for cid, sc in k if min_bm25 is not None and sc <= min_bm25])
        local: dict[int, float] = {}
        for hits in (v, k):
            for rank, (cid, _) in enumerate(hits):
                local[cid] = local.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)
        good = [cid for cid in sorted(local, key=local.get, reverse=True)
                if (max_dist is not None and cid in dist and dist[cid] <= max_dist) or cid in strong_kw]
        if not good:
            continue
        looked.update(db.get_chunks(good))
        ordered = cap_per_doc(good, looked, len(good))
        if not ordered:
            continue
        for cid in ordered:
            score.setdefault(cid, local[cid] / 2)   # prompt order only: below the shared top
        quality = (have.get(src, 0) > 0, min(dist.get(c, 2.0) for c in ordered[:per_source]), -local[ordered[0]])
        found.append((quality, src, ordered))
    found.sort(key=lambda f: f[0])
    each = max(1, min(per_source, top_k // max(1, len(found)))) if found else 0
    reserve: dict[str, list[int]] = {}
    spare: dict[str, list[int]] = {}
    total = 0
    for _, src, ordered in found:
        n = min(each, top_k - total)
        if n > 0:
            reserve[src] = ordered[:n]
            total += n
        spare[src] = ordered[n:] if n > 0 else ordered
    return reserve, spare, looked


def source_cards(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """What the UI shows under an answer (and what is saved with the message)."""
    cards = []
    for it in items:
        images = []
        heading_text = it["text"]
        for img in it.get("images") or []:
            # Only images that this chunk links to, so the card shows the right diagram.
            name = posixpath.basename(img["path"])
            if name in heading_text:
                images.append({"url": file_url(it["source"], img["path"]), "alt": img.get("alt", "")})
        cards.append({"n": it["n"], "source": it["source"], "doc_id": it["doc_id"], "doc_row": it["doc_row"],
                      "title": it["title"], "heading": it["heading"], "url": it["url"], "status": it["status"],
                      "date": str((it.get("meta") or {}).get("published") or ""),
                      "reserved": bool(it.get("reserved")),
                      "kind": it["kind"], "via": it.get("via", ""), "from_doc": it.get("from_doc", ""),
                      "snippet": _snippet(it["text"]), "images": images[:3]})
    return cards


def _snippet(text: str, limit: int = 280) -> str:
    lines = [l for l in text.splitlines() if l.strip() and not l.startswith("#") and not l.startswith("![")
             and not l.lstrip().startswith("|") and not l.startswith("```")
             and not l.startswith("Published: ")]
    s = " ".join(l.strip().lstrip("-* ").strip() for l in lines)
    return s[:limit] + ("…" if len(s) > limit else "")


URL_NOTE = ("Each source header and catalog line shows the page's full url. When the question asks for "
            "links or URLs, copy them exactly as shown (full address, starting with http). Never shorten, "
            "guess or make up a url; if a page has none, say so.")


def build_messages(cfg: dict[str, Any], items: list[dict[str, Any]], question: str,
                   history: list[dict[str, Any]], lead: list[str] | None = None,
                   note: str = "") -> list[dict[str, str]]:
    """lead: blocks that come before the chunks (the catalogs of a whole-source question);
    note: an instruction added after the sources."""
    blocks = list(lead or [])
    for it in items:
        head = f"[{it['n']}] id: {it['doc_id']} | {it['title']}"
        if it["heading"]:
            head += f" > {it['heading']}"
        if it["status"]:
            head += f" | status: {it['status']}"
        if (it.get("meta") or {}).get("published"):
            head += f" | published: {it['meta']['published']}"
        if it.get("url"):
            head += f" | url: {it['url']}"
        if it["kind"] == "linked":
            head += f" | linked ({it['via']}) from {it['from_doc']}"
        blocks.append(f"{head}\n{it['text']}")
    context = "\n\n---\n\n".join(blocks) if blocks else "(no sources found)"
    if any(it.get("url") for it in items) or any(" | http" in b for b in lead or []):
        note = (note + "\n\n" if note else "") + URL_NOTE
    msgs = [{"role": "system", "content": cfg["system_prompt"]}]
    turns = max(0, int(cfg["history_turns"]))
    if turns:
        for m in history[-turns * 2:]:
            if m["role"] in ("user", "assistant") and m["content"]:
                msgs.append({"role": m["role"], "content": m["content"]})
    msgs.append({"role": "user", "content": f"Sources:\n\n{context}\n\n" + (f"{note}\n\n" if note else "")
                 + f"=====\n\nQuestion: {question}"})
    return msgs


def plain_messages(cfg: dict[str, Any], question: str, history: list[dict[str, Any]]) -> list[dict[str, str]]:
    """No sources: the plain prompt, the recent turns of this chat, and the request as written."""
    msgs = [{"role": "system", "content": cfg.get("plain_system_prompt") or cfg["system_prompt"]}]
    turns = max(0, int(cfg["history_turns"]))
    for m in (history[-turns * 2:] if turns else []):
        if m["role"] in ("user", "assistant") and m["content"]:
            msgs.append({"role": m["role"], "content": m["content"]})
    msgs.append({"role": "user", "content": question})
    return msgs


def overview_sources(db: DB, cfg: dict[str, Any], question: str, sources: list[str] | None,
                     ov: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]]]:
    """Cards, catalog blocks and extra chunks for a whole-source question. The catalogs come
    first; the best matching chunks fill what is left of the context (at most 6)."""
    cats = ov["catalogs"]
    cards = [overview.card(c, i + 1) for i, c in enumerate(cats)]
    lead = [overview.block(c, i + 1) for i, c in enumerate(cats)]
    extra: list[dict[str, Any]] = []
    room = ov["left"] - 1000
    if room > 3000:
        try:
            items = retrieve(db, cfg, question, [c["source"] for c in cats])
        except OllamaError:
            items = []
        for it in items:
            size = len(it["text"]) + 200
            if it["kind"] != "hit" or size > room:
                continue
            room -= size
            extra.append(it)
            if len(extra) >= 6:
                break
        for k, it in enumerate(extra, len(cats) + 1):
            it["n"] = k
    return cards + source_cards(extra), lead, extra


def build_data_messages(cfg: dict[str, Any], res: dict[str, Any], question: str,
                        history: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Messages for a data question: the query result is source [1]."""
    msgs = [{"role": "system", "content": cfg["system_prompt"]}]
    turns = max(0, int(cfg["history_turns"]))
    if turns:
        for m in history[-turns * 2:]:
            if m["role"] in ("user", "assistant") and m["content"]:
                msgs.append({"role": m["role"], "content": m["content"]})
    if res.get("panels"):
        block = (f"[1] id: dashboard | spreadsheet source \"{res['source_title']}\" (tables: {', '.join(res['tables'])})\n"
                 "These dashboard panels were each computed with an SQL query on the complete data, so the numbers are exact:\n\n"
                 f"{data.panels_markdown(res)}\n\n"
                 "The reader gets these panels as an HTML dashboard report (KPI cards, charts and tables) under your answer. "
                 "Write a short executive summary of it: 3 to 6 bullet points with the most important numbers and what stands "
                 "out, citing [1]. Do not repeat every table, do not draw charts in text and do not say you cannot make reports.")
        msgs.append({"role": "user", "content": f"Sources:\n\n{block}\n\n=====\n\nRequest: {question}"})
        return msgs
    block = (f"[1] id: data query | spreadsheet source \"{res['source_title']}\" (tables: {', '.join(res['tables'])})\n"
             f"This SQL query was run on the complete data, so its result is exact:\n```sql\n{res['sql']}\n```\n"
             f"Result:\n{data.result_markdown(res)}\n\n"
             "Answer from source [1] and cite it. Give the numbers exactly as in the result. If the result "
             "does not answer the question (wrong columns, no rows), say so plainly. The complete result table "
             f"({len(res['rows'])}{'+' if res['truncated'] else ''} rows) is shown to the reader right under your answer, so do not "
             "copy long tables: summarize them (how many rows, notable values) and never say rows are missing "
             "because only some are listed here."
             + (f" A {res['chart']['type']} chart of this result ({res['chart']['title']}) is also drawn for the reader "
                "under your answer, so do not draw charts in text or say you cannot make charts; describe what it shows."
                if res.get("chart") else "")
             + (" The reader also gets this result as an HTML report (KPI cards, chart and table) under your answer: write "
                "a short summary for it (what was counted, the totals, what stands out) and do not say you cannot make reports."
                if res.get("report") else ""))
    msgs.append({"role": "user", "content": f"Sources:\n\n{block}\n\n=====\n\nQuestion: {question}"})
    return msgs


TOOL_NOTE = """You can call the tools of these MCP servers: {servers}. Call a tool whenever the question needs live data or an
action that a tool provides, and call several (one after another) when needed. Each tool result starts with its source
number, for example [4]: cite it like the other sources. Never invent tool results or arguments the user did not give;
if a tool fails or is cancelled, say so. Call a tool that changes something (sends mail, creates or deletes records,
saves files) only when the user asked for exactly that."""


WEB_NOTE = """You can search the web with the web_search tool and read one page with web_fetch. Use them when the
sources above do not answer the question, or when it needs current information (versions, releases, news). Prefer the
knowledge sources when they answer it. Search with short, specific queries; fetch a page only when the search result text
is not enough. Each result starts with its source number, for example [5]: cite it, and name the web site."""


ORDER_NOTE = """Web search has a fixed order. Always search with {first} ({first_name}) first. Read its results: if they
answer the question, use them (web_fetch reads one page in full when needed). Only if they do not answer the question or
are not good enough, call {second} ({second_name}) with a short, specific query. If {first} fails or finds nothing,
{second_name} is searched for you automatically. Each result starts with its source number, for example [5]: cite it,
and name the web site."""

WEB_ORDERS = {"ollama_first": ("web_search", "brave_search"), "brave_first": ("brave_search", "web_search")}


def web_order(cfg: dict[str, Any], servers: list[str]) -> tuple[str, str] | None:
    """(first, second) search tool when both providers are ticked and a fixed order is set
    (Settings "Web search order"); None lets the model pick. Unknown values mean Ollama first."""
    if not all(s in (servers or []) for s in websearch.SCOPES):
        return None
    value = str(cfg.get("web_order") or "ollama_first").strip().lower()
    return None if value == "model" else WEB_ORDERS.get(value, WEB_ORDERS["ollama_first"])


BRAVE_NOTE = """You can search the web with the brave_search tool (Brave Search). Use it when the sources above do not
answer the question, or when it needs current information (versions, releases, news). Prefer the knowledge sources when
they answer it. Search with short, specific queries. Each result starts with its source number, for example [5]: cite it,
and name the web site."""


def tool_messages(cfg: dict[str, Any], items: list[dict[str, Any]], question: str, history: list[dict[str, Any]],
                  servers: list[str]) -> list[dict[str, Any]]:
    names = [s for s in servers if s not in websearch.SCOPES]
    notes = [TOOL_NOTE.format(servers=", ".join(names))] if names else []
    order = web_order(cfg, servers)
    if order:
        notes.append(ORDER_NOTE.format(first=order[0], second=order[1], first_name=websearch.label(order[0]),
                                       second_name=websearch.label(order[1])))
    else:
        notes += ([WEB_NOTE] if websearch.SCOPE in servers else []) + ([BRAVE_NOTE] if websearch.BRAVE_SCOPE in servers else [])
    note = "\n\n".join(notes)
    if items:
        msgs = build_messages(cfg, items, question, history)
    else:
        msgs = [{"role": "system", "content": cfg["system_prompt"]}]
        turns = max(0, int(cfg["history_turns"]))
        for m in (history[-turns * 2:] if turns else []):
            if m["role"] in ("user", "assistant") and m["content"]:
                msgs.append({"role": m["role"], "content": m["content"]})
        msgs.append({"role": "user", "content": f"Question: {question}"})
    msgs[0] = {"role": "system", "content": msgs[0]["content"] + "\n\n" + note}
    return msgs


def sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def answer_stream(db: DB, cfg: dict[str, Any], chat_id: str, question: str,
                  sources: list[str] | None, tools: list[str] | None = None,
                  interactive: bool = True) -> Iterator[str]:
    """Server-sent events: meta (chat id + source cards), thinking, token, tool (a tool call
    card, sent again when it changes), done, error. `tools`: MCP server names whose tools the
    model may call; `sources` may then be empty (tools only). `interactive`: False for the
    REST API, where a tool set to "ask" is refused because nobody can press Run."""
    t0 = time.time()
    history = db.messages(chat_id)
    db.add_message(chat_id, "user", question)
    content, thinking, cards, stats = "", "", [], {}
    model = cfg["chat_model"]
    finished = False
    # Activity log (Logs module): the prompt now, then tool calls, the thinking and the answer.
    syslog_out.prompt(cfg, chat_id, question, sources, tools, interactive)
    try:
        mcp_tools: list[dict[str, Any]] = []
        mcp_index: dict[str, Any] = {}
        servers = [t for t in (tools or []) if t not in websearch.SCOPES]
        if servers:
            info = mcp.servers(cfg.get("agent_url") or "", fresh=True)
            policy = mcp.load_policy(cfg.get("mcp_tools"))
            mcp_tools, mcp_index = mcp.ollama_tools(info["servers"], servers, policy)
            if not mcp_tools:
                why = info["error"] or next((s["error"] for s in info["servers"] if s["name"] in servers and s.get("error")), "")
                raise OllamaError("The MCP tools are not available" + (f": {why}" if why else
                                  " (no tools, or every tool is turned off on the MCP tools page)."))
        for scope in [s for s in websearch.SCOPES if s in (tools or [])]:
            ws = websearch.status_of(cfg, scope)
            if not ws["available"]:
                raise OllamaError(f"{websearch.label(scope)} is not available: " + ws["why"])
            web_tools, web_index = websearch.ollama_tools(scope)
            mcp_tools = mcp_tools + web_tools
            mcp_index = {**mcp_index, **web_index}
        order = web_order(cfg, tools or [])
        if order:   # state of this answer, not a tool (names starting with _ are never tools)
            mcp_index["_web_order"] = {"first": order[0], "second": order[1], "searched": False}
        plain = sources == [] and not tools    # "No sources (just the model)"
        if db.stats()["chunks"] == 0 and not (tools and not sources) and not plain:
            raise OllamaError("Nothing is indexed yet. Open Knowledge sources and press Sync now.")
        res = None
        ov = None
        srcs = data.tables_for(db, sources) if cfg.get("data_questions", True) and not tools and not plain else []
        if not tools and not plain and cfg.get("overview_questions", True) and overview.is_overview_question(question) and not (
                srcs and data.is_data_question(question, srcs) and not overview.TEXTY.search(question)):
            # "How many posts...", "summarize this blog": a catalog of every post, not the top chunks.
            ov = overview.build(db, cfg, sources)
        if ov:
            cards, lead, extra = overview_sources(db, cfg, question, sources, ov)
            stats["overview"] = {"posts": sum(len(c["posts"]) for c in ov["catalogs"]), "level": ov["level"],
                                 "shown": sum(c["shown"] for c in ov["catalogs"]), "excerpts": len(extra)}
            yield sse("meta", {"chat_id": chat_id, "sources": cards, "model": model,
                               "retrieval_ms": int((time.time() - t0) * 1000)})
            msgs = build_messages(cfg, extra, question, history, lead=lead, note=overview.instructions(ov))
        elif srcs and data.is_data_question(question, srcs):
            rkind = data.report_kind(question)
            src = data.pick_source(srcs, question)
            if rkind == "dashboard":
                res = data.dashboard(cfg, src, question)
            else:
                res = data.query(cfg, src, question)
                if rkind and not res.get("error"):
                    res["chart"] = res.get("chart") or data.auto_chart(res["columns"], res["rows"])
            if not res.get("error"):
                res["report"] = rkind
            syslog_out.data_query(cfg, chat_id, res)
            if res.get("error"):
                stats["data_error"] = res["error"]
                res = None
        if plain:
            # No sources: the question (with pasted text) goes to the model with the plain prompt.
            cards = []
            yield sse("meta", {"chat_id": chat_id, "sources": [], "model": model, "plain": True,
                               "retrieval_ms": int((time.time() - t0) * 1000)})
            msgs = plain_messages(cfg, question, history)
        elif tools:
            # Tools picked: the usual search over the picked sources (if any), plus the tools.
            items = retrieve(db, cfg, question, sources, stats) if sources else []
            cards = source_cards(items)
            yield sse("meta", {"chat_id": chat_id, "sources": cards, "model": model, "tools": len(mcp_tools),
                               "retrieval_ms": int((time.time() - t0) * 1000)})
            msgs = tool_messages(cfg, items, question, history, tools)
        elif ov:
            pass
        elif res:
            cards = [data.card(res)]
            yield sse("meta", {"chat_id": chat_id, "sources": cards, "model": model,
                               "retrieval_ms": int((time.time() - t0) * 1000)})
            msgs = build_data_messages(cfg, res, question, history)
        else:
            items = retrieve(db, cfg, question, sources, stats)
            cards = source_cards(items)
            yield sse("meta", {"chat_id": chat_id, "sources": cards, "model": model,
                               "retrieval_ms": int((time.time() - t0) * 1000)})
            msgs = build_messages(cfg, items, question, history)
        stats.setdefault("retrieval_ms", int((time.time() - t0) * 1000))   # saved, for the answer quality test
        options = {"temperature": cfg["temperature"], "num_ctx": cfg["num_ctx"]}
        olla = Ollama(cfg["ollama_url"])
        rounds = max(1, int(cfg.get("mcp_max_rounds") or 6)) if mcp_tools else 0
        ev_sum = ed_sum = 0
        for rnd in range(rounds + 1):
            # With tools: the model may call tools for `rounds` turns; the last turn has no tools,
            # so it has to answer.
            calls: list[dict[str, Any]] = []
            said = ""
            for obj in olla.chat_stream(model, msgs, options=options, think=cfg["think"], keep_alive=cfg["keep_alive"],
                                        tools=mcp_tools if rnd < rounds else None):
                msg = obj.get("message") or {}
                if msg.get("thinking"):
                    thinking += msg["thinking"]
                    yield sse("thinking", msg["thinking"])
                if msg.get("content"):
                    content += msg["content"]
                    said += msg["content"]
                    yield sse("token", msg["content"])
                if msg.get("tool_calls"):
                    calls += msg["tool_calls"]
                if obj.get("done"):
                    ev, ed = obj.get("eval_count") or 0, obj.get("eval_duration") or 0
                    ev_sum, ed_sum = ev_sum + ev, ed_sum + ed
                    stats = {**stats, "tokens": ev_sum, "tok_per_s": round(ev_sum / (ed_sum / 1e9), 1) if ed_sum else None,
                             "prompt_tokens": obj.get("prompt_eval_count"),
                             "seconds": round(time.time() - t0, 1)}
            if not calls:
                break
            msgs.append({"role": "assistant", "content": said, "tool_calls": calls})
            for c in calls:
                card = {"kind": "tool", "n": len(cards) + 1, "status": "running"}
                cards.append(card)
                yield from run_tool(cfg, chat_id, c, card, mcp_index, interactive)
                syslog_out.tool(cfg, chat_id, card)
                msgs.append({"role": "tool", "tool_name": card["tool"], "content": card.pop("_for_model")})
            stats["tool_calls"] = sum(1 for k in cards if k.get("kind") == "tool")
        page = reports_mod.page_in_answer(content) if REPORTS is not None and not (res and res.get("report")) else None
        if page:
            # The model wrote a whole web page ("return it as an html page"): save it as a report
            # of kind "page", show it under the answer, and keep the code out of the answer text.
            try:
                meta = REPORTS.save_page(page[0], question, model, chat_id)
                content = page[1]
                card = {"kind": "page", "n": len(cards) + 1, "title": meta["title"],
                        "report": {"id": meta["id"], "title": meta["title"], "kind": "page",
                                   "url": f"/reports/{meta['id']}.html"}}
                cards.append(card)
                yield sse("page", {"card": card, "content": content})
            except Exception as e:  # a failed save must not lose the answer
                log.exception("page save failed")
                stats["report_error"] = str(e)
        if res and res.get("report") and REPORTS is not None:
            # The HTML report: built from the results (and the summary just written), saved,
            # and linked from the card.
            try:
                meta = REPORTS.create(res, question, content, model, chat_id)
                cards[0]["report"] = {"id": meta["id"], "title": meta["title"], "kind": meta["kind"],
                                      "url": f"/reports/{meta['id']}.html"}
                yield sse("report", cards[0]["report"])
            except Exception as e:  # a failed report must not lose the answer
                log.exception("report failed")
                stats["report_error"] = str(e)
        finished = True
        yield sse("done", {"stats": stats, "model": model})
    except OllamaError as e:
        content = content or ""
        stats = {"error": str(e)}
        yield sse("error", {"message": str(e), "chat_id": chat_id})
    finally:
        # Saved even when the browser stops the stream half way.
        if content or stats.get("error"):
            db.add_message(chat_id, "assistant", content or f"_{stats.get('error')}_", thinking=thinking,
                           sources=cards, model=model, stats=stats)
        syslog_out.finish(cfg, chat_id, model, thinking, content, stats, finished)


def run_tool(cfg: dict[str, Any], chat_id: str, call: dict[str, Any], card: dict[str, Any],
             index: dict[str, Any], interactive: bool) -> Iterator[str]:
    """Run one tool call of the model, filling `card` (shown in the chat) and
    card["_for_model"] (the tool message). Yields tool events, and keep-alive comments
    while a Run / Cancel question waits for the reader."""
    fn = call.get("function") or {}
    name, args = str(fn.get("name") or "?"), fn.get("arguments") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args) or {}
        except ValueError:
            args = {}
    if not isinstance(args, dict):
        args = {}
    card.update(tool=name, arguments=args)
    n = card["n"]
    if name not in index or name.startswith("_"):
        card.update(status="error", error=f"There is no tool named {name}.")
        card["_for_model"] = f"[{n}] Error: there is no tool named {name}. Use only the tools you were given."
        yield sse("tool", {k: v for k, v in card.items() if k != "_for_model"})
        return
    server, tool = index[name]
    if server == websearch.SERVER:
        yield from run_web_tool(cfg, card, tool["name"], args, index.get("_web_order"))
        return
    card.update(tool=tool["name"], server=server, read_only=mcp.read_only(tool))
    pol = mcp.policy_of(mcp.load_policy(cfg.get("mcp_tools")), server, tool)
    if pol == "ask":
        if not interactive:
            card.update(status="refused", error="This tool asks first, which works only in the chat page.")
        else:
            key = mcp.APPROVALS.new(chat_id)
            card.update(status="waiting", approve=key)
            yield sse("tool", {k: v for k, v in card.items() if k != "_for_model"})
            try:
                answer, waited = None, 0
                while answer is None and waited < 900:
                    answer = mcp.APPROVALS.wait(key, 15)
                    waited += 15
                    if answer is None:
                        yield ": waiting for Run or Cancel\n\n"
            finally:
                mcp.APPROVALS.drop(key)
                card.pop("approve", None)
            if not answer:
                card.update(status="cancelled", error="Cancelled in the chat." if answer is False else "Nobody answered in 15 minutes.")
    if card["status"] in ("refused", "cancelled"):
        card["_for_model"] = f"[{n}] The tool {tool['name']} was not run: {card['error']}"
        yield sse("tool", {k: v for k, v in card.items() if k != "_for_model"})
        return
    card["status"] = "running"
    yield sse("tool", {k: v for k, v in card.items() if k != "_for_model"})
    res = mcp.call(cfg.get("agent_url") or "", server, tool["name"], args)
    card["ms"] = res["ms"]
    if res["ok"]:
        limit = max(1000, int(cfg.get("mcp_result_chars") or 16000))
        text = res["text"]
        cut = len(text) > limit
        card.update(status="ok", result=mcp.pretty(text)[:20000], chars=len(text), cut=cut)
        card["_for_model"] = (f"[{n}] Result of {tool['name']} (MCP server {server})"
                              + (f", first {limit} of {len(text)} characters" if cut else "") + f":\n{text[:limit]}")
    else:
        card.update(status="error", error=res["error"][:2000])
        card["_for_model"] = f"[{n}] The tool {tool['name']} failed: {res['error'][:2000]}"
    yield sse("tool", {k: v for k, v in card.items() if k != "_for_model"})


def run_web_tool(cfg: dict[str, Any], card: dict[str, Any], tool: str, args: dict[str, Any],
                 order: dict[str, Any] | None = None) -> Iterator[str]:
    """web_search / web_fetch / brave_search: read-only, so they run without a Run / Cancel question.

    `order` is set when both providers are ticked with a fixed order ({"first", "second",
    "searched"}): a search with the second provider before the first one ran runs the first
    instead, and a first search that fails or finds nothing is followed by the second
    provider automatically, in the same card. After that the model may search the second."""
    n = card["n"]
    searches = ("web_search", websearch.BRAVE_TOOL)
    if order is not None and tool in searches and tool != order["first"] and not order["searched"]:
        card["note"] = f"Fixed order: {websearch.label(order['first'])} searches first."
        tool = order["first"]
    card.update(tool=tool, server=websearch.label(tool), read_only=True, status="running")
    yield sse("tool", {k: v for k, v in card.items() if k != "_for_model"})
    res = websearch.call(cfg, tool, args)
    ms = res["ms"]
    if order is not None and tool == order["first"]:
        order["searched"] = True
        if not res["ok"] or not res["links"]:
            why = res["error"] if not res["ok"] else "no results"
            first, second = websearch.label(tool), websearch.label(order["second"])
            tool = f"{order['first']} → {order['second']}"
            card.update(tool=tool, server=f"{first}, then {second}", note=f"{first}: {why}. Searched {second} instead.")
            yield sse("tool", {k: v for k, v in card.items() if k != "_for_model"})
            nxt = websearch.call(cfg, order["second"], args)
            ms += nxt["ms"]
            if nxt["ok"]:
                nxt = {**nxt, "text": f"{first}: {why}. {second} results instead:\n\n{nxt['text']}"}
            else:
                nxt = {**nxt, "error": f"{first}: {why}; {second}: {nxt['error']}"}
            res = nxt
    card["ms"] = ms
    if res["ok"]:
        limit = max(1000, int(cfg.get("mcp_result_chars") or 16000))
        text = res["text"]
        cut = len(text) > limit
        card.update(status="ok", result=text[:20000], chars=len(text), cut=cut, links=res["links"][:10])
        card["_for_model"] = (f"[{n}] Result of {tool} (web)" + (f", first {limit} of {len(text)} characters" if cut else "")
                              + f":\n{text[:limit]}")
    else:
        card.update(status="error", error=res["error"][:2000])
        card["_for_model"] = f"[{n}] The tool {tool} failed: {res['error'][:2000]}"
    yield sse("tool", {k: v for k, v in card.items() if k != "_for_model"})


def answer(db: DB, cfg: dict[str, Any], chat_id: str, question: str, sources: list[str] | None,
           tools: list[str] | None = None) -> dict[str, Any]:
    """Non-streaming version for the REST API."""
    text, cards, err, stats = "", [], None, {}
    for chunk in answer_stream(db, cfg, chat_id, question, sources, tools, interactive=False):
        if chunk.startswith(":"):
            continue
        event, _, data = chunk.partition("\ndata: ")
        payload = json.loads(data)
        if event.endswith("token"):
            text += payload
        elif event.endswith("meta"):
            cards = payload["sources"]
        elif event.endswith("report") and cards:
            cards[0]["report"] = payload
        elif event.endswith("page"):
            text = payload["content"]
            cards = cards + [payload["card"]]
        elif event.endswith("tool"):
            cards = [c for c in cards if c.get("n") != payload.get("n")] + [payload]
        elif event.endswith("done"):
            stats = payload["stats"]
        elif event.endswith("error"):
            err = payload["message"]
    return {"chat_id": chat_id, "answer": text, "sources": cards, "stats": stats, "error": err}
