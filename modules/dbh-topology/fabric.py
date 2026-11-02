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

def sample(with_vms: bool) -> dict[str, list[dict]]:
    """A small lab like the DBH Insights screenshots: CL-01 is one host with HA off and a
    local datastore only it can reach; CL-02 has two hosts on vSAN with HA on; esx9-04
    is standalone."""
    T = 1024 ** 4
    hosts = [
        {"moref": "host-11", "name": "esx9-01.vcrocs.local", "runtime.connectionState": "connected",
         "runtime.inMaintenanceMode": False, "summary.hardware.numCpuCores": 12,
         "summary.hardware.memorySize": 63.7 * GIB, "hardware.memoryTierInfo": []},
        {"moref": "host-12", "name": "esx9-02.vcrocs.local", "runtime.connectionState": "connected",
         "runtime.inMaintenanceMode": False, "summary.hardware.numCpuCores": 16,
         "summary.hardware.memorySize": 384 * GIB,
         "hardware.memoryTierInfo": [{"type": "DRAM", "size": 128 * GIB}, {"type": "NVMe", "size": 256 * GIB}]},
        {"moref": "host-13", "name": "esx9-03.vcrocs.local", "runtime.connectionState": "connected",
         "runtime.inMaintenanceMode": False, "summary.hardware.numCpuCores": 16,
         "summary.hardware.memorySize": 128 * GIB, "hardware.memoryTierInfo": []},
        {"moref": "host-14", "name": "esx9-04.vcrocs.local", "runtime.connectionState": "connected",
         "runtime.inMaintenanceMode": True, "summary.hardware.numCpuCores": 8,
         "summary.hardware.memorySize": 64 * GIB, "hardware.memoryTierInfo": []},
    ]
    clusters = [
        {"moref": "domain-c8", "name": "CL-01", "host": ["host-11"],
         "configuration.dasConfig.enabled": False, "configuration.drsConfig.enabled": False},
        {"moref": "domain-c9", "name": "CL-02", "host": ["host-12", "host-13"],
         "configuration.dasConfig.enabled": True, "configuration.drsConfig.enabled": True},
    ]
    every = ["host-11", "host-12", "host-13", "host-14"]
    ds_def = [
        # moref, name, type, capacity, free, mounted by
        ("datastore-21", "ESX9-01-2TB", "VMFS", 1.82 * T, 1.82 * T - 349.1 * GIB, ["host-11"]),
        ("datastore-22", "ESX9-04-local", "VMFS", 0.9 * T, 0.7 * T, ["host-14"]),
        ("datastore-23", "SYN-HDD", "NFS41", 16 * T, 6.4 * T, every),
        ("datastore-24", "SYN-SSD-04", "NFS41", 3.6 * T, 1.1 * T, every),
        ("datastore-25", "SYN-SSD-05", "NFS41", 3.6 * T, 2.9 * T, every),
        ("datastore-26", "vsanDatastore", "vsan", 7.3 * T, 3.0 * T, ["host-12", "host-13"]),
    ]
    vm_def = [
        # name, host, power, vcpu, memory GiB, datastores
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
    vms = []
    for i, (name, host, power, vcpu, mem, dss) in enumerate(vm_def):
        vms.append({"moref": f"vm-{100 + i}", "name": name, "runtime.powerState": power,
                    "config.template": name.endswith("-template"), "config.hardware.numCPU": vcpu,
                    "config.hardware.memoryMB": mem * 1024, "runtime.host": host, "_ds": dss})
    datastores = []
    for moref, name, kind, cap, free, mounted in ds_def:
        datastores.append({"moref": moref, "name": name, "summary.type": kind, "summary.capacity": cap,
                           "summary.freeSpace": free, "summary.accessible": True,
                           "host": [{"key": h} for h in mounted],
                           "vm": [v["moref"] for v in vms if name in v["_ds"]]})
    return build("vcsa91.vcrocs.local", ["DC-01"], hosts, clusters, datastores, vms if with_vms else None)


# --------------------------------------------------------------------------- collector entry point

def main(with_vms: bool, extend=None) -> None:
    """Collector contract: VCO_VCENTER_* in, JSON keyed by table out to VCO_OUTPUT_FILE.
    `extend(tables)` may add more tables (dbh-impact adds its answers)."""
    settings = json.loads(os.environ.get("VCO_SETTINGS") or "{}")
    host = os.environ.get("VCO_VCENTER_HOST", "")
    started = time.monotonic()
    if os.environ.get("VCO_MOCK") == "1":
        data = sample(with_vms)
        if settings.get("vcenter_name"):
            data["fabric_source"][0]["server"] = settings["vcenter_name"]
    else:
        if not host:
            raise SystemExit("VCO_VCENTER_HOST is not set; configure the vcenter connection")
        if not os.environ.get("VCO_VCENTER_PASSWORD"):
            raise SystemExit("vCenter password is empty: set it on the Settings page or in .env")
        source = LiveSource(host, os.environ.get("VCO_VCENTER_USER", ""), os.environ["VCO_VCENTER_PASSWORD"],
                            os.environ.get("VCO_VCENTER_VERIFY_SSL", "false").lower() == "true")
        try:
            data = source.fabric(settings.get("vcenter_name") or host, with_vms)
        finally:
            source.close()
    if extend:
        data.update(extend(data))
    log(f"{len(data['fabric_hosts'])} hosts, {len(data['fabric_datastores'])} datastores, "
        f"{len(data['fabric_mounts'])} mounts" + (f", {len(data['fabric_vms'])} VMs" if with_vms else "")
        + f" in {time.monotonic() - started:.1f}s")
    payload = json.dumps(data, default=str)
    if os.environ.get("VCO_OUTPUT_FILE"):
        with open(os.environ["VCO_OUTPUT_FILE"], "w") as fh:
            fh.write(payload)
    else:
        print(payload)
