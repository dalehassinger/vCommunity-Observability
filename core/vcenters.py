"""Multi-vCenter helpers: normalize config instances and resolve what collectors get."""

from __future__ import annotations

import re
from typing import Any

from .connections import STANDARD_FIELDS, as_bool

ID_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,31}$")


def valid_id(value: str) -> bool:
    return bool(ID_RE.match(value or ""))


def _instance_from_flat(conn: dict[str, Any], default_id: str = "default") -> dict[str, Any]:
    inst = {k: conn.get(k, "") for k in STANDARD_FIELDS}
    inst["id"] = str(conn.get("id") or default_id)
    inst["label"] = str(conn.get("label") or inst.get("host") or inst["id"])
    inst["enabled"] = as_bool(conn.get("enabled", True))
    return inst


def normalize_vcenter(conn: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Turn a config `vcenter` block (flat or instances[]) into a list of instances."""
    conn = dict(conn or {})
    raw = conn.get("instances")
    if isinstance(raw, list) and raw:
        out = []
        for i, item in enumerate(raw):
            if not isinstance(item, dict):
                continue
            inst = _instance_from_flat(item, default_id="default" if i == 0 else f"vc{i + 1}")
            if not valid_id(inst["id"]):
                inst["id"] = "default" if i == 0 else f"vc{i + 1}"
            out.append(inst)
        return out or [_instance_from_flat({"id": "default", "label": "vCenter"})]
    if any(conn.get(k) not in (None, "") for k in STANDARD_FIELDS) or conn.get("id") or conn.get("label"):
        return [_instance_from_flat(conn)]
    return [_instance_from_flat({"id": "default", "label": "vCenter"})]


def mirror_primary(instances: list[dict[str, Any]]) -> dict[str, Any]:
    """Connection dict with flat primary fields plus `instances` (for cfg.connections['vcenter'])."""
    primary = instances[0] if instances else _instance_from_flat({"id": "default", "label": "vCenter"})
    out = {k: primary.get(k, "") for k in STANDARD_FIELDS}
    out["instances"] = [dict(i) for i in instances]
    return out


def selected_instances(instances: list[dict[str, Any]], settings: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Enabled instances, optionally filtered by module settings `vcenters: [id, ...]`."""
    enabled = [i for i in instances if i.get("enabled", True)]
    wanted = (settings or {}).get("vcenters")
    if not wanted:
        return enabled
    if isinstance(wanted, str):
        wanted = [w.strip() for w in wanted.split(",") if w.strip()]
    ids = {str(w) for w in wanted}
    picked = [i for i in enabled if i["id"] in ids]
    return picked or enabled


def public_instance(inst: dict[str, Any]) -> dict[str, str]:
    """Fields passed to collectors (includes password)."""
    return {
        "id": str(inst.get("id") or "default"),
        "label": str(inst.get("label") or inst.get("host") or inst.get("id") or "vCenter"),
        "host": str(inst.get("host") or ""),
        "user": str(inst.get("user") or ""),
        "password": str(inst.get("password") or ""),
        "verify_ssl": "true" if as_bool(inst.get("verify_ssl", False)) else "false",
    }
