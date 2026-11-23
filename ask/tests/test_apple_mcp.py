"""Apple Reminders MCP server (ask/agent/apple-mcp.py), over stdio like the host agent runs it,
with a stand-in osascript that keeps the lists in a JSON file.
Run from the ask/ folder:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
SERVER = HERE / "agent" / "apple-mcp.py"

FAKE_OSA = r'''#!/usr/bin/env python3
import json, os, sys
state_file = os.environ["FAKE_REMINDERS"]
state = json.load(open(state_file))
script, args = sys.argv[2], sys.argv[3:]
def need(name):
    if name not in state:
        sys.stderr.write("execution error: No Reminders list named %s (1001)" % name); sys.exit(1)
    return state[name]
if "name of every list" in script:
    print("\x1f".join(state))
elif "make new reminder" in script:
    lst = need(args[0])
    for n in args[2:]:
        lst.append({"name": n, "done": False, "note": args[1]})
    print(len(args) - 2)
elif "set completed of r to true" in script:
    lst, done = need(args[0]), 0
    for n in args[1:]:
        for r in lst:
            if not r["done"] and r["name"] == n:
                r["done"] = True; done += 1
    print(done)
else:
    print("\x1f".join(r["name"] for r in need(args[0]) if not r["done"]))
json.dump(state, open(state_file, "w"))
'''


class AppleMcp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.state = d / "reminders.json"
        self.state.write_text(json.dumps({"Groceries": [{"name": "Milk", "done": False, "note": ""}], "Work": []}))
        osa = d / "osascript"
        osa.write_text(FAKE_OSA)
        osa.chmod(osa.stat().st_mode | stat.S_IEXEC)
        env = {**os.environ, "PATH": f"{d}{os.pathsep}{os.environ['PATH']}", "FAKE_REMINDERS": str(self.state)}
        self.p = subprocess.Popen([sys.executable, str(SERVER)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  text=True, env=env)
        self.n = 0

    def tearDown(self):
        self.p.stdin.close()
        self.p.wait(5)
        self.p.stdout.close()
        self.tmp.cleanup()

    def rpc(self, method, params=None):
        self.n += 1
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}}) + "\n")
        self.p.stdin.flush()
        return json.loads(self.p.stdout.readline())

    def call(self, name, **args):
        r = self.rpc("tools/call", {"name": name, "arguments": args})["result"]
        return r["content"][0]["text"], r["isError"]

    def test_flow(self):
        self.assertEqual(self.rpc("initialize", {"protocolVersion": "2025-06-18"})["result"]["serverInfo"]["name"], "apple")
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        tools = {t["name"]: t for t in self.rpc("tools/list")["result"]["tools"]}
        self.assertTrue(tools["reminders_list"]["annotations"]["readOnlyHint"])
        self.assertFalse(tools["reminders_add"]["annotations"]["readOnlyHint"])   # Ask asks first by default

        text, err = self.call("reminders_lists")
        self.assertIn("Groceries, Work", text)
        # Add: default list Groceries, duplicates (open item, repeated name) skipped, quotes and commas kept
        text, err = self.call("reminders_add", items=["Eggs", "milk", 'Bread "sourdough"', "Eggs"])
        self.assertFalse(err)
        self.assertIn('Added 2 item(s) to Groceries: Eggs, Bread "sourdough"', text)
        self.assertIn("skipped: milk", text)
        # a comma-joined string is split
        text, _ = self.call("reminders_add", list="Groceries", items="Apples, Coffee")
        self.assertIn("Added 2", text)
        text, _ = self.call("reminders_list")
        self.assertIn("5 open item(s)", text)
        self.assertIn("- Coffee", text)
        text, _ = self.call("reminders_complete", items=["Milk"])
        self.assertIn("Completed 1", text)
        self.assertIn("4 open", self.call("reminders_list", list="Groceries")[0])
        # unknown list: a clear error the model can act on
        text, err = self.call("reminders_add", list="Shopping", items=["x"])
        self.assertTrue(err)
        self.assertIn('no Reminders list named "Shopping"', text)


if __name__ == "__main__":
    unittest.main()
