"""Answer quality test (ask/app/evalrun.py): which saved prompts are replayed, the judge's
verdict mapping (answers are shown in random order), and the summary.

Run from the ask/ folder:      python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import evalrun        # noqa: E402
from app.db import DB          # noqa: E402


class FakeJudge:
    """Prefers whichever answer says "GOOD"."""
    def chat(self, model, messages, **kw):
        text = messages[1]["content"]
        a1 = text.split("Answer 1:", 1)[1].split("=====", 1)[0]
        better = 1 if "GOOD" in a1 else 2
        return "Sure: " + json.dumps({"better": better, "score1": 5 if better == 1 else 2,
                                       "score2": 5 if better == 2 else 2, "reason": "r"})


CFG = {"chat_model": "m", "num_ctx": 4096, "keep_alive": "1m"}


class Eval(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.tmp.name) / "ask.db")
        self.runner = evalrun.EvalRunner(self.db, lambda: CFG, lambda scope: None)

    def tearDown(self):
        self.tmp.cleanup()

    def chat(self, q, answer, scope="all", stats=None, cards=None):
        cid = self.db.new_chat(q, scope)
        self.db.add_message(cid, "user", q)
        self.db.add_message(cid, "assistant", answer, stats=stats or {"seconds": 2},
                            sources=cards or [{"kind": "hit", "source": "s", "doc_id": "d1"}])
        return cid

    def test_candidates_skip_tools_data_errors_and_duplicates(self):
        self.chat("What is CPU ready?", "An answer")
        self.chat("what is  CPU ready?", "Same question again")             # duplicate (newer one is kept)
        self.chat("Run the tool", "done", scope="mcp:pwsh")
        self.chat("Search the web", "done", scope="web:brave")
        self.chat("Fix my grammar", "done", scope="none")
        self.chat("How many VMs?", "42", cards=[{"kind": "data"}])
        self.chat("Summarize the blog", "...", stats={"overview": {"posts": 3}})
        self.chat("Broken", "_Ollama is down_", stats={"error": "down"})
        self.chat("What is DRS?", "Another answer")
        got = [c["question"] for c in self.runner.candidates(10)]
        self.assertEqual(got, ["What is DRS?", "what is  CPU ready?"])
        self.assertEqual(len(self.runner.candidates(1)), 1)

    def test_judge_maps_back_to_old_and_new_in_both_orders(self):
        for flip in (0.1, 0.9):          # random() < 0.5 puts the new answer first
            with mock.patch.object(evalrun.random, "random", return_value=flip):
                v = evalrun.judge_answers(FakeJudge(), CFG, "q", [], "old answer", "GOOD new answer")
                self.assertEqual(v["better"], "new")
                self.assertEqual((v["new_score"], v["old_score"]), (5, 2))
                v = evalrun.judge_answers(FakeJudge(), CFG, "q", [], "GOOD old answer", "new answer")
                self.assertEqual(v["better"], "old")
                self.assertEqual((v["new_score"], v["old_score"]), (2, 5))

    def test_judge_without_json_is_an_error_not_a_verdict(self):
        class Chatty:
            def chat(self, *a, **k):
                return "I think the second one."
        v = evalrun.judge_answers(Chatty(), CFG, "q", [], "a", "b")
        self.assertIn("error", v)
        self.assertNotIn("better", v)

    def test_start_records_answer_and_judge_models(self):
        self.chat("What is CPU ready?", "An answer")
        seen = {}

        def fake_replay(item, model, judge_model):
            seen.update(model=model, judge_model=judge_model)
        self.runner._replay = fake_replay
        rid = self.runner.start(5, judge=True, model="gemma3:27b", judge_model="big:70b")
        self.runner._thread.join(5)
        run = self.runner.run(rid)
        self.assertEqual((run["settings"]["chat_model"], run["settings"]["judge_model"]), ("gemma3:27b", "big:70b"))
        self.assertEqual(seen, {"model": "gemma3:27b", "judge_model": "big:70b"})
        rid = self.runner.start(5, judge=True)                 # defaults: saved chat model judges itself
        self.runner._thread.join(5)
        self.assertEqual(self.runner.run(rid)["settings"]["judge_model"], "m")
        rid = self.runner.start(5, judge=False, model="x")    # no judge
        self.runner._thread.join(5)
        run = self.runner.run(rid, vs=1)
        self.assertEqual(run["settings"]["judge_model"], "")
        self.assertEqual(run["previous"]["id"], 1)
        self.assertIn("chat_model", run["changed"])

    def test_summary(self):
        items = [
            {"status": "done", "old_stats": {"seconds": 10, "tok_per_s": 20}, "new_stats": {"seconds": 6, "tok_per_s": 30,
             "retrieval": {"reserved": 2}}, "old_docs": ["s/a", "s/b"], "new_docs": ["s/a", "p/c"],
             "old_answer": "The sources do not contain that.", "new_answer": "It is 5 percent.",
             "verdict": {"better": "new", "old_score": 2, "new_score": 5}},
            {"status": "done", "old_stats": {"seconds": 4}, "new_stats": {"seconds": 4}, "old_docs": ["s/a"],
             "new_docs": ["s/a"], "old_answer": "x", "new_answer": "x", "verdict": {"better": 0, "old_score": 3, "new_score": 3}},
            {"status": "error", "old_stats": {}, "new_stats": {}, "old_docs": [], "new_docs": [], "old_answer": "",
             "new_answer": "", "verdict": {}},
        ]
        s = evalrun.summarize(items)
        self.assertEqual((s["done"], s["errors"]), (2, 1))
        self.assertEqual((s["old_seconds"], s["new_seconds"]), (7, 5))
        self.assertEqual((s["old_not_found"], s["new_not_found"]), (1, 0))
        self.assertEqual((s["new_better"], s["old_better"], s["same"]), (1, 0, 1))
        self.assertEqual(s["reserved"], 2)
        self.assertEqual(s["overlap"], round((1 / 3 + 1) / 2, 2))


if __name__ == "__main__":
    unittest.main()
