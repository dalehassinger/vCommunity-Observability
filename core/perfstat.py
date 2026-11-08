"""Raw counters of this container for the one Performance dashboard (on vCommunity Ask).

GET /perf/container returns them: CPU time, memory, disk and network byte counts and
uptime from the container's own cgroup files (v2 first, then v1). No Docker socket, no
secrets. Ask samples them every 10 seconds (ASK_VCO_INTERNAL_URL) and turns them into
rates. The readers are the same as in ask/app/perf.py."""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any


CG = Path("/sys/fs/cgroup")


def _read(path: Path) -> str | None:
    try:
        return path.read_text()
    except OSError:
        return None


def _int(s: str | None) -> int | None:
    try:
        return int(s.strip()) if s and s.strip() not in ("max", "") else None
    except ValueError:
        return None


def parse_io_stat(text: str) -> tuple[int, int]:
    r = w = 0
    for line in text.splitlines():
        m1, m2 = re.search(r"rbytes=(\d+)", line), re.search(r"wbytes=(\d+)", line)
        r += int(m1.group(1)) if m1 else 0
        w += int(m2.group(1)) if m2 else 0
    return r, w


def parse_blkio(text: str) -> tuple[int, int]:
    r = w = 0
    for line in text.splitlines():
        p = line.split()
        if len(p) == 3 and p[1] == "Read":
            r += int(p[2])
        elif len(p) == 3 and p[1] == "Write":
            w += int(p[2])
    return r, w


def parse_net_dev(text: str) -> tuple[int, int]:
    rx = tx = 0
    for line in text.splitlines()[2:]:
        if ":" not in line:
            continue
        name, rest = line.split(":", 1)
        if name.strip() == "lo":
            continue
        f = rest.split()
        if len(f) >= 9:
            rx += int(f[0])
            tx += int(f[8])
    return rx, tx


def container_raw() -> dict[str, Any]:
    """Counters (cumulative) and gauges of this container, cgroup v2 first, then v1."""
    out: dict[str, Any] = {"cgroup": None}
    if (CG / "cpu.stat").exists():
        out["cgroup"] = 2
        m = re.search(r"usage_usec\s+(\d+)", _read(CG / "cpu.stat") or "")
        out["cpu_ns"] = int(m.group(1)) * 1000 if m else None
        quota = (_read(CG / "cpu.max") or "max").split()
        out["cpu_limit"] = round(int(quota[0]) / int(quota[1]), 2) if quota and quota[0] != "max" and len(quota) > 1 else None
        out["mem"] = _int(_read(CG / "memory.current"))
        out["mem_limit"] = _int(_read(CG / "memory.max"))
        m = re.search(r"^inactive_file\s+(\d+)", _read(CG / "memory.stat") or "", re.M)
        out["mem_cache"] = int(m.group(1)) if m else 0
        out["io_read"], out["io_write"] = parse_io_stat(_read(CG / "io.stat") or "")
    elif (CG / "cpuacct" / "cpuacct.usage").exists():
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
    try:
        out["rss"] = int(re.search(r"VmRSS:\s+(\d+)", _read(Path("/proc/self/status")) or "").group(1)) * 1024
    except AttributeError:
        out["rss"] = None
    try:
        out["uptime"] = round(time.time() - Path("/proc/1").stat().st_mtime)
    except OSError:
        out["uptime"] = None
    return out
