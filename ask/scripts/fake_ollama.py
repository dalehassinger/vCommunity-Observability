"""A stand-in for Ollama, for tests without a GPU or a model download.

    python ask/scripts/fake_ollama.py            # listens on 127.0.0.1:11555

Embeddings are hashed bag-of-words vectors (so keyword overlap still finds the
right chunks), and the chat model replies with a short answer that quotes the
first sources it was given. Only the endpoints vCommunity Ask uses exist."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DIM = 256
MODELS = [
    {"name": "nomic-embed-text:latest", "size": 274302450, "capabilities": ["embedding"],
     "details": {"parameter_size": "137M"}},
    {"name": "qwen3.8:27b-mlx", "size": 18174721847, "capabilities": ["completion", "tools", "thinking"],
     "details": {"parameter_size": ""}},
    {"name": "gemma3:27b", "size": 17396936941, "capabilities": ["completion", "vision"],
     "details": {"parameter_size": "27.4B"}},
]


def embed(text: str) -> list[float]:
    v = [0.0] * DIM
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        h = int(hashlib.md5(w.encode()).hexdigest(), 16)
        v[h % DIM] += 1.0 if (h >> 8) % 2 else -1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/version":
            return self._json({"version": "0.0.0-fake"})
        if self.path == "/api/tags":
            return self._json({"models": MODELS})
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if self.path == "/api/embed":
            inputs = body.get("input")
            inputs = [inputs] if isinstance(inputs, str) else inputs
            return self._json({"model": body.get("model"), "embeddings": [embed(t) for t in inputs]})
        if self.path == "/api/chat" and body["messages"][0]["content"].startswith("You plan a dashboard"):
            # Dashboard reports: a canned plan over the sample RVTools vInfo sheet, with one
            # broken panel so the "left out" handling is exercised too.
            plan = [
                {"title": "Virtual machines", "chart": "kpi", "sql": "SELECT count(*) FROM vinfo WHERE template = 'False'"},
                {"title": "Powered on", "chart": "kpi", "sql": "SELECT count(*) FROM vinfo WHERE powerstate = 'poweredOn' AND template = 'False'"},
                {"title": "Total vCPUs", "chart": "kpi", "sql": "SELECT sum(cpus) FROM vinfo WHERE template = 'False'"},
                {"title": "Memory (GiB)", "chart": "kpi", "sql": "SELECT round(sum(memory) / 1024.0, 1) FROM vinfo WHERE template = 'False'"},
                {"title": "VMs by OS", "chart": "donut", "sql": "SELECT os_according_to_the_configuration_file AS os, count(*) AS vms FROM vinfo WHERE template = 'False' GROUP BY 1 ORDER BY vms DESC"},
                {"title": "VMs by power state", "chart": "pie", "sql": "SELECT powerstate, count(*) AS vms FROM vinfo WHERE template = 'False' GROUP BY 1 ORDER BY vms DESC"},
                {"title": "vCPUs and VMs per cluster", "chart": "bar", "sql": "SELECT cluster, count(*) AS vms, sum(cpus) AS vcpus FROM vinfo WHERE template = 'False' GROUP BY cluster ORDER BY vms DESC"},
                {"title": "Top 10 VMs by provisioned storage", "chart": "table", "sql": "SELECT vm, cluster, host, round(provisioned_mib / 1024.0, 1) AS provisioned_gib FROM vinfo ORDER BY provisioned_mib DESC LIMIT 10"},
                {"title": "Broken panel", "chart": "bar", "sql": "SELECT nope FROM vinfo"},
            ]
            msg = {"model": body.get("model"), "message": {"role": "assistant", "content": json.dumps(plan)}, "done": True}
            body_out = (json.dumps(msg) + "\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()
            self.wfile.write(body_out)
            return
        if self.path == "/api/chat" and body["messages"][0]["content"].startswith("You write ONE SQLite SELECT"):
            # Data questions: a canned query, chosen by a word in the question.
            q = body["messages"][1]["content"].rsplit("Question:", 1)[-1].lower()
            failed_once = len(body["messages"]) > 2
            if "broken" in q and not failed_once:
                sql = "SELECT nope FROM vinfo"
            elif "drop" in q:
                sql = "DROP TABLE vinfo"
            elif "per host" in q:
                sql = ("SELECT host, count(*) AS vms, sum(cpus) AS vcpus, round(sum(memory) / 1024.0, 1) AS memory_gib "
                       "FROM vinfo WHERE template = 'False' GROUP BY host ORDER BY vms DESC")
            elif "per cluster" in q:
                sql = ("SELECT cluster, count(*) AS vms, sum(cpus) AS vcpus FROM vinfo WHERE template = 'False' "
                       "GROUP BY cluster ORDER BY vms DESC")
            elif "by os" in q:
                sql = ("SELECT os_according_to_the_configuration_file AS os, count(*) AS vms FROM vinfo "
                       "WHERE template = 'False' GROUP BY 1 ORDER BY vms DESC")
            elif "all hosts" in q:
                sql = "SELECT host, cluster, cpu_model, num_cpu FROM vhost ORDER BY host"
            else:
                sql = "SELECT count(*) AS powered_on_vms FROM vinfo WHERE powerstate = 'poweredOn' AND template = 'False'"
            msg = {"model": body.get("model"), "message": {"role": "assistant", "content": f"```sql\n{sql}\n```"}, "done": True}
            body_out = (json.dumps(msg) + "\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()
            self.wfile.write(body_out)
            return
        if self.path == "/api/chat":
            user = body["messages"][-1]["content"]
            ids = re.findall(r"^\[(\d+)\] id: (\S+)", user, re.M)
            q = user.rsplit("Question:", 1)[-1].strip()
            if ids:
                lines = [f"**Stand-in answer** for: _{q}_", "", "The knowledge base covers this:"]
                for n, i in ids[:3]:
                    lines.append(f"- `{i}` explains part of it [{n}].")
                lines += ["", "| Source | Id |", "|---|---|"] + [f"| [{n}] | `{i}` |" for n, i in ids[:3]]
                text = "\n".join(lines)
            else:
                text = "The sources do not cover this question."
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()
            if body.get("think"):
                for w in ["Looking ", "at ", "the ", "sources…"]:
                    self.wfile.write((json.dumps({"message": {"role": "assistant", "content": "", "thinking": w}}) + "\n").encode())
            for tok in re.findall(r"\S+\s*|\n", text):
                self.wfile.write((json.dumps({"message": {"role": "assistant", "content": tok}, "done": False}) + "\n").encode())
                self.wfile.flush()
                time.sleep(0.01)
            self.wfile.write((json.dumps({"message": {"role": "assistant", "content": ""}, "done": True,
                                          "eval_count": 60, "eval_duration": 1_500_000_000,
                                          "prompt_eval_count": len(user) // 4}) + "\n").encode())
            return
        self._json({"error": "not found"}, 404)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 11555
    print(f"fake Ollama on http://127.0.0.1:{port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
