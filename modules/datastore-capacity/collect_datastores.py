"""Collect datastore capacity from vCenter with pyVmomi.

Follows the vCommunity-Observability collector contract: connection details come
in as VCO_VCENTER_* environment variables, one JSON object keyed by table name is
printed to stdout, and logs go to stderr.
"""

import json
import os
import sys

GB = 1024 ** 3


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def sample_rows() -> list[dict]:
    """Built-in sample data for VCO_MOCK=1 (demos and tests without a lab)."""
    import time
    drift = (time.time() / 3600) % 24  # grows a little each hour so runway has a trend
    data = [
        ("vsanDatastore-wld01", "dc-wld01", "vsan", 16384, 6100 - drift * 4, 58, 21000),
        ("vsanDatastore-mgmt", "dc-mgmt", "vsan", 8192, 1200 - drift * 6, 41, 11800),
        ("nfs-iso-library", "dc-mgmt", "NFS", 2048, 1650, 0, 400),
        ("vmfs-tier1-01", "dc-wld01", "VMFS", 4096, 380 - drift * 2, 37, 5200),
        ("vmfs-tier2-01", "dc-wld01", "VMFS", 4096, 2300, 22, 2600),
    ]
    rows = []
    for name, dc, typ, cap, free, vms, prov in data:
        rows.append(row(name, dc, typ, cap * GB, free * GB, (cap - free) * GB + max(prov - cap, 0) * GB, vms, True))
    return rows


def row(name, dc, typ, capacity, free, provisioned, vm_count, accessible) -> dict:
    cap_gb = capacity / GB
    free_gb = free / GB
    used_gb = cap_gb - free_gb
    prov_gb = provisioned / GB
    return {
        "name": name, "datacenter": dc, "type": typ,
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


def live_rows() -> list[dict]:
    from pyVim.connect import Disconnect, SmartConnect
    from pyVmomi import vim

    host = os.environ.get("VCO_VCENTER_HOST")
    if not host:
        raise SystemExit("VCO_VCENTER_HOST is not set; configure the vcenter connection in config.yaml")
    if not os.environ.get("VCO_VCENTER_PASSWORD"):
        raise SystemExit("vCenter password is empty: set VCO_VCENTER_PASSWORD in .env and make sure "
                         "config.yaml has password_env: VCO_VCENTER_PASSWORD")
    verify = os.environ.get("VCO_VCENTER_VERIFY_SSL", "false").lower() == "true"
    si = SmartConnect(
        host=host,
        user=os.environ.get("VCO_VCENTER_USER", ""),
        pwd=os.environ.get("VCO_VCENTER_PASSWORD", ""),
        disableSslCertValidation=not verify,
    )
    try:
        content = si.RetrieveContent()
        view = content.viewManager.CreateContainerView(content.rootFolder, [vim.Datastore], True)
        rows = []
        for ds in view.view:
            s = ds.summary
            provisioned = s.capacity - s.freeSpace + (s.uncommitted or 0)
            rows.append(row(s.name, datacenter_of(ds), s.type, s.capacity, s.freeSpace,
                            provisioned, len(ds.vm), s.accessible))
        view.Destroy()
        log(f"collected {len(rows)} datastores from {host}")
        return rows
    finally:
        Disconnect(si)


if __name__ == "__main__":
    rows = sample_rows() if os.environ.get("VCO_MOCK") == "1" else live_rows()
    # Same rows feed the current-state table and the history table.
    payload = json.dumps({"datastores": rows, "datastore_history": rows})
    if os.environ.get("VCO_OUTPUT_FILE"):
        with open(os.environ["VCO_OUTPUT_FILE"], "w") as fh:
            fh.write(payload)
    else:
        print(payload)
