"""Logs module webhooks (modules/logs/notify.py): Discord splitting and the payload per service.

Run from the repository folder:   python -m unittest discover -s tests -v
(scripts/smoke_test.py runs these too.) No network: urlopen is replaced by a fake.
"""
from __future__ import annotations

import io
import json
import sys
import unittest
import unittest.mock
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "modules" / "logs"))

import notify  # noqa: E402

DISCORD = "https://discord.com/api/webhooks/123/abc"


class FakeResponse:
    status = 204

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def capture():
    """A fake urlopen that keeps each JSON body it is sent."""
    sent = []

    def urlopen(req, timeout=0):
        sent.append(json.loads(req.data))
        return FakeResponse()
    return sent, urlopen


class SplitText(unittest.TestCase):
    def test_short_text_is_one_piece(self):
        self.assertEqual(notify.split_text("hello", 2000, 5), ["hello"])
        self.assertEqual(notify.split_text("", 2000, 5), [" "])

    def test_cuts_at_blank_lines_and_keeps_the_limit(self):
        text = "\n\n".join(f"Paragraph {i} " + "x" * 300 for i in range(20))
        parts = notify.split_text(text, 2000, 5)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(p) <= 2000 for p in parts))
        self.assertTrue(all(p.startswith("Paragraph") for p in parts))

    def test_says_when_text_was_left_out(self):
        parts = notify.split_text("word " * 5000, 2000, 2)
        self.assertEqual(len(parts), 2)
        self.assertLessEqual(len(parts[-1]), 2000)
        self.assertTrue(parts[-1].endswith("(cut: too long for the chat)"))

    def test_no_spaces_is_hard_cut(self):
        parts = notify.split_text("y" * 4500, 2000, 5)
        self.assertEqual([len(p) for p in parts], [2000, 2000, 500])


class PostWebhook(unittest.TestCase):
    def test_discord_gets_embeds_without_mentions(self):
        sent, fake = capture()
        with unittest.mock.patch.object(notify.urllib.request, "urlopen", fake):
            out = notify.post_webhook(DISCORD, "Title", "@everyone " + "z " * 3000, username="Ask",
                                      color="fail", link="http://ask:8081/chat/1")
        self.assertEqual(len(sent), 2)
        self.assertIn("2 messages", out)
        for body in sent:
            self.assertEqual(set(body), {"embeds", "username", "allowed_mentions"})
            self.assertEqual(body["allowed_mentions"], {"parse": []})
            self.assertLessEqual(len(body["embeds"][0]["description"]), notify.DISCORD_EMBED_LIMIT)
            self.assertEqual(body["embeds"][0]["color"], notify.COLORS["fail"])
        first = sent[0]["embeds"][0]
        self.assertEqual((first["title"], first["url"]), ("Title", "http://ask:8081/chat/1"))
        self.assertNotIn("title", sent[1]["embeds"][0])
        self.assertEqual(sent[1]["embeds"][0]["footer"]["text"], "Part 2 of 2")


class NamedWebhooks(unittest.TestCase):
    S = {"webhook_url": "https://hooks.example.com/default",
         "webhooks": [{"name": "Daily", "url": "https://discord.com/api/webhooks/9/secret"}]}

    def test_resolve(self):
        self.assertEqual(notify.resolve_webhook(self.S, ""), "https://hooks.example.com/default")
        self.assertEqual(notify.resolve_webhook(self.S, "default"), "https://hooks.example.com/default")
        self.assertEqual(notify.resolve_webhook(self.S, "daily"), "https://discord.com/api/webhooks/9/secret")
        with self.assertRaisesRegex(ValueError, "no webhook named 'news'"):
            notify.resolve_webhook(self.S, "news")
        self.assertEqual(notify.webhook_names(self.S), ["Daily"])

    def test_mask_shows_only_the_site(self):
        self.assertEqual(notify.mask_url("https://discord.com/api/webhooks/9/secret"), "https://discord.com/…")
        self.assertEqual(notify.mask_url(""), "")

    def test_other_services_get_one_text_message(self):
        sent, fake = capture()
        with unittest.mock.patch.object(notify.urllib.request, "urlopen", fake):
            notify.post_webhook("https://hooks.example.com/x", "Title", "Body", {"alert": {"id": 1}})
        self.assertEqual(sent, [{"text": "Body", "title": "Title", "alert": {"id": 1}}])

    def test_alert_webhook_is_one_message(self):
        sent, fake = capture()
        with unittest.mock.patch.object(notify.urllib.request, "urlopen", fake):
            notify.send_webhook({"webhook_url": DISCORD}, "Subject", "b " * 3000, {"rule": "r", "fired_at_text": "now"})
        self.assertEqual(len(sent), 1)

    def test_bad_urls_say_what_to_do(self):
        with self.assertRaisesRegex(ValueError, "no webhook URL is set"):
            notify.post_webhook("", "t", "x")
        with self.assertRaisesRegex(ValueError, "http:// or https://"):
            notify.post_webhook("ftp://example.com", "t", "x")

    def test_http_error_never_shows_the_url(self):
        def fail(req, timeout=0):
            raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, io.BytesIO(b'{"message": "Cannot send an empty message"}'))
        with unittest.mock.patch.object(notify.urllib.request, "urlopen", fail):
            with self.assertRaises(ValueError) as ctx:
                notify.post_webhook(DISCORD, "t", "x")
        self.assertIn("400 Bad Request", str(ctx.exception))
        self.assertIn("empty message", str(ctx.exception))
        self.assertNotIn("webhooks/123", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
