"""Retrieval and answering.

1. Hybrid search: sqlite-vec similarity + FTS5 keyword (BM25), merged with
   reciprocal rank fusion, at most two chunks per document.
2. Link expansion: files named in same_name, internal_derive, same_meaning and
   related are added (same_name first: it stops the model mixing the VM and the
   ESXi meaning of the same metric label).
3. The numbered sources go to the Ollama chat model with the question and the
   last few turns of the chat; the answer streams back token by token."""

from __future__ import annotations

import json
import posixpath
import re
import time
from typing import Any, Iterator
from urllib.parse import quote

from . import data
from .db import DB
from .indexer import embed_prefixes

log = __import__("logging").getLogger("ask.rag")
REPORTS = None   # app.reports.Reports, set by create_app; makes HTML reports for report questions
from .ollama import Ollama, OllamaError

RRF_K = 60
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


def retrieve(db: DB, cfg: dict[str, Any], question: str, sources: list[str] | None) -> list[dict[str, Any]]:
    model = cfg["embed_model"]
    _, qprefix = embed_prefixes(model)
    ollama = Ollama(cfg["ollama_url"])
    qvec = ollama.embed(model, [qprefix + question], keep_alive=cfg["keep_alive"])[0]
    pool = max(30, cfg["top_k"] * 4)
    vec = db.vector_search(qvec, pool, sources)
    kw = db.keyword_search(fts_query(question), pool, sources)

    score: dict[int, float] = {}
    for hits in (vec, kw):
        for rank, (cid, _) in enumerate(hits):
            score[cid] = score.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)
    ranked = sorted(score, key=score.get, reverse=True)
    chunks = db.get_chunks(ranked[: pool])

    picked: list[dict[str, Any]] = []
    per_doc: dict[int, int] = {}
    for cid in ranked:
        ch = chunks.get(cid)
        if not ch or per_doc.get(ch["doc_row"], 0) >= 2:
            continue
        per_doc[ch["doc_row"]] = per_doc.get(ch["doc_row"], 0) + 1
        picked.append({**ch, "kind": "hit", "score": round(score[cid], 4)})
        if len(picked) >= cfg["top_k"]:
            break

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
    budget = cfg["context_chars"]
    out: list[dict[str, Any]] = []
    for item in picked + linked:
        size = len(item["text"]) + 200
        if out and budget - size < 0:
            continue
        budget -= size
        out.append(item)
    for n, item in enumerate(out, 1):
        item["n"] = n
    return out


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
                      "kind": it["kind"], "via": it.get("via", ""), "from_doc": it.get("from_doc", ""),
                      "snippet": _snippet(it["text"]), "images": images[:3]})
    return cards


def _snippet(text: str, limit: int = 280) -> str:
    lines = [l for l in text.splitlines() if l.strip() and not l.startswith("#") and not l.startswith("![")
             and not l.lstrip().startswith("|") and not l.startswith("```")
             and not l.startswith("Published: ")]
    s = " ".join(l.strip().lstrip("-* ").strip() for l in lines)
    return s[:limit] + ("…" if len(s) > limit else "")


def build_messages(cfg: dict[str, Any], items: list[dict[str, Any]], question: str,
                   history: list[dict[str, Any]]) -> list[dict[str, str]]:
    blocks = []
    for it in items:
        head = f"[{it['n']}] id: {it['doc_id']} | {it['title']}"
        if it["heading"]:
            head += f" > {it['heading']}"
        if it["status"]:
            head += f" | status: {it['status']}"
        if (it.get("meta") or {}).get("published"):
            head += f" | published: {it['meta']['published']}"
        if it["kind"] == "linked":
            head += f" | linked ({it['via']}) from {it['from_doc']}"
        blocks.append(f"{head}\n{it['text']}")
    context = "\n\n---\n\n".join(blocks) if blocks else "(no sources found)"
    msgs = [{"role": "system", "content": cfg["system_prompt"]}]
    turns = max(0, int(cfg["history_turns"]))
    if turns:
        for m in history[-turns * 2:]:
            if m["role"] in ("user", "assistant") and m["content"]:
                msgs.append({"role": m["role"], "content": m["content"]})
    msgs.append({"role": "user", "content": f"Sources:\n\n{context}\n\n=====\n\nQuestion: {question}"})
    return msgs


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


def sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def answer_stream(db: DB, cfg: dict[str, Any], chat_id: str, question: str,
                  sources: list[str] | None) -> Iterator[str]:
    """Server-sent events: meta (chat id + source cards), thinking, token, done, error."""
    t0 = time.time()
    history = db.messages(chat_id)
    db.add_message(chat_id, "user", question)
    content, thinking, cards, stats = "", "", [], {}
    model = cfg["chat_model"]
    try:
        if db.stats()["chunks"] == 0:
            raise OllamaError("Nothing is indexed yet. Open Knowledge sources and press Sync now.")
        res = None
        srcs = data.tables_for(db, sources) if cfg.get("data_questions", True) else []
        if srcs and data.is_data_question(question, srcs):
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
            if res.get("error"):
                stats["data_error"] = res["error"]
                res = None
        if res:
            cards = [data.card(res)]
            yield sse("meta", {"chat_id": chat_id, "sources": cards, "model": model,
                               "retrieval_ms": int((time.time() - t0) * 1000)})
            msgs = build_data_messages(cfg, res, question, history)
        else:
            items = retrieve(db, cfg, question, sources)
            cards = source_cards(items)
            yield sse("meta", {"chat_id": chat_id, "sources": cards, "model": model,
                               "retrieval_ms": int((time.time() - t0) * 1000)})
            msgs = build_messages(cfg, items, question, history)
        options = {"temperature": cfg["temperature"], "num_ctx": cfg["num_ctx"]}
        for obj in Ollama(cfg["ollama_url"]).chat_stream(model, msgs, options=options, think=cfg["think"],
                                                          keep_alive=cfg["keep_alive"]):
            msg = obj.get("message") or {}
            if msg.get("thinking"):
                thinking += msg["thinking"]
                yield sse("thinking", msg["thinking"])
            if msg.get("content"):
                content += msg["content"]
                yield sse("token", msg["content"])
            if obj.get("done"):
                ev, ed = obj.get("eval_count") or 0, obj.get("eval_duration") or 0
                stats = {**stats, "tokens": ev, "tok_per_s": round(ev / (ed / 1e9), 1) if ed else None,
                         "prompt_tokens": obj.get("prompt_eval_count"),
                         "seconds": round(time.time() - t0, 1)}
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


def answer(db: DB, cfg: dict[str, Any], chat_id: str, question: str, sources: list[str] | None) -> dict[str, Any]:
    """Non-streaming version for the REST API."""
    text, cards, err, stats = "", [], None, {}
    for chunk in answer_stream(db, cfg, chat_id, question, sources):
        event, _, data = chunk.partition("\ndata: ")
        payload = json.loads(data)
        if event.endswith("token"):
            text += payload
        elif event.endswith("meta"):
            cards = payload["sources"]
        elif event.endswith("report") and cards:
            cards[0]["report"] = payload
        elif event.endswith("done"):
            stats = payload["stats"]
        elif event.endswith("error"):
            err = payload["message"]
    return {"chat_id": chat_id, "answer": text, "sources": cards, "stats": stats, "error": err}
