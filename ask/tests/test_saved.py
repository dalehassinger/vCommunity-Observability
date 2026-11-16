"""Saved prompts (ask/app/saved.py) and the Web fetch (Ollama) scope (web:fetch).

Run from the ask/ folder:      python -m unittest discover -s tests -v
"""
from __future__ import annotations

import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import rag, saved, websearch   # noqa: E402
from app.db import DB                   # noqa: E402


class Checks(unittest.TestCase):
    def test_name_defaults_to_the_prompt(self):
        s, errors = saved.clean({"prompt": "  What is new on   the blog?  ", "scope": ["vcrocs"]})
        self.assertEqual(errors, {})
        self.assertEqual((s["name"], s["scope"]), ("What is new on the blog?", "vcrocs"))

    def test_prompt_needed(self):
        self.assertIn("prompt", saved.clean({"name": "x"})[1])

    def test_pages_need_a_fetch_tool_and_full_urls(self):
        self.assertIn("urls", saved.clean({"prompt": "p", "scope": "all", "urls": "https://a.example/x"})[1])
        self.assertIn("urls", saved.clean({"prompt": "p", "scope": "web:fetch", "urls": "a.example/x"})[1])
        self.assertIn("urls", saved.clean({"prompt": "p", "scope": "web:fetch", "urls": [f"https://a.example/{i}" for i in range(11)]})[1])
        s, errors = saved.clean({"prompt": "p", "scope": "web:ollama,vcrocs", "urls": "https://a.example/x\nhttps://b.example/y"})
        self.assertEqual(errors, {})
        self.assertEqual(s["urls"], "https://a.example/x\nhttps://b.example/y")

    def test_tags(self):
        s, errors = saved.clean({"prompt": "p", "tags": "Sports, #news  sports weather"})
        self.assertEqual((errors, s["tags"]), ({}, "sports,news,weather"))
        self.assertIn("tags", saved.clean({"prompt": "p", "tags": [f"t{i}" for i in range(11)]})[1])

    def test_question_lists_the_pages(self):
        self.assertEqual(saved.question({"prompt": "p", "urls": ""}), "p")
        self.assertEqual(saved.question({"prompt": "p", "urls": "https://a.example/x"}),
                         "p\n\nRead these pages with web_fetch:\n- https://a.example/x")


class Store(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = saved.Saved(DB(Path(self.tmp.name) / "ask.db"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_save_edit_use_delete(self):
        a = self.s.save({"name": "B", "prompt": "p", "scope": "web:fetch", "urls": "https://a.example/x", "model": "gemma"})
        self.s.save({"name": "A", "prompt": "q", "tags": "news, sports"})
        self.assertEqual(self.s.all_tags(), ["news", "sports"])
        self.assertEqual(self.s.list()[0]["tag_list"], ["news", "sports"])
        self.assertEqual([i["name"] for i in self.s.list()], ["A", "B"])
        self.assertTrue(self.s.get(a["id"])["question"].endswith("- https://a.example/x"))
        self.s.save({"name": "B2", "prompt": "p2", "scope": "all"}, a["id"])
        self.assertEqual((self.s.get(a["id"])["name"], self.s.get(a["id"])["urls"]), ("B2", ""))
        self.s.used(a["id"])
        self.s.used(a["id"])
        self.assertEqual(self.s.get(a["id"])["uses"], 2)
        with self.assertRaises(ValueError):
            self.s.save({"name": "x"})
        with self.assertRaises(KeyError):
            self.s.save({"prompt": "x"}, 999)
        self.assertEqual(self.s.delete_many([a["id"], 999]), 1)
        self.assertEqual(len(self.s.list()), 1)


class FetchGuard(unittest.TestCase):
    """Web fetch (Ollama) alone fetches only the pages named in the question, and a page asked
    for twice in one answer is fetched once."""

    def run_tool(self, url, fetch, n):
        calls = []
        def fake(cfg, tool, args):
            calls.append(args["url"])
            return {"ok": True, "text": "Page text", "links": [{"title": "t", "url": args["url"]}], "error": "", "ms": 5}
        card = {"n": n}
        with unittest.mock.patch.object(websearch, "call", fake):
            list(rag.run_web_tool({}, card, "web_fetch", {"url": url}, None, fetch))
        return card, calls

    def test_only_named_pages_and_no_repeats(self):
        q = "Weather?\n\nRead these pages with web_fetch:\n- https://www.AccuWeather.com/en/us/dillsburg/17019/10-day/2216137/"
        fetch = rag.fetch_state(q, [websearch.FETCH_SCOPE])
        card, calls = self.run_tool("https://www.accuweather.com/en/us/dillsburg/17019/10-day/2216137", fetch, 1)
        self.assertEqual((card["status"], len(calls)), ("ok", 1))
        card, calls = self.run_tool("https://www.accuweather.com/en/us/dillsburg/17019/10-day/2216137?page=2", fetch, 2)
        self.assertEqual((card["status"], calls), ("refused", []))
        self.assertIn("only the pages named", card["_for_model"])
        card, calls = self.run_tool("https://www.accuweather.com/en/us/dillsburg/17019/10-day/2216137/#top", fetch, 3)
        self.assertEqual((card["status"], calls), ("ok", []))
        self.assertIn("[1]", card["note"])

    def test_web_search_or_no_named_pages_are_not_limited(self):
        self.assertIsNone(rag.fetch_state("https://a.example/x", [websearch.FETCH_SCOPE, websearch.SCOPE])["only"])
        self.assertIsNone(rag.fetch_state("weather in Dillsburg", [websearch.FETCH_SCOPE])["only"])


class WebFetchScope(unittest.TestCase):
    def test_fetch_scope_offers_only_web_fetch(self):
        tools, index = websearch.ollama_tools(websearch.FETCH_SCOPE)
        self.assertEqual([t["function"]["name"] for t in tools], ["web_fetch"])
        self.assertEqual(list(index), ["web_fetch"])
        self.assertEqual(websearch.label(websearch.FETCH_SCOPE), "Ollama web fetch")
        self.assertEqual(websearch.status_of({"web_search": False}, websearch.FETCH_SCOPE)["available"], False)

    def test_fetch_note_and_no_search_order(self):
        cfg = {"system_prompt": "S", "history_turns": 0, "web_order": "ollama_first"}
        msgs = rag.tool_messages(cfg, [], "q", [], [websearch.FETCH_SCOPE, websearch.BRAVE_SCOPE])
        self.assertIn("you cannot search the web", msgs[0]["content"])
        self.assertIsNone(rag.web_order(cfg, [websearch.FETCH_SCOPE, websearch.BRAVE_SCOPE]))
        msgs = rag.tool_messages(cfg, [], "q", [], [websearch.FETCH_SCOPE, websearch.SCOPE])
        self.assertNotIn("you cannot search the web", msgs[0]["content"])


if __name__ == "__main__":
    unittest.main()
