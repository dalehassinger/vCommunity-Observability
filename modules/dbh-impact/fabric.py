"""The vSphere "fabric" behind DBH Insights' Topology and Impact views.

Python port of DBH Insights' collect/js/insights.js (fetchTopology) and
collect/js/impact.js (fetchFabric): hosts, clusters, datastores, which host mounts
which datastore, and (for Impact) the VMs that tie hosts and datastores together.

This file is shared by the dbh-topology and dbh-impact modules. Both keep an
identical copy so each module stays self-contained; scripts/smoke_test.py checks
the two copies match.

Output tables (one JSON object keyed by table name, the collector contract):
  fabric_source        server, datacenters
  fabric_clusters      moref, name, ha_enabled, drs_enabled
  fabric_hosts         moref, name, cluster_moref, cluster, connection_state, in_maintenance,
                       cores, memory_gib (incl. memory tiers), dram_gib
  fabric_datastores    moref, name, kind, capacity_gib, free_gib, used_gib, used_pct,
                       vm_count, mount_count, accessible
  fabric_mounts        datastore_moref, host_moref
  fabric_vms           moref, name, power_state, template, vcpu, memory_gib, host_moref   (with_vms)
  fabric_vm_datastores vm_moref, datastore_moref                                          (with_vms)
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from typing import Any

GIB = 1024 ** 3

TABLES = ["fabric_source", "fabric_clusters", "fabric_hosts", "fabric_datastores", "fabric_mounts"]
VM_TABLES = ["fabric_vms", "fabric_vm_datastores"]

HOST_PROPS = ["name", "runtime.connectionState", "runtime.inMaintenanceMode",
              "summary.hardware.numCpuCores", "summary.hardware.memorySize", "hardware.memoryTierInfo"]
# HA and DRS flags: the website reads them from the REST API; here they come from the
# same SOAP session. A vCenter that refuses the property leaves them unknown (NULL).
CLUSTER_PROPS = ["name", "host", "configuration.dasConfig.enabled", "configuration.drsConfig.enabled"]
DATASTORE_PROPS = ["name", "summary.type", "summary.capacity", "summary.freeSpace", "summary.accessible", "host", "vm"]
VM_PROPS = ["name", "runtime.powerState", "config.template", "config.hardware.numCPU",
            "config.hardware.memoryMB", "runtime.host"]


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def round2(v: float) -> float:
    # JavaScript Math.round(v * 100) / 100, as the website rounds.
    return math.floor(v * 100 + 0.5) / 100


def gib(v: Any) -> float | None:
    return None if v is None else round2(float(v) / GIB)


def moid(value: Any) -> str | None:
    """Managed object -> its id. Never compare a pyVmomi object with ==."""
    if value is None:
        return None
    mo = getattr(value, "_moId", None)
    if mo:
        return mo
    return value if isinstance(value, str) and value else None


def field(obj: Any, name: str) -> Any:
    """A field of a pyVmomi data object, or of the plain dicts the sample data uses."""
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)


def flag(value: Any) -> int | None:
    return None if value is None else int(bool(value))


# --------------------------------------------------------------------------- building the tables

MOREF_FIELDS = {
    "fabric_clusters": ["moref"],
    "fabric_hosts": ["moref", "cluster_moref"],
    "fabric_datastores": ["moref"],
    "fabric_mounts": ["datastore_moref", "host_moref"],
    "fabric_vms": ["moref", "host_moref"],
    "fabric_vm_datastores": ["vm_moref", "datastore_moref"],
}


def stamp_vcenter(data: dict[str, list[dict]], vc_id: str, namespace_morefs: bool) -> dict[str, list[dict]]:
    """Tag every row with the connection id; optionally prefix morefs so they stay unique across vCenters."""
    out: dict[str, list[dict]] = {}
    for table, rows in data.items():
        stamped = []
        for row in rows:
            r = dict(row)
            r["vcenter"] = vc_id
            if namespace_morefs:
                for key in MOREF_FIELDS.get(table, []):
                    if r.get(key):
                        r[key] = f"{vc_id}:{r[key]}"
            stamped.append(r)
        out[table] = stamped
    return out


def build(server: str, datacenters: list[str], hosts: list[dict], clusters: list[dict],
          datastores: list[dict], vms: list[dict] | None) -> dict[str, list[dict]]:
    """Join raw objects (property name -> value dicts with a 'moref') into the tables."""
    host_rows: dict[str, dict] = {}
    for h in hosts:
        name = h.get("name")
        if not name:
            raise RuntimeError(f"HostSystem {h['moref']} returned no name property")
        tiers = h.get("hardware.memoryTierInfo") or []
        # With memory tiering on, memorySize includes the NVMe tier; DRAM is kept alongside.
        dram = next((field(t, "size") for t in tiers if field(t, "type") == "DRAM"), None)
        host_rows[h["moref"]] = {
            "moref": h["moref"], "name": name, "cluster_moref": None, "cluster": None,
            "connection_state": h.get("runtime.connectionState"),
            "in_maintenance": int(bool(h.get("runtime.inMaintenanceMode"))),
            "cores": h.get("summary.hardware.numCpuCores"),
            "memory_gib": gib(h.get("summary.hardware.memorySize")),
            "dram_gib": gib(dram),
        }

    cluster_rows = []
    for c in clusters:
        name = c.get("name") or c["moref"]
        # Membership comes from each cluster's own host list.
        for ref in c.get("host") or []:
            row = host_rows.get(moid(ref))
            if row:
                row["cluster_moref"], row["cluster"] = c["moref"], name
        cluster_rows.append({"moref": c["moref"], "name": name,
                             "ha_enabled": flag(c.get("configuration.dasConfig.enabled")),
                             "drs_enabled": flag(c.get("configuration.drsConfig.enabled"))})

    vm_rows: dict[str, dict] = {}
    if vms is not None:
        for v in vms:
            name = v.get("name")
            # vCLS VMs are vSphere's own agents; excluded as on every DBH sheet.
            if not name or name.startswith("vCLS-"):
                continue
            host = moid(v.get("runtime.host"))
            vm_rows[v["moref"]] = {
                "moref": v["moref"], "name": name, "power_state": v.get("runtime.powerState"),
                "template": int(bool(v.get("config.template"))),
                "vcpu": v.get("config.hardware.numCPU") or 0,
                "memory_gib": round2((v.get("config.hardware.memoryMB") or 0) / 1024),
                "host_moref": host if host in host_rows else None,
            }

    ds_rows, mounts, vm_ds = [], [], []
    for ds in datastores:
        name = ds.get("name")
        if not name:
            raise RuntimeError(f"Datastore {ds['moref']} returned no name property")
        cap, free = gib(ds.get("summary.capacity")), gib(ds.get("summary.freeSpace"))
        used = round2(cap - free) if cap is not None and free is not None else None
        hosts_here = []
        for m in ds.get("host") or []:
            key = moid(field(m, "key"))
            if key:
                hosts_here.append(key)
                if key in host_rows:
                    mounts.append({"datastore_moref": ds["moref"], "host_moref": key})
        vm_refs = [moid(r) for r in ds.get("vm") or []]
        if vms is not None:
            # A VM is listed on every datastore holding any of its files, so split VMs show up.
            vm_ds += [{"vm_moref": r, "datastore_moref": ds["moref"]} for r in vm_refs if r in vm_rows]
        ds_rows.append({
            "moref": ds["moref"], "name": name, "kind": ds.get("summary.type"),
            "capacity_gib": cap, "free_gib": free, "used_gib": used,
            "used_pct": round(used / cap * 100, 1) if cap and used is not None else None,
            "vm_count": len(vm_refs), "mount_count": len(hosts_here),
            "accessible": flag(ds.get("summary.accessible")),
        })
    ds_rows.sort(key=lambda d: d["name"])

    out = {
        "fabric_source": [{"server": server, "datacenters": ", ".join(d for d in datacenters if d)}],
        "fabric_clusters": sorted(cluster_rows, key=lambda c: c["name"]),
        "fabric_hosts": sorted(host_rows.values(), key=lambda h: h["name"]),
        "fabric_datastores": ds_rows,
        "fabric_mounts": mounts,
    }
    if vms is not None:
        out["fabric_vms"] = sorted(vm_rows.values(), key=lambda v: v["name"].lower())
        out["fabric_vm_datastores"] = vm_ds
    return out


# --------------------------------------------------------------------------- live vCenter

class LiveSource:
    def __init__(self, host: str, user: str, password: str, verify_ssl: bool):
        from pyVim.connect import SmartConnect
        from pyVmomi import vim, vmodl
        self.vim, self.vmodl = vim, vmodl
        self.si = SmartConnect(host=host, user=user, pwd=password, disableSslCertValidation=not verify_ssl)
        self.content = self.si.RetrieveContent()

    def close(self) -> None:
        from pyVim.connect import Disconnect
        Disconnect(self.si)

    def retrieve(self, kind: Any, props: list[str]) -> list[dict]:
        """Every object of one type via one PropertyCollector call, following the
        continuation token. An unknown property (older vCenter) is dropped, not fatal."""
        vim, vmodl = self.vim, self.vmodl
        pc = self.content.propertyCollector
        paths = list(props)
        view = self.content.viewManager.CreateContainerView(self.content.rootFolder, [kind], True)
        try:
            while True:
                spec = vmodl.query.PropertyCollector.FilterSpec(
                    objectSet=[vmodl.query.PropertyCollector.ObjectSpec(
                        obj=view, skip=True,
                        selectSet=[vmodl.query.PropertyCollector.TraversalSpec(
                            name="view", type=vim.view.ContainerView, path="view", skip=False)])],
                    propSet=[vmodl.query.PropertyCollector.PropertySpec(type=kind, pathSet=paths)])
                try:
                    result = pc.RetrievePropertiesEx(specSet=[spec], options=vmodl.query.PropertyCollector.RetrieveOptions())
                    objects = []
                    while result is not None:
                        for oc in result.objects or []:
                            objects.append({"moref": oc.obj._moId, **{p.name: p.val for p in (oc.propSet or [])}})
                        if not result.token:
                            break
                        result = pc.ContinueRetrievePropertiesEx(token=result.token)
                    return objects
                except vmodl.query.InvalidProperty as exc:
                    bad = getattr(exc, "name", None)
                    if not bad or bad not in paths:
                        raise
                    log(f"{kind.__name__}: this vCenter has no property {bad}; it stays empty")
                    paths.remove(bad)
        finally:
            try:
                view.Destroy()
            except Exception:
                pass

    def fabric(self, server: str, with_vms: bool) -> dict[str, list[dict]]:
        vim = self.vim
        return build(
            server,
            [d.get("name") for d in self.retrieve(vim.Datacenter, ["name"])],
            self.retrieve(vim.HostSystem, HOST_PROPS),
            self.retrieve(vim.ClusterComputeResource, CLUSTER_PROPS),
            self.retrieve(vim.Datastore, DATASTORE_PROPS),
            self.retrieve(vim.VirtualMachine, VM_PROPS) if with_vms else None,
        )


# --------------------------------------------------------------------------- sample data (VCO_MOCK=1)

def sample(with_vms: bool, suffix: str = "") -> dict[str, list[dict]]:
    """A small lab like the DBH Insights screenshots: CL-01 is one host with HA off and a
    local datastore only it can reach; CL-02 has two hosts on vSAN with HA on; esx9-04
    is standalone. `suffix` distinguishes a second mock vCenter (empty = primary lab)."""
    T = 1024 ** 4
    # Primary lab keeps exact original names so existing smoke assertions stay stable.
    s = suffix  # "" or "-lab"
    hosts = [
        {"moref": "host-11", "name": f"esx9-01.vcrocs.local{s}", "runtime.connectionState": "connected",
         "runtime.inMaintenanceMode": False, "summary.hardware.numCpuCores": 12,
         "summary.hardware.memorySize": 63.7 * GIB, "hardware.memoryTierInfo": []},
        {"moref": "host-12", "name": f"esx9-02.vcrocs.local{s}", "runtime.connectionState": "connected",
         "runtime.inMaintenanceMode": False, "summary.hardware.numCpuCores": 16,
         "summary.hardware.memorySize": 384 * GIB,
         "hardware.memoryTierInfo": [{"type": "DRAM", "size": 128 * GIB}, {"type": "NVMe", "size": 256 * GIB}]},
        {"moref": "host-13", "name": f"esx9-03.vcrocs.local{s}", "runtime.connectionState": "connected",
         "runtime.inMaintenanceMode": False, "summary.hardware.numCpuCores": 16,
         "summary.hardware.memorySize": 128 * GIB, "hardware.memoryTierInfo": []},
        {"moref": "host-14", "name": f"esx9-04.vcrocs.local{s}", "runtime.connectionState": "connected",
         "runtime.inMaintenanceMode": True, "summary.hardware.numCpuCores": 8,
         "summary.hardware.memorySize": 64 * GIB, "hardware.memoryTierInfo": []},
    ]
    clusters = [
        {"moref": "domain-c8", "name": f"CL-01{s}", "host": ["host-11"],
         "configuration.dasConfig.enabled": False, "configuration.drsConfig.enabled": False},
        {"moref": "domain-c9", "name": f"CL-02{s}", "host": ["host-12", "host-13"],
         "configuration.dasConfig.enabled": True, "configuration.drsConfig.enabled": True},
    ]
    every = ["host-11", "host-12", "host-13", "host-14"]
    ds = [
        ("datastore-21", f"ESX9-01-2TB{s}", "VMFS", 1.82 * T, 1.82 * T - 349.1 * GIB, ["host-11"]),
        ("datastore-22", f"ESX9-04-local{s}", "VMFS", 0.9 * T, 0.7 * T, ["host-14"]),
        ("datastore-23", f"SYN-HDD{s}", "NFS41", 16 * T, 6.4 * T, every),
        ("datastore-24", f"SYN-SSD-04{s}", "NFS41", 3.6 * T, 1.1 * T, every),
        ("datastore-25", f"SYN-SSD-05{s}", "NFS41", 3.6 * T, 2.9 * T, every),
        ("datastore-26", f"vsanDatastore{s}", "vsan", 7.3 * T, 3.0 * T, ["host-12", "host-13"]),
    ]
    # VM inventory for the primary lab; secondary labs get a compact rename.
    if not s:
        vm_def = [
            ("collector", "host-11", "poweredOn", 4, 16, ["SYN-HDD"]),
            ("DC-02", "host-11", "poweredOn", 4, 4, ["SYN-HDD"]),
            ("lic91", "host-11", "poweredOn", 2, 4, ["SYN-HDD"]),
            ("minion01", "host-11", "poweredOn", 1, 2, ["SYN-SSD-04"]),
            ("minion02", "host-11", "poweredOn", 1, 2, ["SYN-SSD-04"]),
            ("offDepot", "host-11", "poweredOn", 2, 4, ["SYN-HDD"]),
            ("OPS91", "host-11", "poweredOn", 4, 16, ["SYN-HDD"]),
            ("SaltMaster", "host-11", "poweredOn", 2, 8, ["SYN-SSD-05"]),
            ("TWINGATE", "host-11", "poweredOn", 1, 2, ["SYN-HDD"]),
            ("UniFi", "host-11", "poweredOn", 4, 8, ["SYN-HDD"]),
            ("vcf91installer", "host-11", "poweredOn", 4, 16, ["SYN-SSD-05"]),
            ("vcf91sddc", "host-11", "poweredOn", 4, 16, ["SYN-HDD"]),
            ("veeam-01", "host-11", "poweredOn", 2, 8, ["SYN-SSD-04", "ESX9-01-2TB"]),
            ("MPB2", "host-11", "poweredOff", 2, 8, ["SYN-SSD-05"]),
            ("POWERSHELL-HOST", "host-11", "poweredOff", 1, 2, ["SYN-HDD"]),
            ("ROCKY-Postgres", "host-11", "poweredOff", 2, 2, ["SYN-HDD"]),
            ("vcsa91", "host-12", "poweredOn", 4, 21, ["vsanDatastore"]),
            ("nsx02", "host-12", "poweredOn", 6, 24, ["vsanDatastore"]),
            ("vsp01-b2hr2", "host-12", "poweredOn", 4, 16, ["vsanDatastore"]),
            ("vsp01-c2td7", "host-13", "poweredOn", 4, 16, ["vsanDatastore"]),
            ("vsp01-lhkgg", "host-13", "poweredOn", 4, 16, ["vsanDatastore", "SYN-SSD-04"]),
            ("DC-03", "host-13", "poweredOn", 2, 4, ["SYN-HDD"]),
            ("TWINGATE-02", "host-13", "poweredOn", 1, 2, ["SYN-HDD"]),
            ("vCLS-1a2b3c", "host-12", "poweredOn", 1, 0.125, ["vsanDatastore"]),
            ("ubuntu-template", "host-13", "poweredOff", 2, 4, ["SYN-HDD"]),
            ("lab-scratch", "host-14", "poweredOff", 2, 4, ["ESX9-04-local"]),
        ]
    else:
        vm_def = [
            (f"collector{s}", "host-11", "poweredOn", 4, 16, [f"SYN-HDD{s}"]),
            (f"app{s}-01", "host-11", "poweredOn", 2, 8, [f"SYN-SSD-04{s}"]),
            (f"vcsa{s}", "host-12", "poweredOn", 4, 21, [f"vsanDatastore{s}"]),
            (f"edge{s}", "host-13", "poweredOn", 2, 4, [f"SYN-HDD{s}"]),
            (f"vCLS{s}", "host-12", "poweredOn", 1, 0.125, [f"vsanDatastore{s}"]),
            (f"scratch{s}", "host-14", "poweredOff", 2, 4, [f"ESX9-04-local{s}"]),
        ]
    vms = []
    for i, (name, host, power, vcpu, mem, dss) in enumerate(vm_def):
        vms.append({"moref": f"vm-{100 + i}", "name": name, "runtime.powerState": power,
                    "config.template": name.endswith("-template"), "config.hardware.numCPU": vcpu,
                    "config.hardware.memoryMB": mem * 1024, "runtime.host": host, "_ds": dss})
    datastores = []
    for moref, name, kind, cap, free, mounted in ds:
        datastores.append({"moref": moref, "name": name, "summary.type": kind, "summary.capacity": cap,
                           "summary.freeSpace": free, "summary.accessible": True,
                           "host": [{"key": h} for h in mounted],
                           "vm": [v["moref"] for v in vms if name in v["_ds"]]})
    server = f"vcsa91.vcrocs.local{s}" if s else "vcsa91.vcrocs.local"
    return build(server, [f"DC-01{s}" if s else "DC-01"], hosts, clusters, datastores,
                 vms if with_vms else None)


def _vcenters() -> list[dict]:
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


def _merge(parts: list[dict[str, list[dict]]]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for part in parts:
        for table, rows in part.items():
            out.setdefault(table, []).extend(rows)
    return out


# --------------------------------------------------------------------------- collector entry point

def main(with_vms: bool, extend=None) -> None:
    """Collector contract: VCO_VCENTER_* / VCO_VCENTERS in, JSON keyed by table out.
    `extend(tables)` may add more tables (dbh-impact adds its answers)."""
    settings = json.loads(os.environ.get("VCO_SETTINGS") or "{}")
    instances = _vcenters()
    if not instances:
        raise SystemExit("No vCenter configured; add one on the Settings page or in config.yaml")
    started = time.monotonic()
    mock = os.environ.get("VCO_MOCK") == "1"
    multi = len(instances) > 1
    parts = []
    for i, vc in enumerate(instances):
        if mock:
            data = sample(with_vms, suffix="" if i == 0 else f"-{vc['id']}")
            if i == 0 and settings.get("vcenter_name") and not multi:
                data["fabric_source"][0]["server"] = settings["vcenter_name"]
            elif multi or not settings.get("vcenter_name"):
                data["fabric_source"][0]["server"] = vc.get("label") or vc.get("host") or vc["id"]
        else:
            host = vc.get("host") or ""
            if not host:
                raise SystemExit(f"vCenter '{vc.get('id')}' has no host; configure it on the Settings page")
            if not vc.get("password"):
                raise SystemExit(f"vCenter '{vc.get('id')}' password is empty: set it on the Settings page or in .env")
            source = LiveSource(host, vc.get("user", ""), vc["password"],
                                str(vc.get("verify_ssl", "false")).lower() == "true")
            try:
                server = (settings.get("vcenter_name") if not multi else None) or vc.get("label") or host
                data = source.fabric(server, with_vms)
            finally:
                source.close()
        parts.append(stamp_vcenter(data, vc["id"], namespace_morefs=multi))
    data = _merge(parts)
    if extend:
        data.update(extend(data))
        # Impact rows inherit vcenter from the moref prefix when multi; stamp when missing.
        if "impact" in data:
            for row in data["impact"]:
                if "vcenter" not in row:
                    moref = row.get("moref") or ""
                    row["vcenter"] = moref.split(":", 1)[0] if multi and ":" in moref else instances[0]["id"]
    log(f"{len(data['fabric_hosts'])} hosts, {len(data['fabric_datastores'])} datastores, "
        f"{len(data['fabric_mounts'])} mounts" + (f", {len(data['fabric_vms'])} VMs" if with_vms else "")
        + f" across {len(instances)} vCenter(s) in {time.monotonic() - started:.1f}s")
    payload = json.dumps(data, default=str)
    if os.environ.get("VCO_OUTPUT_FILE"):
        with open(os.environ["VCO_OUTPUT_FILE"], "w") as fh:
            fh.write(payload)
    else:
        print(payload)
