"""Apple Notes: ask/app/applenotes.py (note HTML, sending to the host agent), the agent's
POST /notes/create (with a stand-in osascript) and scheduled prompts' send_notes.
Run from the ask/ folder:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

from app import applenotes, schedules     # noqa: E402


def load_agent():
    spec = importlib.util.spec_from_file_location("agent_notes", HERE / "agent" / "ask-host-agent.py")
    agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agent)
    return agent


class FakeAgent(BaseHTTPRequestHandler):
    got: list = []
    answer = {"ok": True, "id": "x-coredata://1"}
    status = 200

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        FakeAgent.got.append((self.path, self.headers.get("Authorization"), json.loads(self.rfile.read(n))))
        body = json.dumps(FakeAgent.answer).encode()
        self.send_response(FakeAgent.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class NoteHtml(unittest.TestCase):
    def test_markdown_tables_link_and_no_raw_html(self):
        h = applenotes.to_html("## Hosts\n\n| Host | State |\n| --- | --- |\n| esx01 | ok |\n\n<script>x</script>",
                               "http://mac.local:8081/chat/abc", "Saved")
        self.assertIn("<h2>Hosts</h2>", h)
        self.assertIn('<table border="1"', h)
        self.assertIn("<td>esx01</td>", h)
        self.assertNotIn("<script>", h)                      # raw HTML is shown as text
        self.assertIn('href="http://mac.local:8081/chat/abc"', h)


class Send(unittest.TestCase):
    def setUp(self):
        FakeAgent.got, FakeAgent.answer, FakeAgent.status = [], {"ok": True, "id": "n1"}, 200
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeAgent)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        os.environ["ASK_AGENT_TOKEN"] = "tok-123"

    def tearDown(self):
        self.srv.shutdown()

    def test_send_ok_and_errors(self):
        r = applenotes.send(self.url, "My title", "**bold**", "", "", "")
        self.assertIn("vCommunity Ask", r)
        path, auth, body = FakeAgent.got[0]
        self.assertEqual(path, "/notes/create")
        self.assertIn("tok-123", auth or "")
        self.assertEqual(body["folder"], "vCommunity Ask")
        self.assertIn("<strong>bold</strong>", body["html"])
        FakeAgent.status = 404
        with self.assertRaisesRegex(RuntimeError, "older than 0.5.0"):
            applenotes.send(self.url, "t", "x")
        FakeAgent.status, FakeAgent.answer = 200, {"ok": False, "error": "macOS did not allow"}
        with self.assertRaisesRegex(RuntimeError, "did not allow"):
            applenotes.send(self.url, "t", "x")
        with self.assertRaisesRegex(RuntimeError, "No host agent"):
            applenotes.send("", "t", "x")


class AgentNotes(unittest.TestCase):
    def test_create_with_stand_in_osascript(self):
        agent = load_agent()
        agent.MAC = True                                   # run the macOS path with a fake osascript
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "log.json"
            fake = Path(tmp) / "osascript"
            fake.write_text("#!/usr/bin/env python3\nimport json,sys\n"
                            f"a=sys.argv[1:]\njson.dump({{'args':a[2:],'body':open(a[-1]).read()}},open({str(log)!r},'w'))\n"
                            "print('x-coredata://note/1')\n")
            fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
            old = os.environ["PATH"]
            os.environ["PATH"] = tmp + os.pathsep + old
            h = agent.make_handler(agent.Collector(60), "t" * 20, None, tmp, "draw.io")
            srv = ThreadingHTTPServer(("127.0.0.1", 0), h)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            url = f"http://127.0.0.1:{srv.server_address[1]}/notes/create"
            hdr = {"Authorization": "Bearer " + "t" * 20}
            try:
                self.assertEqual(requests.post(url, json={"title": "a", "html": "<p>b</p>"}, timeout=5).status_code, 401)
                self.assertEqual(requests.post(url, json={"title": "", "html": ""}, headers=hdr, timeout=5).status_code, 400)
                d = requests.post(url, json={"title": "Host \"status\" & more", "html": "<p>b</p>", "folder": "Lab"},
                                  headers=hdr, timeout=30).json()
                self.assertTrue(d["ok"], d)
                self.assertEqual(d["id"], "x-coredata://note/1")
                seen = json.loads(log.read_text())
                self.assertEqual(seen["args"][:2], ['Host "status" & more', "Lab"])   # title and folder as arguments
                self.assertIn("<h1>Host &quot;status&quot; &amp; more</h1><p>b</p>", seen["body"])
                self.assertFalse(Path(seen["args"][2]).exists())                      # temp file removed
            finally:
                srv.shutdown()
                os.environ["PATH"] = old


class ScheduleField(unittest.TestCase):
    def test_send_notes_alone_is_enough(self):
        body = {"name": "n", "prompt": "p", "send_email": False, "send_notes": True, "frequency": "daily", "at_time": "07:00",
                "timezone": "America/New_York"}
        s, errors = schedules.clean(body)
        self.assertNotIn("send_to", errors)
        self.assertEqual(s["send_notes"], 1)
        s, errors = schedules.clean({**body, "send_notes": False})
        self.assertIn("send_to", errors)


if __name__ == "__main__":
    unittest.main()
