"""x-posts source plug-in (sources/x-posts/fetch_x.py) against the stand-in X API
(scripts/fake_x.py): accounts, one combined query, since_id on later syncs, keep_days,
token and rate-limit errors. No network, no Ollama.

Run from the ask/ folder:      python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(HERE / "sources" / "x-posts"))
sys.path.insert(0, str(HERE))

import fetch_x  # noqa: E402

try:                # the stand-in lives in ask/scripts, which the image does not ship
    import fake_x   # noqa: E402
except ImportError:
    fake_x = None

PORT = 11598


@unittest.skipIf(fake_x is None, "ask/scripts/fake_x.py is not here (inside the image); run from the repo")
class XPosts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = fake_x.serve(PORT)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        fake_x.EXTRA.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def sync(self, state=None, token="good-token", **opts):
        o = {"accounts": ["@bpatvmware", "https://x.com/vcrocs"], "api_url": f"http://127.0.0.1:{PORT}", **opts}
        env = {**os.environ, "ASK_SOURCE_OPTIONS": json.dumps(o), "ASK_SOURCE_WORKDIR": self.tmp.name,
               "ASK_SOURCE_STATE": json.dumps(state or {}), "X_BEARER_TOKEN": token}
        p = subprocess.run([sys.executable, str(HERE / "sources" / "x-posts" / "fetch_x.py")], env=env,
                           capture_output=True, text=True, timeout=30)
        return p.returncode, (json.loads(p.stdout) if p.returncode == 0 else None), p.stderr

    def test_first_and_later_syncs(self):
        code, out, err = self.sync()
        self.assertEqual(code, 0, err)
        docs = out["documents"]
        self.assertEqual(len(docs), 6)                                   # 3 each, lamw not listed
        self.assertIn("in 1 request", err)                               # one combined query
        d = docs[0]
        self.assertTrue(d["url"].startswith("https://x.com/") and "/status/" in d["url"])
        self.assertIn("Published: ", d["text"])
        self.assertIn("Likes ", d["text"])
        self.assertEqual(d["meta"]["author"][0], "@")
        # A new post: the next sync asks only for newer ones and keeps the older ones.
        fake_x.EXTRA.append(fake_x.make("vcrocs", 9, hours_ago=0.1))
        code, out2, err = self.sync(state=out["state"])
        self.assertEqual(code, 0, err)
        self.assertEqual(len(out2["documents"]), 7)
        self.assertIn("1 new, 1 read", err)
        self.assertIn("Post 9 from vcrocs", out2["documents"][0]["text"])

    def test_keep_days_and_removed_accounts_are_dropped(self):
        code, out, _ = self.sync()
        fake_x.EXTRA.append(fake_x.make("vcrocs", 8, hours_ago=0.2))
        code, out, err = self.sync(state=out["state"], accounts=["vcrocs"], keep_days=1)
        self.assertEqual(code, 0, err)
        self.assertTrue(all(d["meta"]["author"] == "@vcrocs" for d in out["documents"]))
        # vcrocs' stand-in posts are 15, 25 and 35 hours old, plus the new one: two are under a day old.
        self.assertEqual(len(out["documents"]), 2)

    def test_errors_are_clear(self):
        code, _, err = self.sync(token="")
        self.assertNotEqual(code, 0)
        self.assertIn("X_BEARER_TOKEN=", err)
        code, _, err = self.sync(token="wrong")
        self.assertIn("refused the Bearer token", err)
        code, _, err = self.sync(accounts=["not a handle!"])
        self.assertIn("is not an X account name", err)
        code, out, err = self.sync(accounts=["ratelimited"])
        self.assertEqual(code, 0, err)                                    # nothing lost, just nothing new
        self.assertIn("rate limit", out["version"])

    def test_whole_source_catalog_has_the_complete_post_text(self):
        from app import overview
        from app.db import DB
        code, out, err = self.sync()
        self.assertEqual(code, 0, err)
        long_post = fake_x.make("vcrocs", 7, hours_ago=0.05)
        long_post["text"] = "Line one of a long post.\n\nSecond paragraph " + "word " * 80 + "END."
        fake_x.EXTRA.append(long_post)
        code, out, err = self.sync(state=out["state"])
        db = DB(Path(self.tmp.name) / "ask.db")
        db.upsert_source({"name": "xp", "plugin": "x-posts", "options": {}, "interval_hours": 24})
        with db.conn() as c:
            for d in out["documents"]:
                c.execute("INSERT INTO documents(source, doc_id, title, path, url, meta, text, hash, updated) VALUES(?,?,?,?,?,?,?,?,0)",
                          ("xp", d["id"], d["title"], d["path"], d["url"], json.dumps(d["meta"]), d["text"], d["hash"]))
        ov = overview.build(db, {"context_chars": 24000, "num_ctx": 16384}, ["xp"])
        text = overview.block(ov["catalogs"][0], 1)
        self.assertIn("- Now: ", text)
        self.assertIn("COMPLETE post text", text)
        first = [ln for ln in text.splitlines() if ln.startswith("- 20")][0]
        self.assertIn("UTC | @vcrocs | Line one of a long post. / Second paragraph", first)
        self.assertIn("END. | https://x.com/vcrocs/status/", first)
        self.assertNotIn("…", first)
        self.assertIn("word for word", overview.instructions(ov))
        db.close() if hasattr(db, "close") else None

    def test_queries_fit_the_limit(self):
        accounts = [f"user{i:010d}" for i in range(60)]
        qs = fetch_x.queries(accounts, " -is:retweet -is:reply")
        self.assertGreater(len(qs), 1)
        self.assertTrue(all(len(q) <= fetch_x.QUERY_MAX for q in qs))
        self.assertEqual(sum(q.count("from:") for q in qs), 60)
        self.assertEqual(fetch_x.queries(["a"], " -is:retweet"), ["from:a -is:retweet"])


if __name__ == "__main__":
    unittest.main()
