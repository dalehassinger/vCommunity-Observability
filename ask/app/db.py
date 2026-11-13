"""SQLite storage: one file (data/ask.db) holds settings, sources, documents,
chunks, the FTS5 keyword index, the sqlite-vec vector index and chat history.

Each thread keeps one connection (sqlite-vec loaded once), so SQLite's page cache
stays warm across the many small reads of one question. WAL mode lets the indexer
write while the chat reads; synchronous=NORMAL is durable enough for an index that
can be rebuilt.

Tuning (environment): ASK_SQLITE_CACHE_MB page cache per connection (default 64),
ASK_SQLITE_MMAP_MB memory-mapped I/O (default 0 = off; try it on a Linux volume,
measure it on a Docker Desktop for Mac file share before keeping it)."""

from __future__ import annotations

import json
import logging
import os
import re
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


class IndexReset(RuntimeError):
    """The index was reset (new embedding model) while a sync was embedding: its vectors
    belong to the old index, so the sync stops and runs again."""


def _env_mb(name: str, default: int) -> int:
    try:
        return max(0, int(float(os.environ.get(name, default))))
    except ValueError:
        return default


CACHE_MB = _env_mb("ASK_SQLITE_CACHE_MB", 64)
MMAP_MB = _env_mb("ASK_SQLITE_MMAP_MB", 0)
VEC_PARTITION = "source text partition key"


class DB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        self._local = threading.local()
        # Goes up by one at every reset_index(). A sync notes it when it starts and may
        # only write vectors while it is unchanged (see save_document).
        self.generation = 0
        # The vector table's size (dimensions), remembered so a search needs no schema read.
        # None = not read yet; 0 = no vector table.
        self._dim: int | None = None
        with self.conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SCHEMA)
        self._migrate_vec_partition()

    def _open(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute(f"PRAGMA cache_size=-{CACHE_MB * 1024}")
        if MMAP_MB:
            c.execute(f"PRAGMA mmap_size={MMAP_MB * 1024 * 1024}")
        c.enable_load_extension(True)
        sqlite_vec.load(c)
        c.enable_load_extension(False)
        return c

    @contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        """This thread's connection. The outermost `with` commits on success and rolls back
        on an error, so a nested call (one DB method used inside another) joins the same
        transaction instead of committing half of it."""
        local = self._local
        c = getattr(local, "conn", None)
        if c is None:
            c = local.conn = self._open()
            local.depth = 0
        local.depth += 1
        try:
            yield c
        except BaseException:
            if local.depth == 1 and c.in_transaction:
                c.rollback()
            raise
        else:
            if local.depth == 1:
                c.commit()
        finally:
            local.depth -= 1

    def close(self) -> None:
        """Close this thread's connection (tests; threads that end close theirs when collected)."""
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None

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
                       "interval_hours": (lambda v: 24.0 if v in (None, "") else max(0.0, float(v)))(src.get("interval_hours")),
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
    @staticmethod
    def _vec_sql(c: sqlite3.Connection) -> str:
        r = c.execute("SELECT sql FROM sqlite_master WHERE name='chunks_vec'").fetchone()
        return r[0] if r else ""

    def _vec_dim(self, c: sqlite3.Connection) -> int | None:
        m = re.search(r"float\[(\d+)\]", self._vec_sql(c))
        self._dim = int(m.group(1)) if m else 0
        return self._dim or None

    def vec_dim(self) -> int | None:
        if self._dim is None:
            with self.conn() as c:
                return self._vec_dim(c)
        return self._dim or None

    @staticmethod
    def _create_vec(c: sqlite3.Connection, dim: int, name: str = "chunks_vec") -> None:
        # The source is a partition key: a search of one source scans only that source's
        # vectors, with the same exact (brute-force) KNN as the whole table.
        c.execute(f"CREATE VIRTUAL TABLE {name} USING vec0({VEC_PARTITION}, embedding float[{dim}] distance_metric=cosine)")

    def _migrate_vec_partition(self) -> None:
        """Older indexes have no source partition key. Copy the vectors into a new table with
        one (no re-embedding: the stored vectors are reused), in one transaction."""
        with self._write_lock, self.conn() as c:
            sql = self._vec_sql(c)
            dim = self._vec_dim(c)
            if not sql or "partition key" in sql or not dim:
                return
            t0 = time.time()
            c.execute("DROP TABLE IF EXISTS temp.vec_copy")
            c.execute("""CREATE TEMP TABLE vec_copy AS
                         SELECT v.rowid AS id, k.source AS source, v.embedding AS embedding
                         FROM chunks_vec v JOIN chunks k ON k.id = v.rowid""")
            c.execute("DROP TABLE chunks_vec")
            self._create_vec(c, dim)
            c.execute("INSERT INTO chunks_vec(rowid, source, embedding) SELECT id, source, embedding FROM temp.vec_copy ORDER BY id")
            n = c.execute("SELECT COUNT(*) FROM temp.vec_copy").fetchone()[0]
            c.execute("DROP TABLE temp.vec_copy")
            logging.getLogger("ask.db").info("vector index: added the source partition key (%d vectors copied in %.1f s)",
                                             n, time.time() - t0)

    def _check_generation(self, generation: int | None) -> None:
        if generation is not None and generation != self.generation:
            raise IndexReset("the index was reset (embedding model changed) during this sync")

    def _ensure_vec(self, c: sqlite3.Connection, dim: int) -> None:
        """Call with the write lock held: check and change the schema in one step, so two
        syncs can't both decide to create the table."""
        current = self._vec_dim(c)
        if current == dim:
            return
        if current is not None:
            c.execute("DROP TABLE chunks_vec")
            c.execute("UPDATE documents SET hash='' ")  # force re-embedding on next sync
        self._create_vec(c, dim)
        self._dim = dim

    def ensure_vec(self, dim: int, generation: int | None = None) -> None:
        """Create the vector table for this embedding size. A different size means a
        different embedding model: drop the vectors so everything is re-embedded."""
        with self._write_lock, self.conn() as c:
            self._check_generation(generation)
            self._ensure_vec(c, dim)

    def reset_index(self) -> None:
        """Drop every document, chunk and vector (used when the embedding model changes).
        Syncs that are still embedding with the old model stop at their next write."""
        with self._write_lock, self.conn() as c:
            self.generation += 1
            c.execute("DELETE FROM chunks_fts")
            c.execute("DROP TABLE IF EXISTS chunks_vec")
            self._dim = 0
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
                      vectors: list[list[float]], generation: int | None = None) -> None:
        self.save_documents(source, [(doc, chunks, vectors)], generation)

    def save_documents(self, source: str, items: list[tuple[dict[str, Any], list[dict[str, Any]], list[list[float]]]],
                       generation: int | None = None) -> None:
        """Save several embedded documents in ONE transaction (the indexer batches a few dozen,
        so a sync commits far less often). Chunks go in with executemany.

        generation: the value of self.generation when the sync started; a reset since then
        raises IndexReset instead of mixing old-model vectors into the new index."""
        if not items:
            return
        with self._write_lock, self.conn() as c:
            self._check_generation(generation)
            for doc, chunks, vectors in items:
                if vectors:
                    self._ensure_vec(c, len(vectors[0]))
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
                pairs = list(zip(chunks, vectors))
                if not pairs:
                    continue
                # Chunk ids are given, so the three tables can be filled with executemany.
                first = c.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM chunks").fetchone()[0]
                ids = range(first, first + len(pairs))
                title = doc.get("title") or doc["id"]
                c.executemany("INSERT INTO chunks(id, document, source, ord, heading, text) VALUES(?,?,?,?,?,?)",
                              [(k, doc_row, source, ch["ord"], ch["heading"], ch["text"]) for k, (ch, _) in zip(ids, pairs)])
                c.executemany("INSERT INTO chunks_fts(rowid, title, heading, text) VALUES(?,?,?,?)",
                              [(k, title, ch["heading"], ch["text"]) for k, (ch, _) in zip(ids, pairs)])
                c.executemany("INSERT INTO chunks_vec(rowid, source, embedding) VALUES(?, ?, ?)",
                              [(k, source, pack(vec)) for k, (_, vec) in zip(ids, pairs)])

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
        """The k nearest chunks (cosine distance). With sources, each source is searched in its
        own partition (exact KNN over only that source's vectors) and the results merged, which
        is the true top k over those sources. `source IN (...)` is not used: sqlite-vec returns
        partitions one after another, not one merged ranking."""
        if self.vec_dim() != len(vec):
            return []
        blob = pack(vec)
        with self.conn() as c:
            if not sources:
                rows = c.execute("SELECT rowid, distance FROM chunks_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                                 (blob, k)).fetchall()
            else:
                rows = []
                for src in dict.fromkeys(sources):
                    rows += c.execute("SELECT rowid, distance FROM chunks_vec WHERE embedding MATCH ? AND k = ? AND source = ?",
                                      (blob, k, src)).fetchall()
                rows.sort(key=lambda r: r[1])
        return [(r[0], r[1]) for r in rows[:k]]

    def vector_distances(self, vec: list[float], ids: list[int]) -> dict[int, float]:
        """Cosine distance from vec for these chunks (same measure as vector_search)."""
        if not ids or self.vec_dim() != len(vec):
            return {}
        marks = ",".join("?" * len(ids))
        with self.conn() as c:
            rows = c.execute(f"SELECT rowid, vec_distance_cosine(embedding, ?) FROM chunks_vec WHERE rowid IN ({marks})",
                             (pack(vec), *ids)).fetchall()
        return {r[0]: r[1] for r in rows}

    def term_hits(self, terms: list[str], ids: list[int]) -> dict[int, int]:
        """How many of these search words each chunk holds (same FTS tokenizer and stemming)."""
        out = {cid: 0 for cid in ids}
        if not ids or not terms:
            return out
        marks = ",".join("?" * len(ids))
        with self.conn() as c:
            for term in terms:
                try:
                    rows = c.execute(f"SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? AND rowid IN ({marks})",
                                     ('"' + term.replace('"', '') + '"', *ids)).fetchall()
                except sqlite3.OperationalError:
                    continue
                for r in rows:
                    out[r[0]] += 1
        return out

    def keyword_search(self, match: str, k: int, sources: list[str] | None) -> list[tuple[int, float]]:
        if not match:
            return []
        # The source filter is part of the query, so the limit counts only the picked sources.
        where, args = "chunks_fts MATCH ?", [match]
        if sources:
            where += f" AND k.source IN ({','.join('?' * len(sources))})"
            args += list(sources)
        with self.conn() as c:
            try:
                rows = c.execute(f"""SELECT f.rowid, bm25(chunks_fts, 4.0, 2.0, 1.0) AS score
                                     FROM chunks_fts f JOIN chunks k ON k.id=f.rowid
                                     WHERE {where} ORDER BY score LIMIT ?""", (*args, k)).fetchall()
            except sqlite3.OperationalError:
                return []
        return [(r[0], r[1]) for r in rows]

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


def _doc(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    for key, empty in (("meta", "{}"), ("links", "{}"), ("images", "[]")):
        d[key] = json.loads(d[key] or empty)
    return d
