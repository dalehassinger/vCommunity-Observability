"""Scheduled prompts (ask/app/schedules.py): form checks, schedules, the email, runs
(answer saved as a chat, email sent, "only when changed", failures) and the notifier's errors.

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

from app import schedules as sched   # noqa: E402
from app.db import DB                 # noqa: E402

GOOD = {"name": "Weekly blog", "prompt": "What is new?", "scope": ["vcrocs"], "frequency": "weekly",
        "weekday": "mon", "at_time": "07:00", "timezone": "America/New_York", "recipients": "a@example.com; b@example.com"}
CFG = {"chat_model": "qwen", "public_url": "http://ask.local:8081"}


class FakeNotifier:
    def __init__(self, fail: str = ""):
        self.sent: list[dict] = []
        self.fail = fail

    def send(self, to, subject, text, html_body, attachments=None):
        if self.fail:
            raise RuntimeError(self.fail)
        self.sent.append({"to": to, "subject": subject, "text": text, "html": html_body, "attachments": attachments or []})
        return f"email sent to {', '.join(to)}"

    hook_fail = ""

    def webhook(self, title, text, *, name="", color="info", link=""):
        if self.hook_fail:
            raise RuntimeError(self.hook_fail)
        self.posted = getattr(self, "posted", []) + [{"title": title, "text": text, "name": name, "color": color, "link": link}]
        return "webhook answered 204"

    def webhooks(self):
        return ["daily"]


class FakeIndexer:
    def __init__(self):
        self.started = []

    def start(self, name, full=False):
        self.started.append(name)
        return True

    def is_running(self, name):
        return False


class FakeReports:
    def __init__(self, folder: Path):
        self.folder = folder

    def path(self, rid):
        p = self.folder / f"{rid}.html"
        return p if p.is_file() else None


class Checks(unittest.TestCase):
    def test_send_to_needs_one_and_email_needs_recipients(self):
        _, errors = sched.clean({**GOOD, "send_email": False, "send_webhook": False})
        self.assertIn("send_to", errors)
        _, errors = sched.clean({**GOOD, "recipients": ""})
        self.assertIn("recipients", errors)
        s, errors = sched.clean({**GOOD, "recipients": "", "send_email": "false", "send_webhook": "on"})
        self.assertEqual(errors, {})
        self.assertEqual((s["send_email"], s["send_webhook"]), (0, 1))

    def test_chat_message(self):
        s = {**GOOD, "id": 1, "include_prompt": 0, "include_sources": 0}
        when = datetime(2026, 11, 16, 7, 0, tzinfo=ZoneInfo("America/New_York"))
        title, text = sched.build_chat(s, {"chat_id": "c1", "answer": "Two posts [1].", "sources": [{"n": 1, "title": "A"}]},
                                       public="http://ask:8081", when=when, model="qwen", seconds=3)
        self.assertEqual(title, "[Ask] Weekly blog: 2026-11-16")
        self.assertIn("Two posts.", text)
        self.assertNotIn("What is new?", text)
        self.assertNotIn("Sources", text)
        _, text = sched.build_chat({**s, "include_prompt": 1}, {"chat_id": "", "error": "boom"}, public="", when=when,
                                   model="qwen", seconds=1)
        self.assertIn("> What is new?", text)
        self.assertIn("failed", text)
        _, text = sched.build_chat({**s, "include_prompt": 1, "prompt": "Line one\n\nLine two"},
                                   {"chat_id": "", "answer": "ok"}, public="", when=when, model="qwen", seconds=1)
        self.assertIn("> Line one\n>\n> Line two", text)

    def test_clean_accepts_a_good_schedule(self):
        s, errors = sched.clean(GOOD)
        self.assertEqual(errors, {})
        self.assertEqual(s["scope"], "vcrocs")
        self.assertEqual(s["recipients"], "a@example.com, b@example.com")
        self.assertEqual((s["attach_report"], s["enabled"], s["only_changed"]), (1, 1, 0))

    def test_clean_reports_each_bad_field(self):
        _, errors = sched.clean({"frequency": "yearly", "at_time": "25:00", "timezone": "Mars/Base",
                                 "recipients": "not-an-address", "monthday": "31", "every_hours": "0"})
        for k in ("name", "prompt", "frequency", "at_time", "timezone", "recipients", "monthday", "every_hours"):
            self.assertIn(k, errors)

    def test_triggers_and_descriptions(self):
        tz = ZoneInfo("America/New_York")
        base = {**sched.clean(GOOD)[0]}
        nxt = sched.trigger(base).get_next_fire_time(None, datetime(2026, 11, 14, 12, 0, tzinfo=tz))
        self.assertEqual((nxt.weekday(), nxt.hour, nxt.minute), (0, 7, 0))            # Monday 07:00
        self.assertEqual(sched.describe(base), "Every Monday at 07:00 America/New_York")
        wd = {**base, "frequency": "weekdays"}
        nxt = sched.trigger(wd).get_next_fire_time(None, datetime(2026, 11, 14, 12, 0, tzinfo=tz))  # a Saturday
        self.assertEqual(nxt.weekday(), 0)
        last = {**base, "frequency": "monthly", "monthday": "last"}
        nxt = sched.trigger(last).get_next_fire_time(None, datetime(2026, 11, 14, 12, 0, tzinfo=tz))
        self.assertEqual((nxt.month, nxt.day), (11, 30))
        hours = {**base, "frequency": "hours", "every_hours": 6}
        self.assertTrue(sched.next_run({**hours, "enabled": 1}))
        self.assertEqual(sched.next_run({**hours, "enabled": 0}), "")


class Email(unittest.TestCase):
    def test_answer_is_html_with_sources_and_links_and_no_raw_html(self):
        s = {**sched.clean(GOOD)[0], "id": 1}
        result = {"chat_id": "abc123", "answer": "## New posts\n\n| Post | Date |\n|---|---|\n| VCF 9 [1] | 2026-11-10 |\n\n<script>alert(1)</script>",
                  "sources": [{"n": 1, "title": "VCF 9", "heading": "", "url": "https://www.vcrocs.info/vcf9/"},
                              {"n": 2, "title": "Doc", "heading": "Intro", "url": "", "doc_row": 42}]}
        subject, text, html = sched.build_email(s, result, public="http://ask.local:8081",
                                                when=datetime(2026, 11, 16, 7, 0, tzinfo=ZoneInfo("America/New_York")),
                                                model="qwen", seconds=12.3)
        self.assertEqual(subject, "[Ask] Weekly blog: 2026-11-16")
        self.assertIn("<table", html)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("https://www.vcrocs.info/vcf9/", html)
        self.assertIn("http://ask.local:8081/doc/42", html)
        self.assertIn("http://ask.local:8081/chat/abc123", html)
        self.assertIn("Open in Ask: http://ask.local:8081/chat/abc123", text)

    def test_prompt_and_sources_can_be_left_out(self):
        s = {**sched.clean({**GOOD, "include_prompt": False, "include_sources": False})[0], "id": 1}
        self.assertEqual((s["include_prompt"], s["include_sources"]), (0, 0))
        result = {"chat_id": "abc", "answer": "New post [1] and [2, 3]. See [docs](https://x.com/a) here.",
                  "sources": [{"n": 1, "title": "Post", "url": "https://x.com/vcrocs/status/1"}]}
        _, text, html = sched.build_email(s, result, public="http://ask:8081",
                                          when=datetime(2026, 11, 16, tzinfo=ZoneInfo("UTC")), model="m", seconds=1)
        for out in (text, html):
            self.assertNotIn("What is new?", out)                      # the prompt
            self.assertNotIn("Sources", out)
            self.assertNotIn("[1]", out)
        self.assertIn("New post and. See [docs](https://x.com/a) here.", text)   # links stay
        self.assertIn("Open in Ask", html)
        # A failure still says what was asked.
        _, text, _ = sched.build_email(s, {"chat_id": "", "error": "boom"}, public="",
                                       when=datetime(2026, 11, 16, tzinfo=ZoneInfo("UTC")), model="m", seconds=1)
        self.assertIn("Prompt: What is new?", text)

    def test_failure_email(self):
        s = {**sched.clean(GOOD)[0], "id": 1}
        subject, text, html = sched.build_email(s, {"chat_id": "", "error": "Ollama is not reachable"}, public="",
                                                when=datetime(2026, 11, 16, tzinfo=ZoneInfo("UTC")), model="m", seconds=1)
        self.assertIn("failed", subject)
        self.assertIn("Ollama is not reachable", text)
        self.assertNotIn("Open in Ask", text)

    def test_hash_ignores_citations_case_and_spacing(self):
        self.assertEqual(sched.answer_hash("Two posts [1, 2].\n\nNew"), sched.answer_hash("two posts.  new"))
        self.assertNotEqual(sched.answer_hash("Two posts"), sched.answer_hash("Three posts"))


class Runs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.tmp.name) / "ask.db")
        self.notifier = FakeNotifier()
        self.indexer = FakeIndexer()
        (Path(self.tmp.name) / "r1.html").write_text("<html>report</html>")
        self.s = sched.Schedules(self.db, lambda: CFG, lambda scope: (["vcrocs"], []), self.indexer,
                                 FakeReports(Path(self.tmp.name)), None, self.notifier)
        self.answers = []

    def tearDown(self):
        self.tmp.cleanup()

    def fake_answer(self, db, cfg, chat_id, question, docs, tools):
        db.add_message(chat_id, "user", question)
        text = self.answers.pop(0) if self.answers else "Answer"
        if text.startswith("ERROR:"):
            return {"chat_id": chat_id, "answer": "", "sources": [], "stats": {}, "error": text[6:]}
        db.add_message(chat_id, "assistant", text)
        self.cfg_seen = cfg
        return {"chat_id": chat_id, "answer": text, "stats": {}, "error": None,
                "sources": [{"n": 1, "title": "Data", "url": "", "report": {"id": "r1", "title": "VM report"}}]}

    def test_run_saves_a_chat_and_emails_with_the_report(self):
        item = self.s.save({**GOOD, "model": "gemma", "sync_first": True})
        with mock.patch.object(sched.rag, "answer", self.fake_answer), mock.patch.object(sched.control, "is_on", return_value=True):
            out = self.s.run(item["id"], "manual")
        self.assertEqual(out["status"], "ok")
        self.assertEqual(self.cfg_seen["chat_model"], "gemma")
        self.assertEqual(self.indexer.started, ["vcrocs"])
        self.assertTrue(self.db.get_chat(out["chat_id"])["title"].startswith("Scheduled: Weekly blog"))
        mail = self.notifier.sent[0]
        self.assertEqual(mail["to"], ["a@example.com", "b@example.com"])
        self.assertEqual(mail["attachments"][0]["filename"], "VM report.html")
        runs = self.s.runs(item["id"])
        self.assertEqual((runs[0]["status"], runs[0]["trigger"]), ("ok", "manual"))
        self.assertEqual(self.s.list()[0]["last"]["status"], "ok")

    def test_only_changed_skips_the_same_answer(self):
        item = self.s.save({**GOOD, "only_changed": True})
        self.answers = ["Two new posts [1].", "two new posts.", "Three new posts."]
        with mock.patch.object(sched.rag, "answer", self.fake_answer), mock.patch.object(sched.control, "is_on", return_value=True):
            statuses = [self.s.run(item["id"])["status"] for _ in range(3)]
        self.assertEqual(statuses, ["ok", "unchanged", "ok"])
        self.assertEqual(len(self.notifier.sent), 2)

    def test_failed_answer_is_emailed_and_email_failure_is_recorded(self):
        item = self.s.save(GOOD)
        self.answers = ["ERROR:Ollama is not reachable", "fine"]
        with mock.patch.object(sched.rag, "answer", self.fake_answer), mock.patch.object(sched.control, "is_on", return_value=True):
            out = self.s.run(item["id"])
            self.assertEqual(out["status"], "error")
            self.assertIn("failed", self.notifier.sent[0]["subject"])
            self.notifier.fail = "The Logs module is switched off"
            out = self.s.run(item["id"])
        self.assertEqual(out["status"], "not_sent")
        self.assertIn("not sent: The Logs module", self.s.runs(item["id"])[0]["email"])

    def test_webhook_only_needs_no_recipients(self):
        item = self.s.save({**GOOD, "recipients": "", "send_email": False, "send_webhook": True, "webhook_name": "daily"})
        self.assertEqual((item["send_email"], item["send_webhook"], item["webhook_name"]), (0, 1, "daily"))
        with mock.patch.object(sched.rag, "answer", self.fake_answer), mock.patch.object(sched.control, "is_on", return_value=True):
            out = self.s.run(item["id"])
        self.assertEqual(out["status"], "ok")
        self.assertEqual(self.notifier.sent, [])
        post = self.notifier.posted[0]
        self.assertIn("**Weekly blog**", post["text"])
        self.assertIn("Open in Ask: <http://ask.local:8081/chat/", post["text"])
        self.assertEqual(out["email"], "webhook answered 204")
        self.assertEqual((post["name"], post["color"]), ("daily", "ok"))
        self.assertTrue(post["link"].startswith("http://ask.local:8081/chat/"))

    def test_email_and_webhook_and_a_failed_webhook(self):
        item = self.s.save({**GOOD, "send_webhook": True})
        self.notifier.hook_fail = "Webhook not sent: no webhook URL is set"
        with mock.patch.object(sched.rag, "answer", self.fake_answer), mock.patch.object(sched.control, "is_on", return_value=True):
            out = self.s.run(item["id"])
        self.assertEqual(out["status"], "not_sent")
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertIn("email sent to a@example.com", out["email"])
        self.assertIn("webhook not sent: Webhook not sent: no webhook URL", out["email"])

    def test_avoid_repeats_sends_earlier_answers(self):
        item = self.s.save({**GOOD, "avoid_repeats": True})
        self.assertEqual(item["avoid_repeats"], 1)
        seen = []
        def answer(db, cfg, chat_id, question, docs, tools):
            seen.append(question)
            return self.fake_answer(db, cfg, chat_id, question, docs, tools)
        self.answers = ['"Quote one" - A', "ERROR:boom", '"Quote two" - B', "x" * 2000]
        with mock.patch.object(sched.rag, "answer", answer), mock.patch.object(sched.control, "is_on", return_value=True):
            for _ in range(4):
                self.s.run(item["id"])
        self.assertEqual(seen[0], GOOD["prompt"])                      # first run: nothing earlier
        self.assertIn("Do NOT repeat", seen[1])
        self.assertIn('1. "Quote one" - A', seen[1])
        self.assertIn('1. "Quote two" - B', seen[3])                   # newest first, failed run left out
        self.assertIn('2. "Quote one" - A', seen[3])
        self.assertNotIn("boom", seen[3])
        prev = self.s.earlier_answers(item["id"])
        self.assertEqual(len(prev), 3)
        self.assertTrue(prev[0].endswith(" ...") and len(prev[0]) <= sched.AVOID_EACH_CHARS + 4)
        # Off: the prompt is sent as written.
        self.s.save({**GOOD, "avoid_repeats": False}, item["id"])
        with mock.patch.object(sched.rag, "answer", answer), mock.patch.object(sched.control, "is_on", return_value=True):
            self.s.run(item["id"])
        self.assertEqual(seen[-1], GOOD["prompt"])

    def test_using_source_finds_prompts_that_pick_it_by_name(self):
        a = self.s.save({**GOOD, "name": "A", "scope": "vcrocs,isvara"})
        self.s.save({**GOOD, "name": "B", "scope": "all"})
        self.s.save({**GOOD, "name": "C", "scope": "vcrocs-old"})
        self.assertEqual(self.s.using_source("vcrocs"), [{"id": a["id"], "name": "A"}])
        self.assertEqual(self.s.using_source("isvara"), [{"id": a["id"], "name": "A"}])
        self.assertEqual(self.s.using_source("other"), [])
        self.s.save({**GOOD, "name": "A", "scope": "other"}, a["id"])
        self.assertEqual(self.s.using_source("vcrocs"), [])

    def test_paused_ask_skips_scheduled_runs(self):
        item = self.s.save(GOOD)
        with mock.patch.object(sched.control, "is_on", return_value=False):
            self.assertIsNone(self.s.run(item["id"], "schedule"))
        self.assertEqual(self.s.runs(item["id"]), [])

    def test_save_errors_edit_enable_delete(self):
        with self.assertRaises(ValueError):
            self.s.save({"name": ""})
        item = self.s.save(GOOD)
        self.s.save({**GOOD, "name": "Renamed"}, item["id"])
        self.assertEqual(self.s.get(item["id"])["name"], "Renamed")
        self.assertEqual(self.s.set_enabled(item["id"], False)["next"], "")
        self.assertTrue(self.s.delete(item["id"]))
        self.assertIsNone(self.s.get(item["id"]))


class SourceSyncSchedule(unittest.TestCase):
    def test_interval_is_the_old_behaviour_and_timed_ones_are_checked(self):
        self.assertEqual(sched.clean_sync_schedule({"frequency": "interval"}), ({}, {}))
        self.assertEqual(sched.clean_sync_schedule(None), ({}, {}))
        s, errors = sched.clean_sync_schedule({"frequency": "weekly", "weekday": "sun", "at_time": "05:30", "timezone": "UTC"})
        self.assertEqual(errors, {})
        self.assertEqual((s["frequency"], s["weekday"], s["at_time"]), ("weekly", "sun", "05:30"))
        _, errors = sched.clean_sync_schedule({"frequency": "daily", "at_time": "9", "timezone": "Nowhere/City"})
        self.assertEqual(set(errors), {"at_time", "timezone"})
        self.assertIn("frequency", sched.clean_sync_schedule({"frequency": "yearly"})[1])

    def test_sync_when_text(self):
        self.assertEqual(sched.sync_when({"interval_hours": 24, "sync_schedule": {}}), "every 24 h")
        self.assertEqual(sched.sync_when({"interval_hours": 0.5}), "every 0.5 h")
        self.assertEqual(sched.sync_when({"interval_hours": 0}), "never (Sync now only)")
        s = sched.clean_sync_schedule({"frequency": "daily", "at_time": "06:00", "timezone": "UTC"})[0]
        text = sched.sync_when({"interval_hours": 24, "sync_schedule": s, "enabled": True})
        self.assertTrue(text.startswith("Daily at 06:00 UTC · next "), text)
        self.assertEqual(sched.sync_when({"interval_hours": 24, "sync_schedule": s, "enabled": False}), "Daily at 06:00 UTC")

    def test_db_keeps_the_schedule_when_an_update_does_not_send_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = DB(Path(tmp) / "ask.db")
            base = {"name": "x", "plugin": "x-posts", "options": {}, "interval_hours": 24}
            db.upsert_source({**base, "sync_schedule": {"frequency": "daily", "at_time": "06:00", "timezone": "UTC"}})
            db.upsert_source({**base, "title": "renamed"})                      # e.g. an older page or the API
            self.assertEqual(db.get_source("x")["sync_schedule"]["frequency"], "daily")
            db.upsert_source({**base, "sync_schedule": {}})                       # back to every N hours
            self.assertEqual(db.get_source("x")["sync_schedule"], {})


class PublicAddress(unittest.TestCase):
    def test_localhost_becomes_the_macs_ip_from_the_host_agent(self):
        cfg = {"public_url": "http://localhost:8081/", "agent_url": "http://host.docker.internal:9101"}
        with mock.patch.object(sched.perf, "host_view", return_value={"ok": True, "lan_ip": "192.168.1.50"}):
            self.assertEqual(sched.public_base(cfg), "http://192.168.1.50:8081")
        with mock.patch.object(sched.perf, "host_view", return_value={"ok": False, "error": "no agent"}):
            self.assertEqual(sched.public_base(cfg), "http://localhost:8081")
        with mock.patch.object(sched.perf, "host_view", return_value=None):      # agent turned off
            self.assertEqual(sched.public_base(cfg), "http://localhost:8081")

    def test_a_real_name_is_kept_and_the_agent_not_asked(self):
        with mock.patch.object(sched.perf, "host_view") as hv:
            self.assertEqual(sched.public_base({"public_url": "http://dales-mac.local:8081"}), "http://dales-mac.local:8081")
        hv.assert_not_called()


class NotifierErrors(unittest.TestCase):
    def test_clear_messages(self):
        m = sched.Notifier()
        for code, text in ((401, "refused the API token"), (404, "Logs module is switched off")):
            resp = mock.Mock(status_code=code)
            with mock.patch.object(sched.requests, "post", return_value=resp):
                with self.assertRaisesRegex(RuntimeError, text):
                    m.send(["a@example.com"], "s", "t", "<p>t</p>")
        with mock.patch.object(sched.requests, "post", side_effect=sched.requests.ConnectionError()):
            with self.assertRaisesRegex(RuntimeError, "not reachable"):
                m.send(["a@example.com"], "s", "t", "")


if __name__ == "__main__":
    unittest.main()
