"""Run collector scripts (Python or PowerShell) as subprocesses.

Contract for a collector script:
  * Connection details arrive as environment variables, e.g.
    VCO_VCENTER_HOST / VCO_VCENTER_USER / VCO_VCENTER_PASSWORD / VCO_VCENTER_VERIFY_SSL.
  * When connections includes vcenter, VCO_VCENTERS is also set to a JSON array of
    every selected instance ({id, label, host, user, password, verify_ssl}). Legacy
    VCO_VCENTER_* mirrors the first instance.
  * VCO_MOCK=1 asks the script to emit sample data instead of connecting.
  * VCO_SETTINGS holds the module's settings as JSON.
  * The script writes ONE JSON object {"<table>": [ {row}, ... ], ...} to the
    file named in VCO_OUTPUT_FILE (preferred: immune to stray console output
    such as PowerCLI warnings), or prints it to stdout.
  * Logs go to stderr. A non-zero exit code marks the run as failed.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from .config import Config
from .modules import Collector, Export, Module
from .storage import Storage, utcnow
from .vcenters import normalize_vcenter, public_instance, selected_instances

log = logging.getLogger("vco.runner")


def _command(c: Collector | Export) -> list[str]:
    if c.language == "python":
        return [sys.executable, str(c.script)]
    return ["pwsh", "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(c.script)]


def _env(cfg: Config, mod: Module, c: Collector) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("VCO_")}
    env["VCO_MOCK"] = "1" if cfg.mock else "0"
    env["VCO_MODULE"] = mod.name
    env["VCO_SETTINGS"] = json.dumps(mod.settings)
    # Only the connections the collector asked for are passed in.
    for conn_name in c.connections:
        conn = cfg.connections.get(conn_name) or {}
        if conn_name == "vcenter":
            instances = selected_instances(normalize_vcenter(conn), mod.settings)
            pubs = [public_instance(i) for i in instances]
            env["VCO_VCENTERS"] = json.dumps(pubs)
            if pubs:
                for key, value in pubs[0].items():
                    if key in ("id", "label"):
                        continue
                    env[f"VCO_VCENTER_{key.upper()}"] = str(value)
            continue
        for key, value in conn.items():
            if key == "instances" or isinstance(value, (dict, list)):
                continue
            env[f"VCO_{conn_name.upper()}_{key.upper()}"] = str(value)
    return env


def _parse_output(text: str) -> object:
    """Parse the collector's JSON, tolerating stray lines printed before it."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        for line in reversed(text.splitlines()):
            if line.lstrip().startswith("{"):
                return json.loads(line)
        raise


def run_collector(cfg: Config, storage: Storage, mod: Module, c: Collector) -> dict:
    started = utcnow()
    out_fd, out_name = tempfile.mkstemp(prefix=f"vco-{mod.name}-", suffix=".json")
    os.close(out_fd)
    out_file = Path(out_name)
    try:
        env = _env(cfg, mod, c)
        env["VCO_OUTPUT_FILE"] = str(out_file)
        proc = subprocess.run(
            _command(c),
            cwd=mod.path,
            env=env,
            capture_output=True,
            text=True,
            timeout=c.timeout_seconds,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                (proc.stderr.strip() or proc.stdout.strip())[-1500:] or f"exit code {proc.returncode}"
            )
        raw = out_file.read_text() if out_file.stat().st_size else proc.stdout
        try:
            payload = _parse_output(raw)
        except json.JSONDecodeError:
            raise ValueError(
                "collector output is not JSON. First output: "
                + (proc.stdout.strip()[:400] or "(empty)")
                + (" | stderr: " + proc.stderr.strip()[-400:] if proc.stderr.strip() else "")
            ) from None
        if not isinstance(payload, dict):
            raise ValueError("collector must print a JSON object keyed by table name")
        total = 0
        for table in c.tables:
            rows = payload.get(table, [])
            if isinstance(rows, dict):  # single row
                rows = [rows]
            total += storage.write_rows(mod, table, rows)
        storage.record_run(mod.name, c.name, started, "ok", total, proc.stderr.strip()[-500:])
        log.info("%s/%s: %d rows", mod.name, c.name, total)
        return {"status": "ok", "rows": total}
    except Exception as exc:
        storage.record_run(mod.name, c.name, started, "error", 0, str(exc))
        log.error("%s/%s failed: %s", mod.name, c.name, exc)
        return {"status": "error", "message": str(exc)}
    finally:
        out_file.unlink(missing_ok=True)


def run_export(cfg: Config, storage: Storage, mod: Module, e: Export, params: dict[str, str]) -> bytes:
    """Run an export script and return the file it wrote. Raises on failure."""
    out_fd, out_name = tempfile.mkstemp(prefix=f"vco-{mod.name}-{e.name}-")
    os.close(out_fd)
    out_file = Path(out_name)
    try:
        env = {k: v for k, v in os.environ.items() if not k.startswith("VCO_")}
        env.update({
            "VCO_MODULE": mod.name, "VCO_EXPORT": e.name,
            "VCO_SETTINGS": json.dumps(mod.settings), "VCO_PARAMS": json.dumps(params),
            "VCO_DB": str(storage.module_db_path(mod.name).resolve()),
            "VCO_OUTPUT_FILE": str(out_file),
        })
        proc = subprocess.run(_command(e), cwd=mod.path, env=env, capture_output=True,
                              text=True, timeout=e.timeout_seconds)
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr.strip() or proc.stdout.strip())[-1500:] or f"exit code {proc.returncode}")
        return out_file.read_bytes()
    finally:
        out_file.unlink(missing_ok=True)
