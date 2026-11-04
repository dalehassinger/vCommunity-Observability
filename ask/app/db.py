"""SQLite storage: one file (data/ask.db) holds settings, sources, documents,
chunks, the FTS5 keyword index, the sqlite-vec vector index and chat history.

Connections are short-lived (one per call) and WAL mode lets the indexer write
while the chat reads."""

from __future__ import annotations

import json
import sqlite3
import struct
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

import sqlite_vec

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sources (
  name TEXT PRIMARY KEY, plugin TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 1, interval_hours REAL NOT NULL DEFAULT 24,
  options TEXT NOT NULL DEFAULT '{}', examples TEXT NOT NULL DEFAULT '[]',
  origin TEXT NOT NULL DEFAULT 'ui', position INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS source_state (
  name TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'never', progress TEXT NOT NULL DEFAULT '',
  last_start REAL, last_end REAL, last_ok REAL, last_error TEXT NOT NULL DEFAULT '',
  version TEXT NOT NULL DEFAULT '', root TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS documents (
  id INTEGER PRIMARY KEY, source TEXT NOT NULL, doc_id TEXT NOT NULL, title TEXT NOT NULL,
  path TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT '',
  meta TEXT NOT NULL DEFAULT '{}', links TEXT NOT NULL DEFAULT '{}', images TEXT NOT NULL DEFAULT '[]',
  text TEXT NOT NULL DEFAULT '', hash TEXT NOT NULL, updated REAL NOT NULL,
  UNIQUE (source, doc_id));
CREATE TABLE IF NOT EXISTS chunks (
  id INTEGER PRIMARY KEY, document INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  source TEXT NOT NULL, ord INTEGER NOT NULL, heading TEXT NOT NULL DEFAULT '', text TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS chunks_doc ON chunks(document);
CREATE INDEX IF NOT EXISTS chunks_source ON chunks(source);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(title, heading, text, tokenize='porter unicode61');
CREATE TABLE IF NOT EXISTS chats (id TEXT PRIMARY KEY, title TEXT NOT NULL, scope TEXT NOT NULL DEFAULT '',
  created REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY, chat TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE, role TEXT NOT NULL,
  content TEXT NOT NULL, thinking TEXT NOT NULL DEFAULT '', sources TEXT NOT NULL DEFAULT '[]',
  model TEXT NOT NULL DEFAULT '', stats TEXT NOT NULL DEFAULT '{}', created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS messages_chat ON messages(chat, id);
"""


def pack(vec: Iterable[float]) -> bytes:
    v = list(vec)
    return struct.pack(f"{len(v)}f", *v)


class DB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        with self.conn() as c:
            c.executescript(SCHEMA)
            c.execute("PRAGMA journal_mode=WAL")

    @contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        c.enable_load_extension(True)
        sqlite_vec.load(c)
        c.enable_load_extension(False)
        try:
            yield c
            c.commit()
        finally:
            c.close()

    # -- settings -------------------------------------------------------------
    def get_settings(self) -> dict[str, Any]:
        with self.conn() as c:
            return {r["key"]: json.loads(r["value"]) for r in c.execute("SELECT key, value FROM settings")}

    def set_setting(self, key: str, value: Any) -> None:
        with self.conn() as c:
            c.execute("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (key, json.dumps(value)))

    def delete_setting(self, key: str) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM settings WHERE key=?", (key,))

    # -- sources --------------------------------------------------------------
    def list_sources(self) -> list[dict[str, Any]]:
        with self.conn() as c:
            rows = c.execute("""SELECT s.*, st.status, st.progress, st.last_start, st.last_end, st.last_ok,
                                   st.last_error, st.version, st.root,
                                   (SELECT COUNT(*) FROM documents d WHERE d.source=s.name) AS docs,
                                   (SELECT COUNT(*) FROM chunks k WHERE k.source=s.name) AS chunks
                                FROM sources s LEFT JOIN source_state st ON st.name=s.name
                                ORDER BY s.position, s.name""").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["options"] = json.loads(d["options"] or "{}")
            d["examples"] = json.loads(d["examples"] or "[]")
            d["enabled"] = bool(d["enabled"])
            d["status"] = d["status"] or "never"
            out.append(d)
        return out

    def get_source(self, name: str) -> dict[str, Any] | None:
        return next((s for s in self.list_sources() if s["name"] == name), None)

    def upsert_source(self, src: dict[str, Any]) -> None:
        with self.conn() as c:
            pos = c.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM sources").fetchone()[0]
            c.execute("""INSERT INTO sources(name, plugin, title, enabled, interval_hours, options, examples, origin, position)
                         VALUES(:name, :plugin, :title, :enabled, :interval_hours, :options, :examples, :origin, :position)
                         ON CONFLICT(name) DO UPDATE SET plugin=excluded.plugin, title=excluded.title,
                           enabled=excluded.enabled, interval_hours=excluded.interval_hours,
                           options=excluded.options, examples=excluded.examples""",
                      {"name": src["name"], "plugin": src["plugin"], "title": src.get("title") or src["name"],
                       "enabled": 1 if src.get("enabled", True) else 0,
                       "interval_hours": float(src.get("interval_hours") or 24),
                       "options": json.dumps(src.get("options") or {}),
                       "examples": json.dumps(src.get("examples") or []),
                       "origin": src.get("origin", "ui"), "position": pos})

    def set_source_enabled(self, name: str, enabled: bool) -> None:
        with self.conn() as c:
            c.execute("UPDATE sources SET enabled=? WHERE name=?", (1 if enabled else 0, name))

    def delete_source(self, name: str) -> None:
        with self._write_lock, self.conn() as c:
            ids = [r[0] for r in c.execute("SELECT id FROM chunks WHERE source=?", (name,))]
            self._delete_chunk_index(c, ids)
            c.execute("DELETE FROM documents WHERE source=?", (name,))
            c.execute("DELETE FROM source_state WHERE name=?", (name,))
            c.execute("DELETE FROM sources WHERE name=?", (name,))

    def set_state(self, name: str, **fields: Any) -> None:
        if "state" in fields and not isinstance(fields["state"], str):
            fields["state"] = json.dumps(fields["state"])
        with self.conn() as c:
            c.execute("INSERT OR IGNORE INTO source_state(name) VALUES(?)", (name,))
            if fields:
                cols = ", ".join(f"{k}=:{k}" for k in fields)
                c.execute(f"UPDATE source_state SET {cols} WHERE name=:name", {**fields, "name": name})

    def get_state(self, name: str) -> dict[str, Any]:
        with self.conn() as c:
            r = c.execute("SELECT * FROM source_state WHERE name=?", (name,)).fetchone()
        if not r:
            return {"state": {}}
        d = dict(r)
        d["state"] = json.loads(d["state"] or "{}")
        return d

    # -- vectors --------------------------------------------------------------
    def vec_dim(self) -> int | None:
        with self.conn() as c:
            r = c.execute("SELECT sql FROM sqlite_master WHERE name='chunks_vec'").fetchone()
        if not r:
            return None
        import re
        m = re.search(r"float\[(\d+)\]", r["sql"])
        return int(m.group(1)) if m else None

    def ensure_vec(self, dim: int) -> None:
        """Create the vector table for this embedding size. A different size means a
        different embedding model: drop the vectors so everything is re-embedded."""
        current = self.vec_dim()
        if current == dim:
            return
        with self._write_lock, self.conn() as c:
            if current is not None:
                c.execute("DROP TABLE chunks_vec")
                c.execute("UPDATE documents SET hash='' ")  # force re-embedding on next sync
            c.execute(f"CREATE VIRTUAL TABLE chunks_vec USING vec0(embedding float[{dim}] distance_metric=cosine)")

    def reset_index(self) -> None:
        """Drop every document, chunk and vector (used when the embedding model changes)."""
        with self._write_lock, self.conn() as c:
            c.execute("DELETE FROM chunks_fts")
            c.execute("DROP TABLE IF EXISTS chunks_vec")
            c.execute("DELETE FROM chunks")
            c.execute("DELETE FROM documents")
            c.execute("UPDATE source_state SET version='', state='{}'")

    def _delete_chunk_index(self, c: sqlite3.Connection, ids: list[int]) -> None:
        if not ids:
            return
        has_vec = c.execute("SELECT 1 FROM sqlite_master WHERE name='chunks_vec'").fetchone()
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            marks = ",".join("?" * len(part))
            c.execute(f"DELETE FROM chunks_fts WHERE rowid IN ({marks})", part)
            if has_vec:
                c.execute(f"DELETE FROM chunks_vec WHERE rowid IN ({marks})", part)

    # -- documents ------------------------------------------------------------
    def doc_hashes(self, source: str) -> dict[str, tuple[int, str]]:
        with self.conn() as c:
            return {r["doc_id"]: (r["id"], r["hash"])
                    for r in c.execute("SELECT id, doc_id, hash FROM documents WHERE source=?", (source,))}

    def save_document(self, source: str, doc: dict[str, Any], chunks: list[dict[str, Any]],
                      vectors: list[list[float]]) -> None:
        with self._write_lock, self.conn() as c:
            old = c.execute("SELECT id FROM documents WHERE source=? AND doc_id=?", (source, doc["id"])).fetchone()
            if old:
                ids = [r[0] for r in c.execute("SELECT id FROM chunks WHERE document=?", (old["id"],))]
                self._delete_chunk_index(c, ids)
                c.execute("DELETE FROM documents WHERE id=?", (old["id"],))
            cur = c.execute("""INSERT INTO documents(source, doc_id, title, path, url, status, meta, links, images, text, hash, updated)
                               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (source, doc["id"], doc.get("title") or doc["id"], doc.get("path", ""), doc.get("url", ""),
                             str(doc.get("status") or ""), json.dumps(doc.get("meta") or {}),
                             json.dumps(doc.get("links") or {}), json.dumps(doc.get("images") or []),
                             doc.get("text", ""), doc["hash"], time.time()))
            doc_row = cur.lastrowid
            for ch, vec in zip(chunks, vectors):
                k = c.execute("INSERT INTO chunks(document, source, ord, heading, text) VALUES(?,?,?,?,?)",
                              (doc_row, source, ch["ord"], ch["heading"], ch["text"])).lastrowid
                c.execute("INSERT INTO chunks_fts(rowid, title, heading, text) VALUES(?,?,?,?)",
                          (k, doc.get("title") or doc["id"], ch["heading"], ch["text"]))
                c.execute("INSERT INTO chunks_vec(rowid, embedding) VALUES(?, ?)", (k, pack(vec)))

    def delete_documents(self, source: str, doc_ids: list[str]) -> None:
        if not doc_ids:
            return
        with self._write_lock, self.conn() as c:
            for doc_id in doc_ids:
                r = c.execute("SELECT id FROM documents WHERE source=? AND doc_id=?", (source, doc_id)).fetchone()
                if not r:
                    continue
                ids = [x[0] for x in c.execute("SELECT id FROM chunks WHERE document=?", (r["id"],))]
                self._delete_chunk_index(c, ids)
                c.execute("DELETE FROM documents WHERE id=?", (r["id"],))

    def get_document(self, row_id: int) -> dict[str, Any] | None:
        with self.conn() as c:
            r = c.execute("SELECT * FROM documents WHERE id=?", (row_id,)).fetchone()
        return _doc(r) if r else None

    def find_documents(self, source: str | None, doc_ids: list[str]) -> list[dict[str, Any]]:
        if not doc_ids:
            return []
        marks = ",".join("?" * len(doc_ids))
        sql = f"SELECT * FROM documents WHERE doc_id IN ({marks})"
        args: list[Any] = list(doc_ids)
        if source:
            sql += " AND source=?"
            args.append(source)
        with self.conn() as c:
            return [_doc(r) for r in c.execute(sql, args)]

    def list_documents(self, source: str) -> list[dict[str, Any]]:
        with self.conn() as c:
            return [dict(r) for r in c.execute(
                """SELECT d.id, d.doc_id, d.title, d.path, d.url, d.status,
                          (SELECT COUNT(*) FROM chunks k WHERE k.document=d.id) AS chunks
                   FROM documents d WHERE d.source=? ORDER BY d.doc_id""", (source,))]

    # -- search ---------------------------------------------------------------
    def vector_search(self, vec: list[float], k: int, sources: list[str] | None) -> list[tuple[int, float]]:
        if self.vec_dim() != len(vec):
            return []
        with self.conn() as c:
            # Ask for more than k: results from other sources are filtered out afterwards.
            want = k * 4 if sources else k
            rows = c.execute("SELECT rowid, distance FROM chunks_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                             (pack(vec), want)).fetchall()
            if sources:
                allowed = _chunk_sources(c, [r[0] for r in rows])
                rows = [r for r in rows if allowed.get(r[0]) in sources]
        return [(r[0], r[1]) for r in rows[:k]]

    def keyword_search(self, match: str, k: int, sources: list[str] | None) -> list[tuple[int, float]]:
        if not match:
            return []
        with self.conn() as c:
            try:
                rows = c.execute("""SELECT f.rowid, bm25(chunks_fts, 4.0, 2.0, 1.0) AS score, k.source
                                    FROM chunks_fts f JOIN chunks k ON k.id=f.rowid
                                    WHERE chunks_fts MATCH ? ORDER BY score LIMIT ?""", (match, k * 4)).fetchall()
            except sqlite3.OperationalError:
                return []
        if sources:
            rows = [r for r in rows if r["source"] in sources]
        return [(r[0], r[1]) for r in rows[:k]]

    def get_chunks(self, ids: list[int]) -> dict[int, dict[str, Any]]:
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        with self.conn() as c:
            rows = c.execute(f"""SELECT k.id, k.ord, k.heading, k.text, k.source, d.id AS doc_row, d.doc_id, d.title,
                                        d.url, d.path, d.status, d.meta, d.links, d.images
                                 FROM chunks k JOIN documents d ON d.id=k.document WHERE k.id IN ({marks})""", ids)
            out = {}
            for r in rows:
                d = dict(r)
                d["links"] = json.loads(d["links"] or "{}")
                d["images"] = json.loads(d["images"] or "[]")
                d["meta"] = json.loads(d["meta"] or "{}")
                out[d["id"]] = d
            return out

    def first_chunks(self, doc_rows: list[int]) -> list[int]:
        if not doc_rows:
            return []
        marks = ",".join("?" * len(doc_rows))
        with self.conn() as c:
            return [r[0] for r in c.execute(
                f"SELECT MIN(id) FROM chunks WHERE document IN ({marks}) GROUP BY document", doc_rows)]

    def doc_chunk_ids(self, doc_row: int) -> list[int]:
        with self.conn() as c:
            return [r[0] for r in c.execute("SELECT id FROM chunks WHERE document=? ORDER BY ord", (doc_row,))]

    def stats(self) -> dict[str, int]:
        with self.conn() as c:
            return {"documents": c.execute("SELECT COUNT(*) FROM documents").fetchone()[0],
                    "chunks": c.execute("SELECT COUNT(*) FROM chunks").fetchone()[0],
                    "chats": c.execute("SELECT COUNT(*) FROM chats").fetchone()[0]}

    # -- chats ----------------------------------------------------------------
    def new_chat(self, title: str, scope: str) -> str:
        cid = uuid.uuid4().hex[:12]
        now = time.time()
        with self.conn() as c:
            c.execute("INSERT INTO chats(id, title, scope, created, updated) VALUES(?,?,?,?,?)",
                      (cid, title[:120], scope, now, now))
        return cid

    def get_chat(self, cid: str) -> dict[str, Any] | None:
        with self.conn() as c:
            r = c.execute("SELECT * FROM chats WHERE id=?", (cid,)).fetchone()
        return dict(r) if r else None

    def list_chats(self, limit: int = 30) -> list[dict[str, Any]]:
        with self.conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM chats ORDER BY updated DESC LIMIT ?", (limit,))]

    def delete_chat(self, cid: str) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM chats WHERE id=?", (cid,))

    def add_message(self, cid: str, role: str, content: str, *, thinking: str = "", sources: list | None = None,
                    model: str = "", stats: dict | None = None) -> int:
        now = time.time()
        with self.conn() as c:
            mid = c.execute("""INSERT INTO messages(chat, role, content, thinking, sources, model, stats, created)
                               VALUES(?,?,?,?,?,?,?,?)""",
                            (cid, role, content, thinking, json.dumps(sources or []), model,
                             json.dumps(stats or {}), now)).lastrowid
            c.execute("UPDATE chats SET updated=? WHERE id=?", (now, cid))
        return mid

    def messages(self, cid: str) -> list[dict[str, Any]]:
        with self.conn() as c:
            rows = c.execute("SELECT * FROM messages WHERE chat=? ORDER BY id", (cid,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["sources"] = json.loads(d["sources"] or "[]")
            d["stats"] = json.loads(d["stats"] or "{}")
            out.append(d)
        return out


def _chunk_sources(c: sqlite3.Connection, ids: list[int]) -> dict[int, str]:
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    return {r[0]: r[1] for r in c.execute(f"SELECT id, source FROM chunks WHERE id IN ({marks})", ids)}


def _doc(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    for key, empty in (("meta", "{}"), ("links", "{}"), ("images", "[]")):
        d[key] = json.loads(d[key] or empty)
    return d
