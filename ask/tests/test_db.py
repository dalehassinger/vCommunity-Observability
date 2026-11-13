"""ask.db storage (ask/app/db.py): the source partition key on the vector table (and the
migration that adds it to an older index without re-embedding), per-source search matching
an exact scan, the remembered vector size, batched saves and per-thread connections.

Run from the ask/ folder:      python -m unittest discover -s tests -v
"""
from __future__ import annotations

import random
import sqlite3
import struct
import sys
import tempfile
import threading
import unittest
from pathlib import Path

import sqlite_vec

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import DB, pack   # noqa: E402

DIM = 32


def rvec(rng: random.Random) -> list[float]:
    v = [rng.gauss(0, 1) for _ in range(DIM)]
    n = sum(x * x for x in v) ** 0.5
    return [x / n for x in v]


def raw(path: Path) -> sqlite3.Connection:
    c = sqlite3.connect(path)
    c.enable_load_extension(True)
    sqlite_vec.load(c)
    return c


def vec_sql(path: Path) -> str:
    c = raw(path)
    try:
        return c.execute("SELECT sql FROM sqlite_master WHERE name='chunks_vec'").fetchone()[0]
    finally:
        c.close()


class Storage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "ask.db"
        self.rng = random.Random(7)

    def tearDown(self):
        import gc
        gc.collect()
        self.tmp.cleanup()

    def fill(self, db: DB, sizes: dict[str, int]) -> None:
        for src, n in sizes.items():
            items = []
            for d in range(n):
                items.append(({"id": f"{src}-{d}", "title": f"{src} {d}", "hash": "h", "text": "x"},
                              [{"ord": 0, "heading": "a", "text": f"alpha {src} {d}"},
                               {"ord": 1, "heading": "b", "text": f"beta {src} {d}"}],
                              [rvec(self.rng), rvec(self.rng)]))
            db.save_documents(src, items)

    def exact(self, q: list[float], k: int, sources: list[str]) -> list[int]:
        c = raw(self.path)
        marks = ",".join("?" * len(sources))
        rows = c.execute(f"""SELECT v.rowid FROM chunks_vec v WHERE v.rowid IN (SELECT id FROM chunks WHERE source IN ({marks}))
                             ORDER BY vec_distance_cosine(v.embedding, ?) LIMIT ?""", (*sources, pack(q), k)).fetchall()
        c.close()
        return [r[0] for r in rows]

    def test_new_index_has_partition_key_and_search_is_exact(self):
        db = DB(self.path)
        self.fill(db, {"big": 300, "small": 5, "tiny": 2})
        sql = vec_sql(self.path)
        self.assertIn("partition key", sql)
        q = rvec(self.rng)
        for sources in (["small"], ["tiny", "small"], ["big", "tiny"]):
            got = [cid for cid, _ in db.vector_search(q, 6, sources)]
            self.assertEqual(got, self.exact(q, 6, sources), sources)
        # Every chunk's partition is its source.
        c = raw(self.path)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM chunks_vec v JOIN chunks k ON k.id=v.rowid "
                                   "WHERE v.source != k.source").fetchone()[0], 0)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM chunks").fetchone()[0], 614)
        c.close()

    def test_old_index_is_migrated_without_re_embedding(self):
        db = DB(self.path)
        self.fill(db, {"a": 20, "b": 3})
        q = rvec(self.rng)
        before = db.vector_search(q, 5, None)
        db.close()
        # Rebuild the table the old way (no partition key), with the same vectors.
        c = raw(self.path)
        rows = c.execute("SELECT rowid, embedding FROM chunks_vec").fetchall()
        c.execute("DROP TABLE chunks_vec")
        c.execute(f"CREATE VIRTUAL TABLE chunks_vec USING vec0(embedding float[{DIM}] distance_metric=cosine)")
        c.executemany("INSERT INTO chunks_vec(rowid, embedding) VALUES(?, ?)", rows)
        c.commit()
        c.close()
        db2 = DB(self.path)
        sql = vec_sql(self.path)
        self.assertIn("partition key", sql)
        self.assertEqual(db2.vector_search(q, 5, None), before)
        self.assertEqual([cid for cid, _ in db2.vector_search(q, 4, ["b"])], self.exact(q, 4, ["b"]))
        doc_hashes = db2.doc_hashes("a")
        self.assertTrue(all(h == "h" for _, h in doc_hashes.values()), "migration must not force re-embedding")

    def test_vector_size_is_remembered_and_reset(self):
        db = DB(self.path)
        self.assertIsNone(db.vec_dim())
        self.fill(db, {"a": 1})
        self.assertEqual(db.vec_dim(), DIM)
        db.reset_index()
        self.assertIsNone(db.vec_dim())
        self.assertEqual(db.vector_search(rvec(self.rng), 3, None), [])

    def test_nested_calls_share_one_transaction(self):
        db = DB(self.path)
        with self.assertRaises(RuntimeError):
            with db.conn() as c:
                c.execute("INSERT INTO settings(key, value) VALUES('outer', '1')")
                db.set_setting("inner", 2)       # nested: must not commit the outer insert
                raise RuntimeError("boom")
        self.assertEqual(db.get_settings(), {})
        db.set_setting("ok", 3)
        self.assertEqual(db.get_settings(), {"ok": 3})

    def test_each_thread_has_its_own_connection(self):
        db = DB(self.path)
        seen = []

        def grab():
            with db.conn() as c:
                seen.append(id(c))
        with db.conn() as mine:
            pass
        t = threading.Thread(target=grab)
        t.start()
        t.join()
        with db.conn() as again:
            self.assertIs(again, mine)
        self.assertNotEqual(seen[0], id(mine))


if __name__ == "__main__":
    unittest.main()
