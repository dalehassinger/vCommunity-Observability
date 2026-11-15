"""Tool answers (rag.answer_stream / rag.answer): when the model keeps calling tools until it runs out
of tool turns, the last turn is told to answer, its stray tool calls are ignored, and rag.answer
returns that final text apart from the "Let me check ..." steps. Also the scheduled email for it.

Run from the ask/ folder:      python -m unittest discover -s tests -v
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import rag                    # noqa: E402
from app import schedules as sched     # noqa: E402
from app.db import DB                  # noqa: E402

CFG = {"chat_model": "m", "temperature": 0.2, "num_ctx": 8192, "think": False, "keep_alive": "5m",
       "mcp_max_rounds": 3, "agent_url": "http://agent", "activity_log": False, "top_k": 8,
       "system_prompt": "sys", "ollama_url": "http://o", "context_chars": 24000, "history_turns": 3}


class FakeOllama:
    """Calls a tool every turn it may; without tools it answers only when told to (else narrates)."""
    def __init__(self, url, stubborn=False):
        self.stubborn = stubborn
        self.turns = []

    def chat_stream(self, model, msgs, *, options, think, keep_alive, tools=None):
        FakeOllama.last = self
        self.turns.append(bool(tools))
        n = len(self.turns)
        if tools:
            yield {"message": {"content": f"Let me check step {n}."}}
            yield {"message": {"tool_calls": [{"function": {"name": "Get-Host", "arguments": {}}}]}}
        elif msgs[-1]["content"] == rag.FINAL_ROUND_NOTE and not self.stubborn:
            yield {"message": {"content": "| Host | CPU |\n|---|---|\n| esx01 | 25% |"}}
            yield {"message": {"tool_calls": [{"function": {"name": "Get-Host", "arguments": {}}}]}}  # ignored
        else:
            yield {"message": {"content": "Let me look at the metric keys."}}
        yield {"done": True, "eval_count": 10, "eval_duration": 1e9, "prompt_eval_count": 100}


def fake_run_tool(cfg, chat_id, call, card, index, interactive):
    card.update(tool="Get-Host", server="powershell-mcp", status="ok", result="[]")
    card["_for_model"] = f"[{card['n']}] []"
    yield rag.sse("tool", {k: v for k, v in card.items() if k != "_for_model"})


class ToolRounds(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.tmp.name) / "ask.db")
        self.patches = [
            mock.patch.object(rag.mcp, "servers", return_value={"servers": [{"name": "powershell-mcp"}], "error": ""}),
            mock.patch.object(rag.mcp, "load_policy", return_value={}),
            mock.patch.object(rag.mcp, "ollama_tools", return_value=(
                [{"type": "function", "function": {"name": "Get-Host"}}], {"Get-Host": ("powershell-mcp", {"name": "Get-Host"})})),
            mock.patch.object(rag, "run_tool", fake_run_tool),
            mock.patch.object(rag, "syslog_out", mock.MagicMock()),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def ask(self, stubborn=False):
        chat_id = self.db.new_chat("t", "mcp:powershell-mcp")
        with mock.patch.object(rag, "Ollama", lambda url: FakeOllama(url, stubborn)):
            return rag.answer(self.db, CFG, chat_id, "Host status?", [], ["powershell-mcp"]), chat_id

    def test_last_turn_is_told_to_answer_and_answer_is_only_that(self):
        out, chat_id = self.ask()
        self.assertEqual(FakeOllama.last.turns, [True, True, True, False])
        self.assertTrue(out["answer"].startswith("| Host | CPU |"), out["answer"])
        self.assertIn("Let me check step 1.", out["steps"])
        self.assertEqual(out["stats"]["tool_calls"], 3)                 # the 4th turn's call was not run
        self.assertEqual(out["stats"]["tool_rounds_used_up"], 3)
        saved = [m for m in self.db.messages(chat_id) if m["role"] == "assistant"][-1]["content"]
        self.assertIn("Let me check step 1.\n\nLet me check step 2.", saved)   # chat keeps the steps, apart
        self.assertTrue(saved.endswith("| esx01 | 25% |"))

    def test_model_that_never_answers_gets_steps_and_a_note_in_the_email(self):
        out, _ = self.ask(stubborn=True)
        self.assertTrue(out["stats"].get("no_final_answer") is None)        # it did write a last turn ...
        self.assertEqual(out["answer"], "Let me look at the metric keys.")  # ... just not a useful one
        # A run that ends on a tool call with nothing after it falls back to the steps, marked:
        res = {"chat_id": "c", "answer": "Let me check step 1.", "sources": [], "error": None,
               "stats": {"no_final_answer": True, "tool_rounds_used_up": 3}}
        s = {**sched.clean({"name": "Host Status", "prompt": "p", "frequency": "daily", "at_time": "07:00",
                            "timezone": "UTC", "recipients": "a@example.com"})[0], "id": 1}
        _, text, html = sched.build_email(s, res, public="", when=datetime(2026, 11, 14, tzinfo=ZoneInfo("UTC")),
                                          model="m", seconds=1)
        self.assertIn("used all its tool turns", text)
        self.assertIn("used all its tool turns", html)

    def test_tool_cards_are_named_in_the_email(self):
        cards = [{"kind": "tool", "n": 1, "tool": "Get-Host", "server": "powershell-mcp", "status": "ok"},
                 {"kind": "tool", "n": 2, "tool": "Get-Metric", "server": "powershell-mcp", "status": "error", "error": "no samples"}]
        lines = [x["title"] for x in sched.source_lines(cards, "")]
        self.assertEqual(lines, ["Tool Get-Host (powershell-mcp)", "Tool Get-Metric (powershell-mcp): error, no samples"])


if __name__ == "__main__":
    unittest.main()
