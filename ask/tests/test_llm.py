"""Model providers (ask/app/llm.py): LM Studio next to Ollama, each one can be turned off.

Runs the stand-in LM Studio server (scripts/fake_lmstudio.py) in a thread; no LM Studio,
no Ollama needed.  Run from the ask/ folder:      python -m unittest discover -s tests -v
"""
from __future__ import annotations

import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from app import llm                    # noqa: E402
from app.ollama import OllamaError     # noqa: E402

try:                                   # scripts/ is not copied into the image
    import fake_lmstudio               # noqa: E402
except ImportError:
    fake_lmstudio = None


@unittest.skipIf(fake_lmstudio is None, "scripts/fake_lmstudio.py not found")
class LMStudioTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), fake_lmstudio.H)
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        # Not in a container here, so 127.0.0.1 stays as typed.
        p = mock.patch.object(llm, "in_container", return_value=False)
        p.start()
        self.addCleanup(p.stop)
        self.cfg = {"ollama_url": "http://127.0.0.1:9", "ollama_enabled": False,
                    "lmstudio_enabled": True, "lmstudio_url": self.url}

    def test_models_are_prefixed_and_typed(self):
        res = llm.client(self.cfg).models()
        self.assertTrue(res["ok"])
        names = {m["name"]: m for m in res["models"]}
        self.assertEqual(names["lmstudio:google/gemma-4-31b"]["kind"], "chat")
        self.assertIn("tools", names["lmstudio:google/gemma-4-31b"]["capabilities"])
        self.assertEqual(names["lmstudio:text-embedding-nomic-embed-text-v1.5"]["kind"], "embedding")
        self.assertEqual(names["lmstudio:google/gemma-4-31b"]["label"], "google/gemma-4-31b")
        self.assertFalse(res["providers"]["ollama"]["on"])
        self.assertNotIn("url", res["providers"]["ollama"])        # Ollama off: not even asked

    def test_embeddings_in_input_order(self):
        m = llm.client(self.cfg)
        a, b = m.embed("lmstudio:text-embedding-nomic-embed-text-v1.5", ["vm cpu ready", "datastore space"])
        self.assertEqual(a, fake_lmstudio.embed("vm cpu ready"))
        self.assertEqual(b, fake_lmstudio.embed("datastore space"))

    def test_stream_is_ollama_shaped_with_stats(self):
        out = list(llm.client(self.cfg).chat_stream("lmstudio:google/gemma-4-31b", [{"role": "user", "content": "hi"}],
                                                    options={"temperature": 0.2, "num_ctx": 9999}, think=True,
                                                    keep_alive="10m"))
        text = "".join((o.get("message") or {}).get("content") or "" for o in out)
        thinking = "".join((o.get("message") or {}).get("thinking") or "" for o in out)
        self.assertIn("LM Studio stand-in answer", text)
        self.assertEqual(thinking, "Thinking about the sources.")
        self.assertTrue(out[-1]["done"])
        self.assertEqual(out[-1]["eval_count"], 42)
        self.assertEqual(out[-1]["prompt_eval_count"], 321)
        last = fake_lmstudio.LAST
        self.assertEqual(last["model"], "google/gemma-4-31b")       # prefix removed
        self.assertEqual(last["ttl"], 600)
        self.assertNotIn("num_ctx", last)

    def test_thinking_hidden_when_off_and_think_tags_split(self):
        out = list(llm.client(self.cfg).chat_stream("lmstudio:google/gemma-4-31b",
                                                    [{"role": "user", "content": "thinktag please"}],
                                                    options={}, think=False))
        text = "".join((o.get("message") or {}).get("content") or "" for o in out)
        self.assertEqual(text, "Visible answer.")
        self.assertFalse(any((o.get("message") or {}).get("thinking") for o in out))

    def test_tool_call_round_trip(self):
        tools = [{"type": "function", "function": {"name": "Get-HostUptime", "description": "d",
                                                    "parameters": {"type": "object", "properties": {"host": {"type": "string"}}}}}]
        m = llm.client(self.cfg)
        msgs = [{"role": "user", "content": "host uptime?"}]
        out = list(m.chat_stream("lmstudio:google/gemma-4-31b", msgs, options={}, tools=tools))
        calls = out[-1]["message"]["tool_calls"]
        self.assertEqual(calls[0]["function"], {"name": "Get-HostUptime", "arguments": {"host": "x"}})
        # The answer loop appends the call and the tool's result the Ollama way.
        msgs += [{"role": "assistant", "content": "Let me check.", "tool_calls": calls},
                 {"role": "tool", "tool_name": "Get-HostUptime", "content": "up 5 days"}]
        out = list(m.chat_stream("lmstudio:google/gemma-4-31b", msgs, options={}, tools=tools))
        self.assertIn("up 5 days", "".join((o.get("message") or {}).get("content") or "" for o in out))
        sent = fake_lmstudio.LAST["messages"]
        self.assertEqual(sent[1]["tool_calls"][0]["id"], "call_abc")
        self.assertEqual(sent[1]["tool_calls"][0]["function"]["arguments"], '{"host": "x"}')
        self.assertEqual(sent[2], {"role": "tool", "tool_call_id": "call_abc", "content": "up 5 days"})

    def test_provider_off_is_a_clear_error(self):
        m = llm.client(self.cfg)
        with self.assertRaises(OllamaError) as e:
            list(m.chat_stream("qwen3.8:27b-mlx", [], options={}))
        self.assertIn("Ollama is turned off", str(e.exception))
        m = llm.client({**self.cfg, "lmstudio_enabled": False, "ollama_enabled": True})
        with self.assertRaises(OllamaError) as e:
            m.embed("lmstudio:x", ["a"])
        self.assertIn("LM Studio is turned off", str(e.exception))

    def test_unknown_model(self):
        with self.assertRaises(OllamaError) as e:
            llm.client(self.cfg).embed("lmstudio:nope", ["a"])
        self.assertIn("404", str(e.exception))

    def test_check_url(self):
        self.assertTrue(llm.check(llm.LMSTUDIO, self.url)["ok"])
        bad = llm.check(llm.LMSTUDIO, "http://127.0.0.1:9")
        self.assertFalse(bad["ok"])
        self.assertIn("Cannot reach LM Studio", bad["error"])


class Helpers(unittest.TestCase):
    def test_reach_in_container(self):
        with mock.patch.object(llm, "in_container", return_value=True):
            self.assertEqual(llm.reach("http://127.0.0.1:1234/"), "http://host.docker.internal:1234")
            self.assertEqual(llm.reach("http://localhost:1234"), "http://host.docker.internal:1234")
            self.assertEqual(llm.reach("http://studio.lab:1234"), "http://studio.lab:1234")
            with mock.patch.dict("os.environ", {"ASK_HOST_ALIAS": "off"}):
                self.assertEqual(llm.reach("http://127.0.0.1:1234"), "http://127.0.0.1:1234")
        with mock.patch.object(llm, "in_container", return_value=False):
            self.assertEqual(llm.reach("http://127.0.0.1:1234"), "http://127.0.0.1:1234")

    def test_keep_alive(self):
        self.assertEqual(llm.keep_alive_seconds("10m"), 600)
        self.assertEqual(llm.keep_alive_seconds("1h"), 3600)
        self.assertIsNone(llm.keep_alive_seconds("0"))
        self.assertIsNone(llm.keep_alive_seconds("-1"))

    def test_names(self):
        self.assertEqual(llm.provider_of("lmstudio:qwen/qwen3-8b"), llm.LMSTUDIO)
        self.assertEqual(llm.provider_of("gemma4:31b-mlx"), llm.OLLAMA)
        self.assertEqual(llm.bare("lmstudio:qwen/qwen3-8b"), "qwen/qwen3-8b")

    def test_nomic_prefix_for_lmstudio_name(self):
        from app.indexer import embed_prefixes
        self.assertEqual(embed_prefixes("lmstudio:text-embedding-nomic-embed-text-v1.5")[1], "search_query: ")
        self.assertEqual(embed_prefixes("nomic-embed-text")[1], "search_query: ")
        self.assertEqual(embed_prefixes("foo/nomic-embed")[1], "")   # Ollama names: unchanged rule


if __name__ == "__main__":
    unittest.main()


AGENT = Path(__file__).resolve().parents[1] / "agent" / "ask-host-agent.py"


@unittest.skipIf(not AGENT.exists(), "ask/agent not in this copy")
class HostAgentProcesses(unittest.TestCase):
    """The host agent counts Ollama's and LM Studio's processes apart (macOS ps shows whole paths)."""

    def test_split(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("ask_host_agent", AGENT)
        agent = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(agent)
        ps = "\n".join([
            "  101  12.0 204800 /Applications/Ollama.app/Contents/Resources/ollama",
            "  102  50.5 1048576 /Applications/LM Studio.app/Contents/MacOS/LM Studio",
            "  103   3.0 102400 /Applications/LM Studio.app/Contents/Frameworks/LM Studio Helper (GPU).app/Contents/MacOS/LM Studio Helper (GPU)",
            "  104 220.0 20971520 /Users/dale/.lmstudio/extensions/backends/vendor/app-mlx/bin/python3",
            "  105   1.0 2048 /usr/bin/python3",
            "  106   0.5 4096 /Users/dale/.lmstudio/bin/lms",
        ])
        self.assertEqual(agent.parse_ps(ps), {"cpu_pct": 12.0, "rss": 204800 * 1024, "processes": 1})
        lm = agent.parse_ps(ps, agent.is_lmstudio)
        self.assertEqual((lm["processes"], lm["cpu_pct"]), (4, 274.0))
