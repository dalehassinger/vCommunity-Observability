"""Answer quality test: replay saved prompts with the current code and settings, and compare
speed and results with the saved answers (and with the previous test run).

One run at a time, in a background thread. Nothing is written to the chats or the activity
log: results go to the eval_runs / eval_items tables in ask.db.

What is replayed: questions from saved chats that were answered from knowledge sources (not
data/spreadsheet answers, whole-source overviews, MCP or web tools, or "No sources"). Each one
is asked again with the same Sources pick and the same earlier turns of its chat.

What is compared, per prompt:
  speed    - total seconds, retrieval ms, tokens/s, prompt tokens (saved answer vs replay)
  sources  - which documents were used (overlap), how many sections were reserved by balancing
  answers  - length, "not found" answers, and optionally a judge: the chat model reads the
             question, the replay's sources and both answers (in random order) and says which
             answer is better, with a 1-5 score for each.
"""
from __future__ import annotations

import json
import logging
import random
import re
import statistics
import threading
import time
from typing import Any, Callable

from . import rag
from .db import DB
from .ollama import Ollama, OllamaError

log = logging.getLogger("ask.eval")

TABLES = """
CREATE TABLE IF NOT EXISTS eval_runs (
  id INTEGER PRIMARY KEY, started REAL NOT NULL, finished REAL, status TEXT NOT NULL DEFAULT 'running',
  total INTEGER NOT NULL DEFAULT 0, done INTEGER NOT NULL DEFAULT 0, judge INTEGER NOT NULL DEFAULT 0,
  settings TEXT NOT NULL DEFAULT '{}', summary TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS eval_items (
  id INTEGER PRIMARY KEY, run INTEGER NOT NULL REFERENCES eval_runs(id) ON DELETE CASCADE,
  pos INTEGER NOT NULL, message INTEGER, chat TEXT NOT NULL DEFAULT '', question TEXT NOT NULL,
  scope TEXT NOT NULL DEFAULT '', old_model TEXT NOT NULL DEFAULT '', old_answer TEXT NOT NULL DEFAULT '',
  old_stats TEXT NOT NULL DEFAULT '{}', old_docs TEXT NOT NULL DEFAULT '[]',
  new_answer TEXT NOT NULL DEFAULT '', new_stats TEXT NOT NULL DEFAULT '{}', new_docs TEXT NOT NULL DEFAULT '[]',
  verdict TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'waiting', error TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS eval_items_run ON eval_items(run, pos);
"""

# The settings that change answers or speed; saved with each run and compared between runs.
SNAPSHOT_KEYS = ["chat_model", "judge_model", "embed_model", "top_k", "source_min_chunks", "context_chars", "num_ctx",
                 "temperature", "think", "history_turns", "expand_links", "keep_alive"]

NOT_FOUND = re.compile(r"\b(not (found|covered|mentioned|included|in the (provided )?sources)|"
                       r"(don't|do not|doesn't|does not) (have|contain|include|mention)|"
                       r"no (information|mention|details)|cannot (find|answer)|can't (find|answer))\b", re.I)
SKIP_SCOPE = re.compile(r"(^|,)\s*(mcp:|web:|none\b)")
MAX_PROMPTS = 50

JUDGE_PROMPT = """You grade answers from a documentation assistant. You get a question, the
source sections the assistant could use, and two answers. Decide which answer is better: more
correct, more complete for what was asked, better grounded in the sources, and clearer. Do not
prefer an answer only because it is longer. An answer that says the information is not there,
when the sources do contain it, is worse. Facts not in these sections are not wrong for that
reason alone (the other answer may have used other sections), but invented-looking claims are.

Reply with JSON only, no other text:
{"better": 1 or 2 or 0 (0 = about the same), "score1": 1-5, "score2": 1-5, "reason": "one short sentence"}"""


class EvalRunner:
    def __init__(self, db: DB, settings: Callable[[], dict[str, Any]], scope: Callable[[str], list[str] | None]):
        self.db, self.settings, self.scope = db, settings, scope
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        with db.conn() as c:
            c.executescript(TABLES)
            # A restart ends any run that was going.
            c.execute("UPDATE eval_runs SET status='stopped', finished=? WHERE status='running'", (time.time(),))

    # -- what can be replayed -----------------------------------------------------------
    def candidates(self, limit: int = 10) -> list[dict[str, Any]]:
        """The most recent saved prompts that were answered from knowledge sources, newest first,
        one per distinct question."""
        with self.db.conn() as c:
            rows = c.execute("""
                SELECT u.id, u.chat, u.content AS question, ch.scope, a.content AS answer, a.sources, a.model,
                       a.stats, a.created
                FROM messages u JOIN chats ch ON ch.id = u.chat
                JOIN messages a ON a.id = (SELECT MIN(id) FROM messages WHERE chat = u.chat AND id > u.id)
                WHERE u.role = 'user' AND a.role = 'assistant'
                ORDER BY u.id DESC LIMIT ?""", (max(limit, 1) * 20,)).fetchall()
        out, seen = [], set()
        for r in rows:
            d = dict(r)
            key = " ".join(d["question"].lower().split())
            if not key or key in seen:
                continue
            stats = json.loads(d["stats"] or "{}")
            cards = json.loads(d["sources"] or "[]")
            if (SKIP_SCOPE.search(d["scope"] or "") or stats.get("error") or stats.get("overview")
                    or stats.get("tool_calls") or stats.get("data_error")
                    or any(k.get("kind") in ("data", "tool", "page") for k in cards)
                    or not d["answer"] or d["answer"].startswith("_")):
                continue
            seen.add(key)
            d["stats"], d["sources"] = stats, cards
            out.append(d)
            if len(out) >= limit:
                break
        return out

    # -- runs ---------------------------------------------------------------------------
    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self, count: int = 10, judge: bool = True, model: str = "", judge_model: str = "") -> int:
        """model: answer with this chat model (default: the saved Chat model; the saved setting is
        not changed). judge_model: the model that grades (default: the answering model). Keep the
        judge the same across runs to compare models fairly."""
        with self._lock:
            if self.running:
                raise RuntimeError("A test is already running. Wait for it or stop it first.")
            count = max(1, min(int(count), MAX_PROMPTS))
            prompts = self.candidates(count)
            if not prompts:
                raise ValueError("No saved prompts to replay yet. Ask a few questions from knowledge sources in the chat first.")
            cfg = self.settings()
            model = (model or "").strip() or cfg["chat_model"]
            judge_model = ((judge_model or "").strip() or model) if judge else ""
            snap = {k: cfg.get(k) for k in SNAPSHOT_KEYS}
            snap.update(chat_model=model, judge_model=judge_model)
            with self.db.conn() as c:
                rid = c.execute("INSERT INTO eval_runs(started, total, judge, settings) VALUES(?,?,?,?)",
                                (time.time(), len(prompts), 1 if judge else 0, json.dumps(snap))).lastrowid
                for pos, p in enumerate(prompts):
                    c.execute("""INSERT INTO eval_items(run, pos, message, chat, question, scope, old_model, old_answer,
                                 old_stats, old_docs) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                              (rid, pos, p["id"], p["chat"], p["question"], p["scope"] or "", p["model"] or "",
                               p["answer"], json.dumps(p["stats"]), json.dumps(docs_of(p["sources"]))))
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, args=(rid, model, judge_model), daemon=True, name=f"eval-{rid}")
            self._thread.start()
            return rid

    def stop(self) -> None:
        self._stop.set()

    def _run(self, rid: int, model: str, judge_model: str) -> None:
        status, error = "done", ""
        try:
            for item in self.items(rid):
                if self._stop.is_set():
                    status = "stopped"
                    break
                self._replay(item, model, judge_model)
                with self.db.conn() as c:
                    c.execute("UPDATE eval_runs SET done = done + 1 WHERE id=?", (rid,))
        except Exception as e:  # keep what was measured so far
            log.exception("eval run %s failed", rid)
            status, error = "failed", str(e)
        with self.db.conn() as c:
            c.execute("UPDATE eval_runs SET status=?, finished=?, error=?, summary=? WHERE id=?",
                      (status, time.time(), error, json.dumps(summarize(self.items(rid))), rid))

    def _replay(self, item: dict[str, Any], model: str, judge_model: str) -> None:
        cfg = {**self.settings(), "chat_model": model}
        self._set_item(item["id"], status="running")
        try:
            history = [m for m in self.db.messages(item["chat"]) if m["id"] < (item["message"] or 0)]
            sources = self.scope(item["scope"] or "all")
            t0 = time.time()
            info: dict[str, Any] = {}
            items = rag.retrieve(self.db, cfg, item["question"], sources, info)
            retrieval_ms = int((time.time() - t0) * 1000)
            msgs = rag.build_messages(cfg, items, item["question"], history)
            content, first, stats = "", None, {}
            olla = Ollama(cfg["ollama_url"])
            for obj in olla.chat_stream(cfg["chat_model"], msgs, think=cfg["think"], keep_alive=cfg["keep_alive"],
                                        options={"temperature": cfg["temperature"], "num_ctx": cfg["num_ctx"]}):
                if self._stop.is_set():
                    raise OllamaError("Stopped")
                msg = obj.get("message") or {}
                if msg.get("content"):
                    if first is None:
                        first = time.time()
                    content += msg["content"]
                if obj.get("done"):
                    ev, ed = obj.get("eval_count") or 0, obj.get("eval_duration") or 0
                    stats = {"tokens": ev, "tok_per_s": round(ev / (ed / 1e9), 1) if ed else None,
                             "prompt_tokens": obj.get("prompt_eval_count")}
            stats.update({"seconds": round(time.time() - t0, 1), "retrieval_ms": retrieval_ms,
                          "first_token_s": round(first - t0, 1) if first else None,
                          "retrieval": info.get("retrieval"), "model": cfg["chat_model"]})
            cards = rag.source_cards(items)
            new_docs = docs_of(cards)
            verdict: dict[str, Any] = {}
            if judge_model and content:
                verdict = judge_answers(olla, {**cfg, "chat_model": judge_model}, item["question"], items,
                                        item["old_answer"], content)
            self._set_item(item["id"], status="done", new_answer=content, new_stats=json.dumps(stats),
                           new_docs=json.dumps(new_docs), verdict=json.dumps(verdict))
        except Exception as e:
            self._set_item(item["id"], status="error", error=str(e)[:500])

    def _set_item(self, iid: int, **fields: Any) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.db.conn() as c:
            c.execute(f"UPDATE eval_items SET {cols} WHERE id=?", (*fields.values(), iid))

    # -- reading results ----------------------------------------------------------------
    def runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.db.conn() as c:
            rows = c.execute("SELECT * FROM eval_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [_run_row(r) for r in rows]

    def run(self, rid: int, vs: int | None = None) -> dict[str, Any] | None:
        """One run with its items, compared with run `vs` (default: the previous finished run)."""
        with self.db.conn() as c:
            r = c.execute("SELECT * FROM eval_runs WHERE id=?", (rid,)).fetchone()
            if vs:
                prev = c.execute("SELECT * FROM eval_runs WHERE id=? AND id != ?", (vs, rid)).fetchone()
            else:
                prev = c.execute("SELECT * FROM eval_runs WHERE id<? AND status IN ('done','stopped') AND done>0 "
                                 "ORDER BY id DESC LIMIT 1", (rid,)).fetchone()
        if not r:
            return None
        run = _run_row(r)
        run["items"] = [_with_metrics(i) for i in self.items(rid)]
        if run["status"] == "running" or not run["summary"]:
            run["summary"] = summarize(self.items(rid))
        run["previous"] = _run_row(prev) if prev else None
        if prev:
            other = {i["question"]: _with_metrics(i) for i in self.items(prev["id"]) if i["status"] == "done"}
            for i in run["items"]:
                o = other.get(i["question"])
                if o:
                    i["other"] = {"seconds": o["new_stats"].get("seconds"), "tok_per_s": o["new_stats"].get("tok_per_s"),
                                  "score": o["verdict"].get("new_score"), "not_found": o["new_not_found"],
                                  "model": o["new_stats"].get("model"), "answer": o["new_answer"]}
            run["same_questions"] = sum(1 for i in run["items"] if i.get("other"))
        if run["previous"]:
            run["changed"] = {k: [run["previous"]["settings"].get(k), run["settings"].get(k)] for k in SNAPSHOT_KEYS
                              if run["previous"]["settings"].get(k) != run["settings"].get(k)}
        return run

    def items(self, rid: int) -> list[dict[str, Any]]:
        with self.db.conn() as c:
            rows = c.execute("SELECT * FROM eval_items WHERE run=? ORDER BY pos", (rid,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for k in ("old_stats", "new_stats", "old_docs", "new_docs", "verdict"):
                d[k] = json.loads(d[k] or ("[]" if k.endswith("docs") else "{}"))
            out.append(d)
        return out

    def delete(self, rid: int) -> None:
        with self.db.conn() as c:
            c.execute("DELETE FROM eval_items WHERE run=?", (rid,))
            c.execute("DELETE FROM eval_runs WHERE id=? AND status != 'running'", (rid,))


# -- helpers ------------------------------------------------------------------------------
def docs_of(cards: list[dict[str, Any]]) -> list[str]:
    """The documents an answer used, as "source/doc id" (sections of one document count once)."""
    out: list[str] = []
    for k in cards or []:
        if k.get("kind") in ("hit", "linked") and k.get("source"):
            key = f"{k['source']}/{k.get('doc_id', '')}"
            if key not in out:
                out.append(key)
    return out


def _run_row(r: Any) -> dict[str, Any]:
    d = dict(r)
    d["settings"] = json.loads(d["settings"] or "{}")
    d["summary"] = json.loads(d["summary"] or "{}")
    return d


def _with_metrics(i: dict[str, Any]) -> dict[str, Any]:
    old, new = set(i["old_docs"]), set(i["new_docs"])
    i["overlap"] = round(len(old & new) / len(old | new), 2) if (old | new) else None
    i["docs_added"] = sorted(new - old)
    i["docs_dropped"] = sorted(old - new)
    i["old_not_found"] = bool(NOT_FOUND.search(i["old_answer"] or ""))
    i["new_not_found"] = bool(NOT_FOUND.search(i["new_answer"] or "")) if i["status"] == "done" else None
    return i


def _median(values: list[float]) -> float | None:
    vals = [v for v in values if isinstance(v, (int, float))]
    if not vals:
        return None
    m = round(statistics.median(vals), 1)
    return int(m) if float(m).is_integer() else m


def summarize(items: list[dict[str, Any]]) -> dict[str, Any]:
    done = [_with_metrics(dict(i)) for i in items if i["status"] == "done"]
    s: dict[str, Any] = {"prompts": len(items), "done": len(done), "errors": sum(1 for i in items if i["status"] == "error")}
    for key in ("seconds", "tok_per_s", "prompt_tokens", "retrieval_ms", "tokens"):
        s[f"old_{key}"] = _median([i["old_stats"].get(key) for i in done])
        s[f"new_{key}"] = _median([i["new_stats"].get(key) for i in done])
    s["new_first_token_s"] = _median([i["new_stats"].get("first_token_s") for i in done])
    overlaps = [i["overlap"] for i in done if i["overlap"] is not None]
    s["overlap"] = round(sum(overlaps) / len(overlaps), 2) if overlaps else None
    s["old_not_found"] = sum(1 for i in done if i["old_not_found"])
    s["new_not_found"] = sum(1 for i in done if i["new_not_found"])
    s["reserved"] = sum(((i["new_stats"].get("retrieval") or {}).get("reserved") or 0) for i in done)
    judged = [i["verdict"] for i in done if i["verdict"].get("better") in (0, "old", "new")]
    s["judged"] = len(judged)
    s["new_better"] = sum(1 for v in judged if v["better"] == "new")
    s["old_better"] = sum(1 for v in judged if v["better"] == "old")
    s["same"] = sum(1 for v in judged if v["better"] == 0)
    s["old_score"] = _avg([v.get("old_score") for v in judged])
    s["new_score"] = _avg([v.get("new_score") for v in judged])
    return s


def _avg(values: list[Any]) -> float | None:
    vals = [v for v in values if isinstance(v, (int, float))]
    return round(sum(vals) / len(vals), 2) if vals else None


def judge_answers(olla: Ollama, cfg: dict[str, Any], question: str, items: list[dict[str, Any]],
                  old: str, new: str, limit: int = 12000) -> dict[str, Any]:
    """Ask the chat model which answer is better. The two answers go in random order so the
    model's habit of preferring the first (or second) one doesn't decide."""
    blocks, size = [], 0
    for it in items:
        block = f"[{it.get('n', '')}] {it['title']}" + (f" > {it['heading']}" if it.get("heading") else "") + f"\n{it['text']}"
        if size + len(block) > limit:
            block = block[: max(0, limit - size)]
        blocks.append(block)
        size += len(block)
        if size >= limit:
            break
    flip = random.random() < 0.5
    first, second = (new, old) if flip else (old, new)
    msgs = [{"role": "system", "content": JUDGE_PROMPT},
            {"role": "user", "content": f"Question: {question}\n\nSource sections:\n\n" + "\n\n---\n\n".join(blocks)
             + f"\n\n=====\n\nAnswer 1:\n{first}\n\n=====\n\nAnswer 2:\n{second}"}]
    try:
        raw = olla.chat(cfg["chat_model"], msgs, options={"temperature": 0, "num_ctx": cfg["num_ctx"]},
                        think=False, keep_alive=cfg["keep_alive"])
    except OllamaError as e:
        return {"error": str(e)[:300]}
    m = re.search(r"\{.*\}", raw, re.S)
    try:
        data = json.loads(m.group(0)) if m else {}
    except ValueError:
        data = {}
    if not data:
        return {"error": "The judge did not reply with JSON", "raw": raw[:300]}

    def num(v: Any) -> int | None:
        try:
            return max(1, min(5, int(v)))
        except (TypeError, ValueError):
            return None

    better = str(data.get("better", "0")).strip()
    s1, s2 = num(data.get("score1")), num(data.get("score2"))
    pick = {"1": "new" if flip else "old", "2": "old" if flip else "new"}.get(better, 0)
    return {"better": pick, "old_score": s2 if flip else s1, "new_score": s1 if flip else s2,
            "reason": str(data.get("reason", ""))[:300], "model": cfg["chat_model"]}
