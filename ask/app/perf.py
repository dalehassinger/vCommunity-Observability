"""Performance samples for the Performance page.

Every SAMPLE_SECONDS Ask records one sample with three parts and keeps 24 hours of them
in ask.db (table perf_samples):

  container  the vco-ask container itself, read from its own cgroup files (CPU, memory,
             disk and network I/O); no Docker socket is needed
  ollama     what Ollama has loaded (/api/ps): models, memory, GPU share, context size
  host       the Mac (or other host) from the optional Ask host agent
             (ask/agent/ask-host-agent.py), when ASK_AGENT_URL / the Agent URL setting
             points at it and ASK_AGENT_TOKEN matches the agent's token

Per-answer speed comes from the stats already saved with every answer."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

import requests

from .db import DB

log = logging.getLogger("ask.perf")
SAMPLE_SECONDS = 10
KEEP_SECONDS = 24 * 3600
CG = Path("/sys/fs/cgroup")

SCHEMA = """CREATE TABLE IF NOT EXISTS perf_samples (ts REAL PRIMARY KEY, data TEXT NOT NULL)"""


def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def _int(s: str | None) -> int | None:
    try:
        return int(s) if s not in (None, "", "max") else None
    except ValueError:
        return None


def parse_io_stat(text: str) -> tuple[int, int]:
    """cgroup v2 io.stat: '8:0 rbytes=123 wbytes=456 rios=..' per device -> (read, written)."""
    r = w = 0
    for m in re.finditer(r"\brbytes=(\d+)\s+wbytes=(\d+)", text or ""):
        r += int(m.group(1)); w += int(m.group(2))
    return r, w


def parse_blkio(text: str) -> tuple[int, int]:
    """cgroup v1 blkio.throttle.io_service_bytes: '8:0 Read 123' lines."""
    r = w = 0
    for line in (text or "").splitlines():
        p = line.split()
        if len(p) == 3 and p[2].isdigit():
            if p[1] == "Read":
                r += int(p[2])
            elif p[1] == "Write":
                w += int(p[2])
    return r, w


def parse_net_dev(text: str) -> tuple[int, int]:
    """/proc/net/dev -> (received, sent) bytes over all interfaces except lo."""
    rx = tx = 0
    for line in (text or "").splitlines()[2:]:
        name, _, rest = line.partition(":")
        f = rest.split()
        if name.strip() == "lo" or len(f) < 9:
            continue
        rx += int(f[0]); tx += int(f[8])
    return rx, tx


def container_raw() -> dict[str, Any]:
    """Counters (cumulative) and gauges of this container, cgroup v2 first, then v1."""
    out: dict[str, Any] = {"cgroup": None}
    if (CG / "cpu.stat").exists():                       # cgroup v2 (Rancher Desktop, Docker Desktop)
        out["cgroup"] = 2
        m = re.search(r"usage_usec\s+(\d+)", _read(CG / "cpu.stat") or "")
        out["cpu_ns"] = int(m.group(1)) * 1000 if m else None
        quota = (_read(CG / "cpu.max") or "max").split()
        out["cpu_limit"] = round(int(quota[0]) / int(quota[1]), 2) if quota and quota[0] != "max" and len(quota) > 1 else None
        out["mem"] = _int(_read(CG / "memory.current"))
        out["mem_limit"] = _int(_read(CG / "memory.max"))
        st = _read(CG / "memory.stat") or ""
        m = re.search(r"^inactive_file\s+(\d+)", st, re.M)
        out["mem_cache"] = int(m.group(1)) if m else 0
        out["io_read"], out["io_write"] = parse_io_stat(_read(CG / "io.stat") or "")
    elif (CG / "cpuacct" / "cpuacct.usage").exists():  # cgroup v1
        out["cgroup"] = 1
        out["cpu_ns"] = _int(_read(CG / "cpuacct" / "cpuacct.usage"))
        q, p = _int(_read(CG / "cpu" / "cpu.cfs_quota_us")), _int(_read(CG / "cpu" / "cpu.cfs_period_us"))
        out["cpu_limit"] = round(q / p, 2) if q and q > 0 and p else None
        out["mem"] = _int(_read(CG / "memory" / "memory.usage_in_bytes"))
        lim = _int(_read(CG / "memory" / "memory.limit_in_bytes"))
        out["mem_limit"] = lim if lim and lim < 1 << 60 else None
        m = re.search(r"^total_inactive_file\s+(\d+)", _read(CG / "memory" / "memory.stat") or "", re.M)
        out["mem_cache"] = int(m.group(1)) if m else 0
        out["io_read"], out["io_write"] = parse_blkio(_read(CG / "blkio" / "blkio.throttle.io_service_bytes") or "")
    out["net_rx"], out["net_tx"] = parse_net_dev(_read(Path("/proc/net/dev")) or "")
    out["cpus"] = os.cpu_count() or 1
    try:   # resident memory of the Ask process itself
        out["rss"] = int(re.search(r"VmRSS:\s+(\d+)", _read(Path("/proc/self/status")) or "").group(1)) * 1024
    except AttributeError:
        out["rss"] = None
    return out


def _rate(now: dict[str, Any], prev: dict[str, Any] | None, key: str, dt: float) -> float | None:
    if not prev or dt <= 0 or now.get(key) is None or prev.get(key) is None:
        return None
    d = now[key] - prev[key]
    return round(d / dt, 1) if d >= 0 else None


def container_view(now: dict[str, Any], prev: dict[str, Any] | None, dt: float) -> dict[str, Any]:
    cpus = now.get("cpu_limit") or now.get("cpus") or 1
    ns = _rate(now, prev, "cpu_ns", dt)
    mem = now.get("mem")
    used = mem - (now.get("mem_cache") or 0) if mem is not None else None   # like docker stats
    return {"cgroup": now.get("cgroup"), "cpus": cpus,
            "cpu_pct": round(ns / 1e9 / cpus * 100, 1) if ns is not None else None,
            "cpu_cores": round(ns / 1e9, 2) if ns is not None else None,
            "mem": used, "mem_limit": now.get("mem_limit"), "rss": now.get("rss"),
            "mem_pct": round(used / now["mem_limit"] * 100, 1) if used is not None and now.get("mem_limit") else None,
            "io_read_bps": _rate(now, prev, "io_read", dt), "io_write_bps": _rate(now, prev, "io_write", dt),
            "net_rx_bps": _rate(now, prev, "net_rx", dt), "net_tx_bps": _rate(now, prev, "net_tx", dt)}


def ollama_view(url: str) -> dict[str, Any]:
    out: dict[str, Any] = {"ok": False, "models": []}
    try:
        out["version"] = requests.get(f"{url}/api/version", timeout=3).json().get("version")
        ps = requests.get(f"{url}/api/ps", timeout=3).json()
        out["ok"] = True
    except (requests.RequestException, ValueError) as e:
        out["error"] = str(e)[:200]
        return out
    for m in ps.get("models") or []:
        size, vram = m.get("size") or 0, m.get("size_vram") or 0
        out["models"].append({"name": m.get("name") or m.get("model"), "size": size, "vram": vram,
                              "gpu_pct": round(vram / size * 100) if size else None,
                              "context": m.get("context_length"), "until": m.get("expires_at"),
                              "params": (m.get("details") or {}).get("parameter_size"),
                              "quant": (m.get("details") or {}).get("quantization_level")})
    out["mem"] = sum(m["size"] for m in out["models"])
    return out


def host_view(url: str, token: str) -> dict[str, Any] | None:
    if not url:
        return None
    try:
        r = requests.get(f"{url.rstrip('/')}/metrics", timeout=4, headers={"Authorization": f"Bearer {token}"} if token else {})
        if r.status_code == 401:
            return {"ok": False, "error": "The agent refused the token: set ASK_AGENT_TOKEN in .env to the agent's token."}
        r.raise_for_status()
        return {"ok": True, **r.json()}
    except (requests.RequestException, ValueError) as e:
        return {"ok": False, "error": f"No agent at {url}: {str(e)[:160]}"}


class Sampler:
    def __init__(self, db: DB, settings: Callable[[], dict[str, Any]]):
        self.db = db
        self.settings = settings
        self.prev: dict[str, Any] | None = None
        self.prev_t = 0.0
        self.last: dict[str, Any] | None = None
        self.lock = threading.Lock()
        with db.conn() as c:
            c.execute(SCHEMA)

    def sample(self) -> dict[str, Any]:
        """Take one sample (and store it). Thread-safe; the scheduler calls it."""
        with self.lock:
            cfg = self.settings()
            t = time.time()
            raw = container_raw()
            s = {"ts": t, "container": container_view(raw, self.prev, t - self.prev_t),
                 "ollama": ollama_view(cfg["ollama_url"].rstrip("/")),
                 "host": host_view(cfg.get("agent_url") or "", os.environ.get("ASK_AGENT_TOKEN", ""))}
            self.prev, self.prev_t, self.last = raw, t, s
            try:
                with self.db.conn() as c:
                    c.execute("INSERT OR REPLACE INTO perf_samples(ts, data) VALUES(?, ?)", (t, json.dumps(s)))
                    c.execute("DELETE FROM perf_samples WHERE ts < ?", (t - KEEP_SECONDS,))
            except Exception:   # a full disk must not stop the app
                log.exception("perf sample not saved")
            return s

    def history(self, hours: float) -> list[dict[str, Any]]:
        since = time.time() - max(0.05, min(24.0, hours)) * 3600
        with self.db.conn() as c:
            rows = c.execute("SELECT data FROM perf_samples WHERE ts >= ? ORDER BY ts", (since,)).fetchall()
        out = []
        for (d,) in rows:
            s = json.loads(d)
            h = s.get("host") or {}
            out.append({"ts": s["ts"], "c_cpu": s["container"].get("cpu_pct"), "c_mem": s["container"].get("mem"),
                        "c_rx": s["container"].get("net_rx_bps"), "c_tx": s["container"].get("net_tx_bps"),
                        "c_rd": s["container"].get("io_read_bps"), "c_wr": s["container"].get("io_write_bps"),
                        "o_mem": (s.get("ollama") or {}).get("mem"),
                        "h_cpu": (h.get("cpu") or {}).get("pct") if h.get("ok") else None,
                        "h_gpu": (h.get("gpu") or {}).get("pct") if h.get("ok") else None,
                        "h_mem": (h.get("memory") or {}).get("used") if h.get("ok") else None,
                        "h_press": (h.get("memory") or {}).get("pressure_pct") if h.get("ok") else None,
                        "h_ollama_cpu": (h.get("ollama") or {}).get("cpu_pct") if h.get("ok") else None,
                        "h_power": (h.get("power") or {}).get("total_w") if h.get("ok") else None,
                        "h_temp": (h.get("temp") or {}).get("gpu_c") if h.get("ok") else None})
        return out

    def answers(self, hours: float, limit: int = 200) -> list[dict[str, Any]]:
        """Speed of recent answers, from the stats saved with each one."""
        since = time.time() - max(0.05, min(24.0 * 30, hours)) * 3600
        with self.db.conn() as c:
            rows = c.execute("""SELECT m.created, m.model, m.stats, c.title FROM messages m JOIN chats c ON c.id = m.chat
                                WHERE m.role='assistant' AND m.created >= ? ORDER BY m.created DESC LIMIT ?""",
                             (since, limit)).fetchall()
        out = []
        for r in rows:
            try:
                st = json.loads(r["stats"] or "{}")
            except ValueError:
                st = {}
            if st.get("error"):
                continue
            out.append({"ts": r["created"], "model": r["model"], "title": r["title"], "seconds": st.get("seconds"),
                        "tok_s": st.get("tok_per_s"), "tokens": st.get("tokens"), "prompt": st.get("prompt_tokens"),
                        "kind": "whole-source" if st.get("overview") else ""})
        return out[::-1]
