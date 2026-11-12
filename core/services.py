"""Long-running module services (module.yaml `services:`).

A service is a script (.py or .ps1) that runs for as long as its module is on, for work a
scheduled collector can't do: listening on a port (the Logs module's syslog server), or
consuming a queue. The core:

  * starts every service of an available module when the app starts,
  * restarts a service that exits (after restart_seconds, doubling up to 5 minutes while
    it keeps failing quickly),
  * stops it when the module is switched off or vCommunity Observability is paused, and
    starts it again when it is switched back on,
  * stops everything when the app stops.

Environment passed to a service: VCO_MODULE, VCO_MODULE_DIR, VCO_DATA_DIR, VCO_SETTINGS
(the module's settings as JSON) and VCO_MOCK. Its stdout and stderr go to the container log
(prefixed with module/service), and the last lines are kept for the Modules page.
"""

from __future__ import annotations

import collections
import json
import logging
import os
import subprocess
import sys
import threading
import time
from typing import Any

from . import control
from .modules import Module, Service

log = logging.getLogger("vco.services")
MAX_BACKOFF = 300


class _Running:
    def __init__(self, mod: Module, svc: Service):
        self.mod, self.svc = mod, svc
        self.proc: subprocess.Popen | None = None
        self.started_at: float | None = None
        self.restarts = 0
        self.backoff = svc.restart_seconds
        self.next_start = 0.0
        self.last_exit: int | None = None
        self.lines: collections.deque[str] = collections.deque(maxlen=40)

    def status(self) -> dict[str, Any]:
        running = self.proc is not None and self.proc.poll() is None
        return {
            "module": self.mod.name, "name": self.svc.name, "script": self.svc.script.name,
            "language": self.svc.language, "running": running,
            "pid": self.proc.pid if running and self.proc else None,
            "since": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(self.started_at)) if running and self.started_at else None,
            "restarts": self.restarts, "last_exit": self.last_exit, "log": list(self.lines)[-8:],
        }


class ServiceManager:
    def __init__(self, cfg, modules: dict[str, Module]):
        self.cfg = cfg
        self.modules = modules
        self._items: dict[tuple[str, str], _Running] = {}
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not any(m.services for m in self.modules.values()):
            return
        self._thread = threading.Thread(target=self._loop, name="vco-services", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            for item in self._items.values():
                self._kill(item)

    def statuses(self, module: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            return [i.status() for (m, _), i in self._items.items() if module in (None, m)]

    def status(self, module: str, name: str) -> dict[str, Any] | None:
        with self._lock:
            item = self._items.get((module, name))
            return item.status() if item else None

    def wanted(self, mod: Module) -> bool:
        return mod.available and control.is_on("observability")

    # -- internals ----------------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:  # never let the supervisor die
                log.exception("service supervisor")
            self._stop.wait(2)

    def _tick(self) -> None:
        now = time.monotonic()
        with self._lock:
            for mod in list(self.modules.values()):
                for svc in mod.services:
                    key = (mod.name, svc.name)
                    item = self._items.setdefault(key, _Running(mod, svc))
                    item.mod = mod
                    alive = item.proc is not None and item.proc.poll() is None
                    if not self.wanted(mod):
                        if alive:
                            log.info("stopping service %s/%s (module off or app paused)", *key)
                            self._kill(item)
                        continue
                    if alive:
                        # Running steadily for a minute: forget earlier quick failures.
                        if item.started_at and time.time() - item.started_at > 60:
                            item.backoff = svc.restart_seconds
                        continue
                    if item.proc is not None:            # it exited by itself
                        item.last_exit = item.proc.returncode
                        log.warning("service %s/%s exited with code %s; restart in %ss",
                                    *key, item.last_exit, item.backoff)
                        item.proc = None
                        item.next_start = now + item.backoff
                        item.backoff = min(MAX_BACKOFF, item.backoff * 2)
                        item.restarts += 1
                        continue
                    if now >= item.next_start:
                        self._spawn(item)

    def _spawn(self, item: _Running) -> None:
        mod, svc = item.mod, item.svc
        if svc.language == "python":
            cmd = [sys.executable, "-u", str(svc.script)]
        else:
            cmd = ["pwsh", "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(svc.script)]
        env = {k: v for k, v in os.environ.items() if not k.startswith("VCO_") or k.startswith("VCO_SYSLOG_")}
        env.update({
            "VCO_MODULE": mod.name, "VCO_MODULE_DIR": str(mod.path), "VCO_DATA_DIR": str(self.cfg.data_dir.resolve()),
            "VCO_SETTINGS": json.dumps(mod.settings), "VCO_MOCK": "1" if self.cfg.mock else "0",
            "PYTHONUNBUFFERED": "1",
        })
        try:
            item.proc = subprocess.Popen(cmd, cwd=str(mod.path), env=env, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True,
                                         errors="replace", bufsize=1)
        except OSError as exc:
            item.lines.append(f"cannot start: {exc}")
            item.last_exit = -1
            item.next_start = time.monotonic() + item.backoff
            item.backoff = min(MAX_BACKOFF, item.backoff * 2)
            log.error("service %s/%s cannot start: %s", mod.name, svc.name, exc)
            return
        item.started_at = time.time()
        log.info("service %s/%s started (pid %s)", mod.name, svc.name, item.proc.pid)
        threading.Thread(target=self._pump, args=(item, item.proc), daemon=True).start()

    @staticmethod
    def _pump(item: _Running, proc: subprocess.Popen) -> None:
        prefix = f"{item.mod.name}/{item.svc.name}"
        for line in proc.stdout or []:
            line = line.rstrip()
            if line:
                item.lines.append(line[:500])
                log.info("[%s] %s", prefix, line[:500])

    @staticmethod
    def _kill(item: _Running) -> None:
        proc = item.proc
        item.proc = None
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
