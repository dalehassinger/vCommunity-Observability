"""On / off switches for the two parts of vCommunity-Observability: the dashboards
(vCommunity Observability, container vco) and the chat (vCommunity Ask, vco-ask).

Both containers mount the same folder (./control -> /app/control) and read and write
one small JSON file there, so either Settings page can switch either part:

    {"observability": true, "ask": true, "updated": 1794137664.1, "by": "observability"}

"Off" pauses a part, the container keeps running so it can be switched on again:
- Observability off: no collector runs (scheduled, Run now, start-up) and no scheduled
  exports; dashboards and the API keep showing the last collected data.
- Ask off: no new questions (chat page and POST /api/ask) and no source syncs.
Each part also hides its sidebar link to the other part while that one is off.

A missing file means both are on. Without the ./control folder mounted the switches
read as on and saving them reports how to mount it.

This file is identical in core/control.py and ask/app/control.py (the smoke test checks)."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

APPS = {"observability": "vCommunity Observability", "ask": "vCommunity Ask"}
_lock = threading.Lock()
_cache: dict[str, Any] = {"path": None, "mtime": None, "at": 0.0, "data": None}


class ControlError(RuntimeError):
    pass


def path() -> Path:
    return Path(os.environ.get("VCO_CONTROL_FILE") or "/app/control/apps.json")


def load() -> dict[str, bool]:
    """{"observability": bool, "ask": bool}; re-read when the file changes (checked at most once a second)."""
    p = path()
    with _lock:
        now = time.time()
        if _cache["path"] != str(p):
            _cache.update(path=str(p), mtime=None, at=0.0, data=None)
        if _cache["data"] is not None and now - _cache["at"] < 1:
            return dict(_cache["data"])
        try:
            mtime = p.stat().st_mtime_ns
        except OSError:
            mtime = None
        if _cache["data"] is not None and mtime == _cache["mtime"]:
            _cache["at"] = now
            return dict(_cache["data"])
        data = {k: True for k in APPS}
        if mtime is not None:
            try:
                raw = json.loads(p.read_text() or "{}")
                for k in APPS:
                    if isinstance(raw.get(k), bool):
                        data[k] = raw[k]
            except (OSError, ValueError):
                pass        # unreadable or half-written: keep everything on
        _cache.update(mtime=mtime, at=now, data=data)
        return dict(data)


def is_on(app: str) -> bool:
    return load().get(app, True)


def status() -> dict[str, Any]:
    p = path()
    folder_ok = p.parent.is_dir()
    writable = folder_ok and os.access(p.parent, os.W_OK) and (not p.exists() or os.access(p, os.W_OK))
    error = ""
    if not folder_ok:
        error = (f"The folder {p.parent} is not mounted, so the switches can't be saved. Add "
                 f"'./control:/app/control' to both services in docker-compose.yml (see the README).")
    elif not writable:
        error = f"{p} can't be written (folder permissions). On the host: chmod 777 control"
    on = load()
    return {"apps": [{"key": k, "title": t, "on": on[k]} for k, t in APPS.items()],
            "path": str(p), "writable": writable, "error": error}


def set_on(app: str, on: bool, by: str) -> dict[str, bool]:
    if app not in APPS:
        raise ControlError(f"unknown part {app!r}")
    p = path()
    st = status()
    if st["error"]:
        raise ControlError(st["error"])
    with _lock:
        data = {k: True for k in APPS}
        try:
            raw = json.loads(p.read_text() or "{}") if p.exists() else {}
            data.update({k: raw[k] for k in APPS if isinstance(raw.get(k), bool)})
        except (OSError, ValueError):
            pass
        data[app] = bool(on)
        body = json.dumps({**data, "updated": time.time(), "by": by}, indent=2)
        fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".apps-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(body + "\n")
            os.chmod(tmp, 0o666)          # the other container runs as another user
            os.replace(tmp, p)
        except OSError as e:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise ControlError(f"could not save {p}: {e}") from None
        _cache.update(mtime=None, at=0.0, data=None)
    return load()
