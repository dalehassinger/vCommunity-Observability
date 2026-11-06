#!/usr/bin/env python3
"""Ask host agent: performance numbers of the machine that runs Ollama, for the
Performance page of vCommunity Ask.

Runs on the host (macOS; Linux works too, with fewer numbers), outside Docker. Python 3.9+
standard library only, so nothing to install and it works offline. Ask polls
  GET /metrics   JSON (needs the token: "Authorization: Bearer <token>" or "X-API-Key")
  GET /health    {"ok": true} (no token)
A background thread samples every few seconds, so a request never waits for the tools.

macOS sources (no sudo): top (CPU), vm_stat + sysctl (memory, pressure, swap), ioreg
(GPU utilization and memory), netstat (network), ps (Ollama's own CPU and memory), and
macmon (https://github.com/vladkens/macmon, MIT) for temperature and power if installed.

MCP bridge: the MCP servers listed in ~/.config/ask-host-agent/mcp.json (or --mcp-config;
the same format as an .mcp.json file) run here, on the host, and Ask calls their tools:
  GET  /mcp[?wait=0]           servers, their tools and errors (starts them when needed;
                               wait=0 answers at once and starts them in the background)
  POST /mcp/<server>/call      {"tool": "...", "arguments": {...}}
  POST /mcp/<server>/reload    restart the server and list its tools again
A server restarts by itself when its script (or a .ps1 next to it) or the config changes.

  python3 ask-host-agent.py --token-file ~/.config/ask-host-agent/token [--port 9101]

Install it as a LaunchAgent with ask/agent/Install-AskAgent.ps1 or install.sh."""

from __future__ import annotations

import argparse
import hmac
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "0.2.0"
MAC = sys.platform == "darwin"


def run(cmd: list, timeout: float = 8) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def sysctl(name: str) -> str:
    return run(["sysctl", "-n", name], 3).strip()


def num(s, kind=float):
    try:
        return kind(s)
    except (TypeError, ValueError):
        return None


# -- parsers (kept separate so they can be tested without a Mac) ------------------
def parse_top_cpu(text: str) -> dict | None:
    """Last 'CPU usage: 7.89% user, 10.52% sys, 81.57% idle' line of `top -l 2`."""
    m = re.findall(r"CPU usage:\s*([\d.]+)% user,\s*([\d.]+)% sys,\s*([\d.]+)% idle", text or "")
    if not m:
        return None
    u, s, i = (float(x) for x in m[-1])
    return {"pct": round(100 - i, 1), "user_pct": u, "sys_pct": s}


def parse_vm_stat(text: str, total: int | None) -> dict | None:
    """vm_stat pages -> bytes, laid out like Activity Monitor: used = app + wired + compressed."""
    m = re.search(r"page size of (\d+) bytes", text or "")
    if not m:
        return None
    page = int(m.group(1))
    p = {k.strip().lower(): int(v) for k, v in re.findall(r'^"?([^:"]+)"?:\s+(\d+)\.?\s*$', text, re.M)}
    get = lambda *names: next((p[n] for n in names if n in p), 0)
    wired = get("pages wired down") * page
    compressed = get("pages occupied by compressor") * page
    anon, purge = get("anonymous pages"), get("pages purgeable")
    app = (anon - purge) * page if anon else get("pages active") * page
    cached = (get("file-backed pages") + purge) * page
    out = {"total": total, "used": app + wired + compressed, "app": app, "wired": wired,
           "compressed": compressed, "cached": cached, "free": get("pages free") * page}
    if total:
        out["used_pct"] = round(out["used"] / total * 100, 1)
    return out


def parse_swap(text: str) -> dict:
    """sysctl vm.swapusage: 'total = 2048.00M  used = 1060.50M  free = 987.50M  (encrypted)'."""
    unit = {"K": 1 << 10, "M": 1 << 20, "G": 1 << 30}
    out = {}
    for k, v, u in re.findall(r"(total|used|free) = ([\d.]+)([KMG])", text or ""):
        out[k] = int(float(v) * unit[u])
    return {"swap_total": out.get("total"), "swap_used": out.get("used")}


def parse_ioreg_gpu(text: str) -> dict | None:
    """ioreg IOAccelerator PerformanceStatistics (Apple Silicon and most Macs)."""
    m = re.search(r'"PerformanceStatistics"\s*=\s*\{([^}]*)\}', text or "")
    if not m:
        return None
    stats = {k: int(v) for k, v in re.findall(r'"([^"]+)"=(\d+)', m.group(1))}
    pct = stats.get("Device Utilization %", stats.get("GPU Activity(%)"))
    if pct is None:
        return None
    return {"pct": pct, "renderer_pct": stats.get("Renderer Utilization %"), "tiler_pct": stats.get("Tiler Utilization %"),
            "mem_used": stats.get("In use system memory"), "mem_alloc": stats.get("Alloc system memory")}


def parse_netstat(text: str) -> tuple[int, int]:
    """netstat -ibn: per-interface <Link#n> rows; bytes counted from the right (Ibytes, Obytes)."""
    rx = tx = 0
    for line in (text or "").splitlines()[1:]:
        f = line.split()
        # en* only: Wi-Fi and Ethernet. Bridges (Docker VM), tunnels and AWDL would count traffic twice.
        if len(f) < 8 or not f[2].startswith("<Link") or not f[0].startswith("en"):
            continue
        try:
            rx += int(f[-5]); tx += int(f[-2])
        except ValueError:
            continue
    return rx, tx


def parse_ps(text: str) -> dict:
    """ps -Ao pid=,%cpu=,rss=,comm= -> Ollama's processes (server and model runners)."""
    cpu = rss = n = 0
    for line in (text or "").splitlines():
        f = line.split(None, 3)
        if len(f) == 4 and "ollama" in os.path.basename(f[3]).lower():
            cpu += num(f[1]) or 0; rss += (num(f[2], int) or 0) * 1024; n += 1
    return {"cpu_pct": round(cpu, 1), "rss": rss, "processes": n}


def parse_macmon(text: str) -> dict:
    """One `macmon pipe` JSON line -> power (W) and temperature (C)."""
    try:
        d = json.loads((text or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {}
    t = d.get("temp") or {}
    out = {"power": {"cpu_w": d.get("cpu_power"), "gpu_w": d.get("gpu_power"), "ane_w": d.get("ane_power"),
                     "total_w": d.get("all_power") or d.get("sys_power")},
           "temp": {"cpu_c": t.get("cpu_temp_avg"), "gpu_c": t.get("gpu_temp_avg")}}
    g = d.get("gpu_usage")
    if isinstance(g, list) and len(g) == 2:
        out["gpu_pct_macmon"] = round(g[1] * 100, 1) if g[1] <= 1 else g[1]
    return out


# -- collectors ------------------------------------------------------------------
class Collector:
    def __init__(self, interval: float):
        self.interval = interval
        self.data: dict = {"sampled": None}
        self.static = self._static()
        self._net = None
        self._cpu = None
        self.lock = threading.Lock()

    def _static(self) -> dict:
        h = {"name": socket.gethostname(), "os": f"{platform.system()} {platform.release()}", "arch": platform.machine()}
        if MAC:
            h["os"] = "macOS " + run(["sw_vers", "-productVersion"], 3).strip()
            h["model"] = sysctl("hw.model")
            h["chip"] = sysctl("machdep.cpu.brand_string")
            h["cores"] = {"total": num(sysctl("hw.ncpu"), int), "p": num(sysctl("hw.perflevel0.logicalcpu"), int),
                          "e": num(sysctl("hw.perflevel1.logicalcpu"), int)}
            h["mem_total"] = num(sysctl("hw.memsize"), int)
        else:
            h["cores"] = {"total": os.cpu_count()}
            m = re.search(r"MemTotal:\s+(\d+)", open("/proc/meminfo").read()) if os.path.exists("/proc/meminfo") else None
            h["mem_total"] = int(m.group(1)) * 1024 if m else None
            try:
                h["chip"] = re.search(r"model name\s*:\s*(.+)", open("/proc/cpuinfo").read()).group(1).strip()
            except (OSError, AttributeError):
                pass
        return h

    def _cpu_linux(self) -> dict | None:
        try:
            f = [int(x) for x in open("/proc/stat").readline().split()[1:]]
        except (OSError, ValueError):
            return None
        idle, total = f[3] + (f[4] if len(f) > 4 else 0), sum(f)
        prev, self._cpu = self._cpu, (idle, total)
        if not prev or total == prev[1]:
            return None
        return {"pct": round((1 - (idle - prev[0]) / (total - prev[1])) * 100, 1)}

    def _mem_linux(self) -> dict | None:
        try:
            mi = {k: int(v) * 1024 for k, v in re.findall(r"^(\w+):\s+(\d+) kB", open("/proc/meminfo").read(), re.M)}
        except OSError:
            return None
        total, avail = mi.get("MemTotal"), mi.get("MemAvailable", mi.get("MemFree"))
        used = total - avail if total and avail is not None else None
        return {"total": total, "used": used, "cached": mi.get("Cached"),
                "used_pct": round(used / total * 100, 1) if used is not None and total else None,
                "pressure_pct": round(used / total * 100, 1) if used is not None and total else None,
                "swap_total": mi.get("SwapTotal"), "swap_used": (mi.get("SwapTotal", 0) - mi.get("SwapFree", 0)) or 0}

    def collect(self) -> dict:
        t = time.time()
        d: dict = {"agent": "ask-host-agent", "version": VERSION, "host": self.static, "errors": []}
        try:
            d["load"] = [round(x, 2) for x in os.getloadavg()]
        except OSError:
            d["load"] = None
        if MAC:
            d["cpu"] = parse_top_cpu(run(["top", "-l", "2", "-n", "0", "-s", "1"], 10))
            mem = parse_vm_stat(run(["vm_stat"], 5), self.static.get("mem_total")) or {}
            level = num(sysctl("kern.memorystatus_level"), int)
            if level is not None:
                mem["pressure_pct"] = 100 - level        # macOS: % of memory that is NOT free for use
            mem.update(parse_swap(sysctl("vm.swapusage")))
            d["memory"] = mem or None
            d["gpu"] = parse_ioreg_gpu(run(["ioreg", "-r", "-d", "1", "-w", "0", "-c", "IOAccelerator"], 5))
            net = parse_netstat(run(["netstat", "-ibn"], 5))
            mm = shutil.which("macmon") or next((p for p in ("/opt/homebrew/bin/macmon", "/usr/local/bin/macmon") if os.path.exists(p)), None)
            if mm:
                extra = parse_macmon(run([mm, "pipe", "-s", "1", "-i", "500"], 10))
                d["power"], d["temp"] = extra.get("power"), extra.get("temp")
                if not d["gpu"] and extra.get("gpu_pct_macmon") is not None:
                    d["gpu"] = {"pct": extra["gpu_pct_macmon"]}
            else:
                d["power"] = d["temp"] = None
        else:
            d["cpu"] = self._cpu_linux()
            d["memory"] = self._mem_linux()
            d["gpu"] = None
            d["power"] = d["temp"] = None
            try:
                rows = open("/proc/net/dev").read().splitlines()[2:]
                f = [r.split(":", 1) for r in rows]
                net = (sum(int(x[1].split()[0]) for x in f if x[0].strip() != "lo"),
                       sum(int(x[1].split()[8]) for x in f if x[0].strip() != "lo"))
            except (OSError, ValueError, IndexError):
                net = (0, 0)
        prev, self._net = self._net, (t, net)
        d["net"] = None
        if prev and t > prev[0]:
            d["net"] = {"rx_bps": max(0, round((net[0] - prev[1][0]) / (t - prev[0]))),
                        "tx_bps": max(0, round((net[1] - prev[1][1]) / (t - prev[0])))}
        disk_path = "/System/Volumes/Data" if os.path.isdir("/System/Volumes/Data") else "/"
        du = shutil.disk_usage(disk_path)
        d["disk"] = {"path": disk_path, "total": du.total, "used": du.used, "free": du.free,
                     "used_pct": round(du.used / du.total * 100, 1)}
        d["ollama"] = parse_ps(run(["ps", "-Ao", "pid=,%cpu=,rss=,comm="], 5))
        ncpu = (self.static.get("cores") or {}).get("total") or os.cpu_count() or 1
        d["ollama"]["cpu_pct_of_host"] = round(d["ollama"]["cpu_pct"] / ncpu, 1)
        for k in ("cpu", "memory"):
            if not d.get(k):
                d["errors"].append(f"{k} not available")
        d["sampled"] = t
        d["sample_seconds"] = round(time.time() - t, 2)
        return d

    def loop(self) -> None:
        while True:
            try:
                d = self.collect()
                with self.lock:
                    self.data = d
            except Exception as e:  # keep serving the last good sample
                with self.lock:
                    self.data = {**self.data, "errors": [f"sample failed: {e}"]}
            time.sleep(self.interval)


# -- MCP bridge ------------------------------------------------------------------------
# Runs the MCP servers listed in an .mcp.json style file (stdio servers only: a command,
# its args and env) on this machine and passes Ask's tool calls to them. The server's own
# script stays where it is, so it can be edited in place; when the script (or the config
# file) changes, the server is restarted on the next request.
MCP_PROTOCOL = "2025-06-18"
EXTRA_PATH = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/local/microsoft/powershell/7", "/usr/bin", "/bin"]


class McpServer:
    def __init__(self, name: str, spec: dict):
        self.name = name
        self.spec = spec
        self.proc = None
        self.lock = threading.Lock()          # start/stop
        self.wlock = threading.Lock()         # writes to stdin
        self.waiters: dict = {}               # id -> [Event, response]
        self.next_id = 0
        self.tools: list = []
        self.info: dict = {}
        self.error = ""
        self.stderr: list = []
        self.started = None
        self.starting = False
        self.watch: dict = {}                 # file -> mtime when started

    # files whose change means "restart": arguments that are existing files (the .ps1)
    def _watched(self) -> dict:
        out = {}
        for a in [self.spec.get("command", "")] + list(self.spec.get("args") or []):
            if isinstance(a, str) and a.endswith((".ps1", ".psm1", ".py", ".js", ".sh")) and os.path.isfile(a):
                try:
                    out[a] = os.path.getmtime(a)
                except OSError:
                    pass
                # helper files dot-sourced next to the script (MCP-ToolSupport.ps1 and friends)
                folder = os.path.dirname(a)
                try:
                    for f in os.listdir(folder):
                        p = os.path.join(folder, f)
                        if f.endswith((".ps1", ".psm1")) and p != a and os.path.isfile(p):
                            out[p] = os.path.getmtime(p)
                except OSError:
                    pass
        return out

    def changed(self) -> bool:
        return bool(self.watch) and self._watched() != self.watch

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        p, self.proc = self.proc, None
        if p and p.poll() is None:
            try:
                p.stdin.close()
            except OSError:
                pass
            try:
                p.wait(3)
            except subprocess.TimeoutExpired:
                p.kill()
        for w in list(self.waiters.values()):
            w[1] = {"error": {"code": -32000, "message": "MCP server stopped"}}
            w[0].set()
        self.waiters.clear()

    def start(self) -> None:
        self.stop()
        self.error, self.stderr = "", []
        cmd = self.spec.get("command") or ""
        env = dict(os.environ)
        env["PATH"] = os.pathsep.join([env.get("PATH", "")] + EXTRA_PATH)
        env.update({str(k): str(v) for k, v in (self.spec.get("env") or {}).items()})
        exe = shutil.which(cmd, path=env["PATH"]) or cmd
        args = [str(a) for a in (self.spec.get("args") or [])]
        script = next((a for a in args if os.path.isfile(a)), None)
        cwd = self.spec.get("cwd") or (os.path.dirname(script) if script else None)
        self.watch = self._watched()
        try:
            self.proc = subprocess.Popen([exe] + args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, cwd=cwd, env=env, text=True, bufsize=1,
                                         encoding="utf-8", errors="replace")
        except OSError as e:
            self.proc = None
            self.error = f"Cannot start {cmd}: {e}"
            raise RuntimeError(self.error)
        self.started = time.time()
        threading.Thread(target=self._read_out, args=(self.proc,), daemon=True).start()
        threading.Thread(target=self._read_err, args=(self.proc,), daemon=True).start()
        init = self.request("initialize", {"protocolVersion": MCP_PROTOCOL, "capabilities": {},
                                           "clientInfo": {"name": "vcommunity-ask-host-agent", "version": VERSION}}, 90)
        self.info = init.get("serverInfo") or {}
        self.notify("notifications/initialized")
        self.tools = (self.request("tools/list", {}, 60).get("tools")) or []
        self.error = ""

    def _read_out(self, proc) -> None:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                self.stderr = (self.stderr + [f"(stdout, not JSON) {line[:300]}"])[-40:]
                continue
            w = self.waiters.get(msg.get("id")) if isinstance(msg, dict) else None
            if w:
                w[1] = msg
                w[0].set()
        if self.proc is proc:
            code = proc.poll()
            self.error = self.error or f"MCP server exited (code {code})"
            for w in list(self.waiters.values()):
                w[1] = {"error": {"code": -32000, "message": self.error}}
                w[0].set()

    def _read_err(self, proc) -> None:
        for line in proc.stderr:
            self.stderr = (self.stderr + [line.rstrip()[:500]])[-40:]

    def _send(self, msg: dict) -> None:
        if not self.running():
            raise RuntimeError(self.error or "MCP server is not running")
        with self.wlock:
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()

    def notify(self, method: str, params: dict | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})})

    def request(self, method: str, params: dict, timeout: float) -> dict:
        with self.wlock:
            self.next_id += 1
            rid = self.next_id
        w = [threading.Event(), None]
        self.waiters[rid] = w
        try:
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            if not w[0].wait(timeout):
                try:
                    self.notify("notifications/cancelled", {"requestId": rid, "reason": "timeout"})
                except (RuntimeError, OSError):
                    pass
                raise TimeoutError(f"{method} took longer than {int(timeout)} s")
        finally:
            self.waiters.pop(rid, None)
        msg = w[1] or {}
        if msg.get("error"):
            raise RuntimeError(msg["error"].get("message") or str(msg["error"]))
        return msg.get("result") or {}

    def needs_start(self) -> bool:
        return not self.running() or self.changed()

    def ensure(self) -> None:
        with self.lock:
            if self.needs_start():
                self.starting = True
                try:
                    self.start()
                except (RuntimeError, TimeoutError, OSError) as e:
                    self.error = str(e)
                    self.tools = []
                    self.stop()
                    raise RuntimeError(self.error)
                finally:
                    self.starting = False

    def ensure_soon(self) -> None:
        """Start in the background (for quick listings)."""
        if self.needs_start() and not self.starting:
            self.starting = True

            def go():
                try:
                    self.ensure()
                except RuntimeError:
                    pass
            threading.Thread(target=go, daemon=True).start()

    def view(self) -> dict:
        return {"name": self.name, "running": self.running(), "starting": self.starting, "error": self.error,
                "command": " ".join([str(self.spec.get("command", ""))] + [str(a) for a in self.spec.get("args") or []]),
                "server": self.info, "tools": self.tools, "stderr": self.stderr[-15:],
                "started": self.started, "changed": self.changed()}


class McpHub:
    """The servers of one config file. The file is read again when it changes."""

    def __init__(self, path: str):
        self.path = path
        self.mtime = None
        self.servers: dict = {}
        self.error = ""
        self.lock = threading.Lock()

    def load(self) -> None:
        with self.lock:
            try:
                m = os.path.getmtime(self.path)
            except OSError:
                self.error = "" if not self.servers else f"{self.path} is gone"
                for s in self.servers.values():
                    s.stop()
                self.servers, self.mtime = {}, None
                return
            if m == self.mtime:
                return
            self.mtime = m
            try:
                raw = json.load(open(self.path, encoding="utf-8"))
                specs = raw.get("mcpServers") or raw.get("servers") or {}
                self.error = ""
            except (OSError, ValueError) as e:
                self.error = f"Cannot read {self.path}: {e}"
                return
            for name in list(self.servers):
                if name not in specs or specs[name] != self.servers[name].spec:
                    self.servers.pop(name).stop()
            for name, spec in specs.items():
                if not isinstance(spec, dict) or spec.get("type", "stdio") != "stdio" or not spec.get("command"):
                    continue
                if name not in self.servers:
                    self.servers[name] = McpServer(name, spec)

    def list(self, wait: bool = True) -> dict:
        """wait=False answers at once (servers that need a start get one in the background)."""
        self.load()
        out = []
        for s in self.servers.values():
            if wait:
                try:
                    s.ensure()
                except RuntimeError:
                    pass
            else:
                s.ensure_soon()
            out.append(s.view())
        return {"config": self.path, "error": self.error, "servers": out}

    def get(self, name: str) -> McpServer:
        self.load()
        s = self.servers.get(name)
        if not s:
            raise KeyError(name)
        return s


def make_handler(collector: Collector, token: str, hub: "McpHub | None" = None):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"ask-host-agent/{VERSION}"

        def log_message(self, fmt, *args):  # quiet: Ask polls every 10 seconds
            pass

        def _send(self, code: int, body: dict) -> None:
            raw = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def _authorized(self) -> bool:
            auth = self.headers.get("Authorization", "")
            supplied = auth[7:].strip() if auth.lower().startswith("bearer ") else self.headers.get("X-API-Key", "")
            if hmac.compare_digest(supplied.encode(), token.encode()):
                return True
            self._send(401, {"error": "missing or wrong token"})
            return False

        def do_POST(self):
            # MCP bridge: POST /mcp/<server>/call {"tool", "arguments", "timeout"} and /mcp/<server>/reload
            parts = self.path.split("?")[0].strip("/").split("/")
            if len(parts) != 3 or parts[0] != "mcp" or parts[2] not in ("call", "reload"):
                return self._send(404, {"error": "not found"})
            if not self._authorized():
                return
            if hub is None:
                return self._send(404, {"error": "no MCP config"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(min(n, 4_000_000)) or b"{}") if n else {}
                srv = hub.get(parts[1])
            except KeyError:
                return self._send(404, {"error": f"unknown MCP server {parts[1]}"})
            except ValueError:
                return self._send(400, {"error": "body is not JSON"})
            if parts[2] == "reload":
                with srv.lock:
                    srv.stop()
                try:
                    srv.ensure()
                except RuntimeError:
                    pass
                return self._send(200, srv.view())
            tool, args = body.get("tool"), body.get("arguments") or {}
            if not isinstance(tool, str) or not isinstance(args, dict):
                return self._send(400, {"error": "tool (string) and arguments (object) are needed"})
            t0 = time.time()
            try:
                srv.ensure()
                res = srv.request("tools/call", {"name": tool, "arguments": args},
                                  float(body.get("timeout") or 180))
                return self._send(200, {"ok": True, "result": res, "ms": int((time.time() - t0) * 1000)})
            except (RuntimeError, TimeoutError, OSError) as e:
                return self._send(200, {"ok": False, "error": str(e), "ms": int((time.time() - t0) * 1000)})

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/health":
                return self._send(200, {"ok": True, "version": VERSION, "mcp": hub is not None})
            if path == "/mcp":
                if not self._authorized():
                    return
                wait = "wait=0" not in (self.path.split("?") + [""])[1]
                return self._send(200, hub.list(wait) if hub else {"config": "", "error": "", "servers": []})
            if path != "/metrics":
                return self._send(404, {"error": "not found"})
            if not self._authorized():
                return
            with collector.lock:
                data = dict(collector.data)
            data["age_seconds"] = round(time.time() - data["sampled"], 1) if data.get("sampled") else None
            return self._send(200, data)

    return Handler


def main() -> None:
    ap = argparse.ArgumentParser(description="Performance agent for vCommunity Ask")
    ap.add_argument("--port", type=int, default=int(os.environ.get("ASK_AGENT_PORT", 9101)))
    ap.add_argument("--bind", default=os.environ.get("ASK_AGENT_BIND", "0.0.0.0"),
                    help="Address to listen on. 0.0.0.0 so the Docker VM can reach it; the token protects it.")
    ap.add_argument("--token-file", default=os.path.expanduser("~/.config/ask-host-agent/token"))
    ap.add_argument("--interval", type=float, default=5.0, help="Seconds between samples")
    ap.add_argument("--once", action="store_true", help="Print one sample as JSON and exit (for testing)")
    ap.add_argument("--mcp-config", default=os.environ.get("ASK_AGENT_MCP_CONFIG", os.path.expanduser("~/.config/ask-host-agent/mcp.json")),
                    help="An .mcp.json style file of MCP servers to offer to Ask (stdio servers: command, args, env)")
    ap.add_argument("--mcp-list", action="store_true",
                    help="Start the MCP servers of --mcp-config, print their tools and errors, and exit (for testing)")
    a = ap.parse_args()
    if a.mcp_list:
        hub = McpHub(a.mcp_config)
        d = hub.list()
        print(f"MCP config: {d['config']}" + (f"  ERROR: {d['error']}" if d["error"] else ""))
        if not d["servers"]:
            print("No stdio MCP servers in it.")
        for s in d["servers"]:
            print(f"\n{s['name']}: {'running' if s['running'] else 'NOT running'}  ({s['command']})")
            if s["error"]:
                print(f"  error: {s['error']}")
            for t in s["tools"]:
                ro = (t.get("annotations") or {}).get("readOnlyHint")
                print(f"  - {t['name']}{'' if ro else '  (changes things)'}: {(t.get('description') or '').strip()[:90]}")
            if s["stderr"] and (s["error"] or not s["tools"]):
                print("  stderr:\n    " + "\n    ".join(s["stderr"]))
        for s in hub.servers.values():
            s.stop()
        return
    c = Collector(a.interval)
    if a.once:
        c.collect()
        time.sleep(1)
        print(json.dumps(c.collect(), indent=2))
        return
    token = os.environ.get("ASK_AGENT_TOKEN", "")
    if not token and os.path.exists(a.token_file):
        token = open(a.token_file).read().strip()
    if len(token) < 16:
        sys.exit(f"No token (at least 16 characters): set ASK_AGENT_TOKEN or write one to {a.token_file}")
    threading.Thread(target=c.loop, daemon=True).start()
    srv = ThreadingHTTPServer((a.bind, a.port), make_handler(c, token, McpHub(a.mcp_config)))
    print(f"ask-host-agent {VERSION} on {a.bind}:{a.port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
