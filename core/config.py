"""Load config.yaml. Secrets are never stored in the file: entries ending in
`_env` name an environment variable that holds the real value."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .vcenters import mirror_primary, normalize_vcenter

log = logging.getLogger("vco.config")
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _resolve_env(section: dict[str, Any]) -> dict[str, Any]:
    """Turn {"password_env": "VCO_X"} into {"password": os.environ["VCO_X"]}."""
    out: dict[str, Any] = {}
    for key, value in (section or {}).items():
        if key == "instances" and isinstance(value, list):
            out[key] = [_resolve_env(item) if isinstance(item, dict) else item for item in value]
            continue
        if key.endswith("_env") and isinstance(value, str):
            name = key[: -len("_env")]
            if not ENV_NAME.match(value):
                # Common mistake: the secret itself was pasted here instead of
                # the NAME of the environment variable that holds it.
                log.error("config: '%s' must name an environment variable (e.g. VCO_VCENTER_PASSWORD), "
                          "not hold the secret itself. Put the secret in .env.", key)
                out[name] = ""
                continue
            out[name] = os.environ.get(value, "")
            if not out[name]:
                log.warning("config: environment variable %s (for '%s') is not set or empty", value, key)
        else:
            out[key] = value
    return out


DEFAULT_HOME = "/dash/dbh-insights/insights"


@dataclass
class Config:
    data_dir: Path
    modules_dirs: list[Path]
    mock: bool
    connections: dict[str, dict[str, Any]]
    api_tokens: list[str]
    modules: dict[str, dict[str, Any]] = field(default_factory=dict)
    # The page "/" opens. A dashboard (/dash/<module>/<slug>) that is missing or switched
    # off falls back to the Overview page (all dashboards as tiles).
    home: str = DEFAULT_HOME


def load_config(path: str | os.PathLike | None = None) -> Config:
    path = Path(path or os.environ.get("VCO_CONFIG", "config/config.yaml"))
    raw: dict[str, Any] = {}
    if path.exists():
        raw = yaml.safe_load(path.read_text()) or {}

    connections = {
        name: _resolve_env(conn) for name, conn in (raw.get("connections") or {}).items()
    }
    if "vcenter" in connections:
        connections["vcenter"] = mirror_primary(normalize_vcenter(connections["vcenter"]))

    tokens: list[str] = []
    for entry in (raw.get("api") or {}).get("tokens", []) or []:
        resolved = _resolve_env(entry)
        if resolved.get("token"):
            tokens.append(resolved["token"])

    mock_env = os.environ.get("VCO_MOCK")
    mock = (mock_env == "1") if mock_env is not None else bool(raw.get("mock", False))

    return Config(
        data_dir=Path(os.environ.get("VCO_DATA_DIR", raw.get("data_dir", "data"))),
        modules_dirs=[Path(p) for p in raw.get("modules_dirs", ["modules"])],
        mock=mock,
        connections=connections,
        api_tokens=tokens,
        modules=raw.get("modules") or {},
        home=str(os.environ.get("VCO_HOME") or raw.get("home") or DEFAULT_HOME).strip(),
    )
