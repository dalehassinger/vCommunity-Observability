"""DBH Insights collector for vCommunity-Observability.

Builds every DBH Insights inventory sheet from one or more vCenters and writes
one table per sheet, plus `sheets` (status per sheet) and `sheet_columns` (the
original column labels, kinds and totals rules, used by the pages and the XLSX export).

Contract: VCO_VCENTER_* / VCO_VCENTERS connection details in, one JSON object keyed
by table name out to VCO_OUTPUT_FILE, logs to stderr. VCO_MOCK=1 uses sample data.
"""

import json
import os
import sys
import time

from sheets import VcenterData, collect
from vsphere import log


def vcenters() -> list[dict]:
    raw = os.environ.get("VCO_VCENTERS")
    if raw:
        try:
            items = json.loads(raw)
            if isinstance(items, list) and items:
                return items
        except json.JSONDecodeError:
            pass
    host = os.environ.get("VCO_VCENTER_HOST", "")
    if not host and os.environ.get("VCO_MOCK") != "1":
        return []
    return [{
        "id": "default", "label": host or "vCenter", "host": host,
        "user": os.environ.get("VCO_VCENTER_USER", ""),
        "password": os.environ.get("VCO_VCENTER_PASSWORD", ""),
        "verify_ssl": os.environ.get("VCO_VCENTER_VERIFY_SSL", "false"),
    }]


def collect_one(vc: dict, settings: dict, multi: bool) -> dict:
    host = vc.get("host") or ""
    user = vc.get("user") or ""
    if multi:
        server = vc.get("label") or host or vc["id"]
    else:
        server = settings.get("vcenter_name") or host or "vCenter"

    if os.environ.get("VCO_MOCK") == "1":
        from sample_data import SampleSource
        # Distinct sample labs per connection id so multi-VC mock rows do not collide.
        sample_host = host or ("vcsa91.vcrocs.local" if vc["id"] == "default" else f"vcsa-{vc['id']}.lab.local")
        source = SampleSource(server=sample_host)
        server = settings.get("vcenter_name") if not multi else None
        server = server or vc.get("label") or sample_host
        user = user or "administrator@vcrocs.local"
    else:
        if not host:
            raise SystemExit(f"vCenter '{vc.get('id')}' has no host; configure it on the Settings page")
        if not vc.get("password"):
            raise SystemExit(f"vCenter '{vc.get('id')}' password is empty: set it on the Settings page or in .env")
        from vsphere import LiveSource
        verify = str(vc.get("verify_ssl", "false")).lower() == "true"
        source = LiveSource(host, user, vc["password"], verify)

    try:
        data = collect(VcenterData(source, server, user))
    finally:
        if hasattr(source, "close"):
            source.close()

    for row in data.get("sheets") or []:
        row["vcenter"] = vc["id"]
    return data


def merge(parts: list[dict]) -> dict:
    if not parts:
        return {}
    out: dict = {"sheets": [], "sheet_columns": parts[0].get("sheet_columns") or []}
    skip = {"sheets", "sheet_columns"}
    for part in parts:
        out["sheets"].extend(part.get("sheets") or [])
        for key, rows in part.items():
            if key in skip:
                continue
            out.setdefault(key, []).extend(rows or [])
    return out


def main() -> None:
    settings = json.loads(os.environ.get("VCO_SETTINGS") or "{}")
    instances = vcenters()
    if not instances:
        raise SystemExit("No vCenter configured; add one on the Settings page or in config.yaml")

    started = time.monotonic()
    multi = len(instances) > 1
    parts = []
    for vc in instances:
        part = collect_one(vc, settings, multi)
        failed = [s for s in part["sheets"] if s["error"]]
        for s in failed:
            log(f"[{vc['id']}] sheet {s['sheet']} failed: {s['error']}")
        log(f"[{vc['id']}] {len(part['sheets']) - len(failed)} of {len(part['sheets'])} sheets "
            f"in {time.monotonic() - started:.1f}s")
        if len(failed) == len(part["sheets"]):
            raise SystemExit(f"every sheet failed on {vc['id']}; first error: {failed[0]['error']}")
        parts.append(part)

    data = merge(parts)
    log(f"merged {len(instances)} vCenter(s) in {time.monotonic() - started:.1f}s")
    payload = json.dumps(data, default=str)
    if os.environ.get("VCO_OUTPUT_FILE"):
        with open(os.environ["VCO_OUTPUT_FILE"], "w") as fh:
            fh.write(payload)
    else:
        print(payload)


if __name__ == "__main__":
    sys.exit(main())
