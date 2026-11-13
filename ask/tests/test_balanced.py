"""Balanced retrieval tests (ask/app/rag.py retrieve + balanced_picks).

No Ollama needed: embeddings are a word hash (same words = close vectors).
Run from the ask/ folder:      python -m unittest discover -s tests -v
or inside the Ask container:   docker compose exec vco-ask python -m unittest discover -s tests -v
"""
from __future__ import annotations

import hashlib
import math
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import rag            # noqa: E402
from app.db import DB          # noqa: E402

DIM = 256


def embed(text: str) -> list[float]:
    v = [0.0] * DIM
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        h = int(hashlib.md5(w.encode()).hexdigest(), 16)
        v[h % DIM] += 1.0 if (h >> 8) % 2 else -1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


class FakeOllama:
    def __init__(self, url):
        pass

    def embed(self, model, texts, keep_alive=None):
        return [embed(t) for t in texts]


CPU = "What is a high VM CPU ready value on a host?"


class Balanced(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.tmp.name) / "ask.db")
        self.n = 0
        self._ollama = rag.Ollama
        rag.Ollama = FakeOllama

    def tearDown(self):
        rag.Ollama = self._ollama
        self.tmp.cleanup()

    def add(self, src: str, title: str, *sections: str) -> None:
        """One document; each section becomes one chunk."""
        self.n += 1
        chunks = [{"ord": i, "heading": f"{title} part {i}", "text": t} for i, t in enumerate(sections)]
        self.db.save_document(src, {"id": f"{src}-{self.n}", "title": title, "hash": str(self.n), "text": " ".join(sections)},
                              chunks, [embed(f"{title} {t}") for t in sections])

    def big_blog(self, posts: int = 60) -> None:
        for i in range(posts):
            self.add("bigblog", f"Blog post {i} on vm cpu ready",
                     "In this post I look at vm cpu ready time on a host. VM CPU ready is shown in esxtop and vCenter charts. " * 2)

    def strong_blog(self, posts: int = 60) -> None:
        """A big source whose posts are almost the question itself: it wins every shared slot."""
        text = "What is a high VM CPU ready value on a host? High VM CPU ready value on a host. " * 2
        for i in range(posts):
            self.add("bigblog", f"Blog post {i} high vm cpu ready value host", text)

    def ask(self, sources, per_source=2, top_k=8, context=60000, q=CPU):
        cfg = {"embed_model": "fake-embed", "ollama_url": "", "keep_alive": "1m", "top_k": top_k,
               "context_chars": context, "expand_links": False, "source_min_chunks": per_source}
        info: dict = {}
        items = rag.retrieve(self.db, cfg, q, sources, info)
        return items, info.get("retrieval")

    # -- 1. one dominant source ---------------------------------------------------------
    def test_dominant_source_leaves_a_slot_for_the_small_one(self):
        self.big_blog()
        self.add("smallpdf", "CPU Ready thresholds",
                 "VM CPU ready above 5 percent per vCPU on a host means contention. Check CPU ready in vCenter.")
        self.add("smallpdf", "Install guide", "Deploy the appliance OVA and configure networking.")

        items, note = self.ask(["bigblog", "smallpdf"], per_source=0)
        self.assertNotIn("smallpdf", {i["source"] for i in items}, "the test needs the big source to win every slot")

        items, note = self.ask(["bigblog", "smallpdf"], per_source=2)
        small = [i for i in items if i["source"] == "smallpdf"]
        self.assertEqual([i["title"] for i in small], ["CPU Ready thresholds"])   # the install guide doesn't match
        self.assertTrue(small[0].get("reserved"))
        self.assertEqual(note["reserved"], 1)
        self.assertEqual(note["sources"], {"smallpdf": 1})
        # The big source already had its share, so it was not searched a second time.
        self.assertEqual(note["searched"], ["smallpdf"])
        self.assertEqual(len(items), 8)

    # -- 2. keyword-only false hit ------------------------------------------------------
    def test_one_shared_word_is_not_a_match(self):
        self.big_blog()
        self.add("party", "Dinner party tips",
                 "A good host plans the menu early, sets the table and greets guests. Keep the value of a relaxed evening in mind.")
        self.add("cooking", "Pasta recipe", "Boil water, add salt and pasta, cook for nine minutes.")
        # The party page is a keyword hit (it shares "host" and "value")...
        self.assertTrue(self.db.keyword_search(rag.fts_query(CPU), 5, ["party"]))
        items, note = self.ask(["bigblog", "party", "cooking"], per_source=2)
        # ...but not a real match, so neither it nor the cooking page gets a reserved slot.
        self.assertFalse([i for i in items if i.get("reserved")])
        self.assertEqual(note["reserved"], 0)
        self.assertCountEqual(note["searched"], ["party", "cooking"])

    # -- 3. more sources than top_k -----------------------------------------------------
    def test_more_sources_than_slots(self):
        self.big_blog()
        names = [f"doc{i}" for i in range(5)]
        for i, name in enumerate(names):
            self.add(name, f"CPU ready note {i}",
                     f"VM CPU ready on a host: a high value means vCPU contention. Note {i}.")
        items, note = self.ask(["bigblog"] + names, per_source=2, top_k=3)
        self.assertEqual(len(items), 3)
        # 6 short sources, 3 slots: each gets at most one, never more than top_k in all.
        self.assertTrue(all(i.get("balanced") for i in items))
        self.assertEqual(len({i["source"] for i in items}), 3)
        self.assertLessEqual(note["reserved"], 3)
        self.assertTrue(all(n == 1 for n in note["sources"].values()), note)

    # -- 4. two sections per document, refill from the same source ----------------------
    def test_per_document_cap_refills_from_the_same_source(self):
        self.strong_blog()
        same = "On this host the VM CPU ready value was high during contention, see the esxtop chart"
        self.add("guide", "CPU ready deep dive", same + " A", same + " B", same + " C", same + " D")
        self.add("guide", "CPU ready quick check", same + " quick")
        items, note = self.ask(["bigblog", "guide"], per_source=0, top_k=8)
        self.assertNotIn("guide", {i["source"] for i in items}, "the test needs the big source to win every slot")

        items, note = self.ask(["bigblog", "guide"], per_source=3, top_k=8)
        titles = [i["title"] for i in items if i["source"] == "guide"]
        # The deep dive has the 4 best guide sections, but only 2 may come from one document;
        # the third reserved slot goes to the guide's other document, not back to the blog.
        self.assertEqual(titles.count("CPU ready deep dive"), 2)
        self.assertEqual(titles.count("CPU ready quick check"), 1)
        self.assertEqual(note["sources"], {"guide": 3})
        self.assertEqual(len(items), 8)

    def test_budget_refill_keeps_the_slot_with_the_source(self):
        self.strong_blog()
        # The long section is the guide's best match, but far too big for the budget.
        self.add("guide", "CPU ready long", "High VM CPU ready value on this host during contention. " * 300)
        self.add("guide", "CPU ready short", "On this host the VM CPU ready value was high during contention, "
                                             "see the esxtop chart and the scheduler queue.")
        items, note = self.ask(["bigblog", "guide"], per_source=0, top_k=4, context=3000)
        self.assertNotIn("guide", {i["source"] for i in items}, "the test needs the big source to win every slot")

        items, note = self.ask(["bigblog", "guide"], per_source=1, top_k=4, context=3000)
        guide = [i["title"] for i in items if i["source"] == "guide"]
        self.assertEqual(guide, ["CPU ready short"])
        self.assertEqual(note["sources"], {"guide": 1})

    def test_single_source_is_not_balanced(self):
        self.big_blog()
        items, note = self.ask(["bigblog"], per_source=2)
        self.assertIsNone(note)
        self.assertFalse([i for i in items if i.get("balanced")])


if __name__ == "__main__":
    unittest.main()
