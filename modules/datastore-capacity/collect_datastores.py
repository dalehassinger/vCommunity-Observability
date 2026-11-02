"""Collect datastore capacity from vCenter with pyVmomi.

Follows the vCommunity-Observability collector contract: connection details come
in as VCO_VCENTER_* / VCO_VCENTERS environment variables, one JSON object keyed by
table name is printed to stdout, and logs go to stderr.
"""

import json
import os
import sys

GB = 1024 ** 3


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


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


def sample_rows(vc: dict, index: int) -> list[dict]:
    """Built-in sample data for VCO_MOCK=1 (demos and tests without a lab)."""
    import time
    drift = (time.time() / 3600) % 24  # grows a little each hour so runway has a trend
    suffix = "" if index == 0 else f"-{vc['id']}"
    dc_suffix = "" if index == 0 else f"-{vc['id']}"
    data = [
        (f"vsanDatastore-wld01{suffix}", f"dc-wld01{dc_suffix}", "vsan", 16384, 6100 - drift * 4, 58, 21000),
        (f"vsanDatastore-mgmt{suffix}", f"dc-mgmt{dc_suffix}", "vsan", 8192, 1200 - drift * 6, 41, 11800),
        (f"nfs-iso-library{suffix}", f"dc-mgmt{dc_suffix}", "NFS", 2048, 1650, 0, 400),
        (f"vmfs-tier1-01{suffix}", f"dc-wld01{dc_suffix}", "VMFS", 4096, 380 - drift * 2, 37, 5200),
        (f"vmfs-tier2-01{suffix}", f"dc-wld01{dc_suffix}", "VMFS", 4096, 2300, 22, 2600),
    ]
    rows = []
    for name, dc, typ, cap, free, vms, prov in data:
        rows.append(row(vc["id"], name, dc, typ, cap * GB, free * GB,
                        (cap - free) * GB + max(prov - cap, 0) * GB, vms, True))
    return rows


def row(vcenter, name, dc, typ, capacity, free, provisioned, vm_count, accessible) -> dict:
    cap_gb = capacity / GB
    free_gb = free / GB
    used_gb = cap_gb - free_gb
    prov_gb = provisioned / GB
    return {
        "vcenter": vcenter, "name": name, "datacenter": dc, "type": typ,
        "capacity_gb": round(cap_gb, 1), "free_gb": round(free_gb, 1), "used_gb": round(used_gb, 1),
        "used_pct": round(used_gb / cap_gb * 100, 1) if cap_gb else None,
        "provisioned_gb": round(prov_gb, 1),
        "provisioned_pct": round(prov_gb / cap_gb * 100, 1) if cap_gb else None,
        "vm_count": vm_count, "accessible": 1 if accessible else 0,
    }


def datacenter_of(entity) -> str:
    from pyVmomi import vim
    obj = entity.parent
    while obj is not None and not isinstance(obj, vim.Datacenter):
        obj = obj.parent
    return obj.name if obj is not None else ""


def live_rows(vc: dict) -> list[dict]:
    from pyVim.connect import Disconnect, SmartConnect
    from pyVmomi import vim

    host = vc.get("host") or ""
    if not host:
        raise SystemExit(f"vCenter '{vc.get('id')}' has no host; configure it on the Settings page")
    if not vc.get("password"):
        raise SystemExit(f"vCenter '{vc.get('id')}' password is empty: set it on the Settings page or in .env")
    verify = str(vc.get("verify_ssl", "false")).lower() == "true"
    si = SmartConnect(
        host=host,
        user=vc.get("user", ""),
        pwd=vc.get("password", ""),
        disableSslCertValidation=not verify,
    )
    try:
        content = si.RetrieveContent()
        view = content.viewManager.CreateContainerView(content.rootFolder, [vim.Datastore], True)
        rows = []
        for ds in view.view:
            s = ds.summary
            provisioned = s.capacity - s.freeSpace + (s.uncommitted or 0)
            rows.append(row(vc["id"], s.name, datacenter_of(ds), s.type, s.capacity, s.freeSpace,
                            provisioned, len(ds.vm), s.accessible))
        view.Destroy()
        log(f"collected {len(rows)} datastores from {host} ({vc['id']})")
        return rows
    finally:
        Disconnect(si)


if __name__ == "__main__":
    instances = vcenters()
    if not instances:
        raise SystemExit("No vCenter configured; add one on the Settings page or in config.yaml")
    mock = os.environ.get("VCO_MOCK") == "1"
    rows: list[dict] = []
    for i, vc in enumerate(instances):
        rows.extend(sample_rows(vc, i) if mock else live_rows(vc))
    # Same rows feed the current-state table and the history table.
    payload = json.dumps({"datastores": rows, "datastore_history": rows})
    if os.environ.get("VCO_OUTPUT_FILE"):
        with open(os.environ["VCO_OUTPUT_FILE"], "w") as fh:
            fh.write(payload)
    else:
        print(payload)
