"""Collect vCenter inventory (clusters, hosts, VMs) with pyVmomi.

Follows the vCommunity-Observability collector contract: connection details come
in as VCO_VCENTER_* / VCO_VCENTERS environment variables, one JSON object keyed by
table name is written to VCO_OUTPUT_FILE (or stdout), and logs go to stderr.
"""

import json
import os
import sys

GB = 1024 ** 3


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def csv(items) -> str:
    return ", ".join(sorted({i for i in items if i}))


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


# --------------------------------------------------------------------------- sample data
def sample(vc: dict, index: int) -> dict:
    """Built-in sample data for VCO_MOCK=1, shaped like a small home lab."""
    suffix = "" if index == 0 else f"-{vc['id']}"
    host_name = vc.get("host") or (f"vcsa91.vcrocs.local{suffix}" if index else "vcsa91.vcrocs.local")
    vcenter = [{"vcenter": vc["id"], "name": host_name, "version": "9.0.1", "build": "24957454"}]
    hosts_def = [
        # name, cluster, status, maint, cpu_used, mem_used, vms, uptime
        (f"esx9-01.vcrocs.local{suffix}", f"CL-01{suffix}", "green", 0, 9100, 210, 18, 41.2),
        (f"esx9-02.vcrocs.local{suffix}", f"CL-02{suffix}", "green", 0, 7400, 168, 4, 41.2),
        (f"esx9-03.vcrocs.local{suffix}", f"CL-02{suffix}", "red", 0, 11800, 236, 7, 3.4),
    ]
    hosts = []
    for name, cl, status, maint, cpu, mem, vms, up in hosts_def:
        hosts.append({
            "vcenter": vc["id"], "name": name, "datacenter": f"DC-01{suffix}", "cluster": cl,
            "connection_state": "connected",
            "power_state": "poweredOn", "overall_status": status, "maintenance_mode": maint,
            "version": "9.0.1", "build": "24957456", "vendor": "Supermicro", "model": "SYS-E300-9D-8CN8TP",
            "cpu_model": "Intel(R) Xeon(R) D-2146NT CPU @ 2.30GHz", "cpu_sockets": 1, "cpu_cores": 8,
            "cpu_threads": 16, "cpu_total_mhz": 18400, "cpu_used_mhz": cpu, "mem_total_gb": 256,
            "mem_used_gb": mem, "nic_count": 6, "vm_count": vms, "uptime_days": up,
            "datastores": "SYN-HDD, SYN-NVMe, vsanDatastore", "networks": "VM Network, vMotion, Management",
        })
    if index == 0:
        vm_names_cl01 = ["collector", "DC-02", "lic91", "minion01", "minion02", "MPB2", "offDepot", "OPS91",
                         "POWERSHELL-HOST", "ROCKY-Postgres", "SaltMaster", "TWINGATE", "UniFi",
                         "VCF-Installer-911", "vcf91installer", "vcf91sddc", "veeam-01"]
        vm_names_cl02 = ["nsx02", "vcsa91", "vsp01-b2hr2", "vsp01-c2td7", "vsp01-lhkgg", "vsp01-lzfms",
                         "DC-03", "TWINGATE-02"]
    else:
        vm_names_cl01 = [f"app-{vc['id']}-01", f"app-{vc['id']}-02"]
        vm_names_cl02 = [f"edge-{vc['id']}-01"]
    vms = []
    for i, name in enumerate(vm_names_cl01 + vm_names_cl02):
        cl = f"CL-01{suffix}" if name in vm_names_cl01 else f"CL-02{suffix}"
        host = hosts_def[0][0] if name in vm_names_cl01 else (hosts_def[1][0] if i % 2 else hosts_def[2][0])
        windows = name.startswith(("DC-", "veeam", "POWERSHELL", "lic"))
        on = name not in ("offDepot", "VCF-Installer-911", "minion02")
        cpus = 4 if i % 3 else 2
        mem = [4, 8, 16, 12][i % 4]
        disk = [40, 60, 100, 250][i % 4]
        ip = f"192.168.{100 + index}.{40 + i}"
        vms.append({
            "vcenter": vc["id"], "name": name, "datacenter": f"DC-01{suffix}", "cluster": cl, "host": host,
            "power_state": "poweredOn" if on else "poweredOff",
            "overall_status": "green" if on else "gray",
            "guest_os": "Microsoft Windows Server 2022 (64-bit)" if windows else "Ubuntu Linux (64-bit)",
            "guest_family": "windowsGuest" if windows else "linuxGuest",
            "tools_status": "guestToolsRunning" if on else "guestToolsNotRunning",
            "tools_version": "13312", "dns_name": name.lower() if on else "",
            "ip_addresses": (f"{ip}, fd1a:7382:63d6:7690:20c:29ff:feaf:{3000 + i:x}" if on else ""),
            "cpu_count": cpus, "cpu_used_mhz": (150 + i * 37) if on else 0,
            "memory_gb": mem, "memory_active_mb": (600 + i * 53) if on else 0,
            "storage_used_gb": round(disk * 0.4 + i, 2), "storage_provisioned_gb": disk + mem,
            "hw_version": "vmx-22",
            "disks": f"Hard disk 1|{disk}|Thin Provision|SYN-HDD",
            "nics": f"Network adapter 1|VM Network|{'connected' if on else 'disconnected'}|00:0c:29:af:{i:02x}:97",
            "cdrom": "Connected" if i % 5 == 0 else "Disconnected",
            "datastores": "SYN-HDD", "networks": "VM Network",
            "snapshots": "before-upgrade" if name in ("UniFi", "SaltMaster") else "",
            "notes": "Home lab network controller" if name == "UniFi" else "",
            "uptime_days": round(3 + i * 1.7, 1) if on else 0, "is_template": 0,
        })
    clusters = []
    for cl in (f"CL-01{suffix}", f"CL-02{suffix}"):
        hs = [h for h in hosts if h["cluster"] == cl]
        clusters.append({
            "vcenter": vc["id"], "name": cl, "datacenter": f"DC-01{suffix}", "host_count": len(hs),
            "vm_count": sum(1 for v in vms if v["cluster"] == cl),
            "drs_enabled": 1, "ha_enabled": 1 if "CL-02" in cl else 0,
            "cpu_total_mhz": sum(h["cpu_total_mhz"] for h in hs), "cpu_used_mhz": sum(h["cpu_used_mhz"] for h in hs),
            "mem_total_gb": sum(h["mem_total_gb"] for h in hs), "mem_used_gb": sum(h["mem_used_gb"] for h in hs),
            "storage_total_gb": 12288, "storage_free_gb": 5120,
        })
    return {"vcenter": vcenter, "clusters": clusters, "hosts": hosts, "vms": vms}


# --------------------------------------------------------------------------- live data
def find_parent(obj, kind):
    while obj is not None and not isinstance(obj, kind):
        obj = getattr(obj, "parent", None)
    return obj


def live(vc: dict) -> dict:
    from pyVim.connect import Disconnect, SmartConnect
    from pyVmomi import vim

    host = vc.get("host") or ""
    if not host:
        raise SystemExit(f"vCenter '{vc.get('id')}' has no host; configure it on the Settings page")
    if not vc.get("password"):
        raise SystemExit(f"vCenter '{vc.get('id')}' password is empty: set it on the Settings page or in .env")
    verify = str(vc.get("verify_ssl", "false")).lower() == "true"
    si = SmartConnect(host=host, user=vc.get("user", ""),
                      pwd=vc["password"], disableSslCertValidation=not verify)
    try:
        content = si.RetrieveContent()
        about = content.about

        def view(kind):
            v = content.viewManager.CreateContainerView(content.rootFolder, [kind], True)
            try:
                return list(v.view)
            finally:
                v.Destroy()

        def dc_name(obj):
            dc = find_parent(obj, vim.Datacenter)
            return dc.name if dc else ""

        def cluster_name(obj):
            cl = find_parent(obj, vim.ClusterComputeResource)
            return cl.name if cl else ""

        out = {"vcenter": [{"vcenter": vc["id"], "name": host, "version": about.version, "build": about.build}],
               "clusters": [], "hosts": [], "vms": []}

        for cl in view(vim.ClusterComputeResource):
            cfg = cl.configurationEx
            hs = list(cl.host)
            cpu_used = sum((h.summary.quickStats.overallCpuUsage or 0) for h in hs)
            mem_used = sum((h.summary.quickStats.overallMemoryUsage or 0) for h in hs) / 1024
            ds = list(cl.datastore)
            out["clusters"].append({
                "vcenter": vc["id"], "name": cl.name, "datacenter": dc_name(cl), "host_count": len(hs),
                "vm_count": sum(len(h.vm) for h in hs),
                "drs_enabled": int(bool(cfg.drsConfig and cfg.drsConfig.enabled)),
                "ha_enabled": int(bool(cfg.dasConfig and cfg.dasConfig.enabled)),
                "cpu_total_mhz": cl.summary.totalCpu, "cpu_used_mhz": cpu_used,
                "mem_total_gb": round(cl.summary.totalMemory / GB, 1), "mem_used_gb": round(mem_used, 1),
                "storage_total_gb": round(sum(d.summary.capacity for d in ds) / GB, 1),
                "storage_free_gb": round(sum(d.summary.freeSpace for d in ds) / GB, 1),
            })

        for h in view(vim.HostSystem):
            s = h.summary
            hw, qs, rt = s.hardware, s.quickStats, s.runtime
            prod = s.config.product if s.config else None
            out["hosts"].append({
                "vcenter": vc["id"], "name": h.name, "datacenter": dc_name(h), "cluster": cluster_name(h),
                "connection_state": str(rt.connectionState), "power_state": str(rt.powerState),
                "overall_status": str(s.overallStatus), "maintenance_mode": int(bool(rt.inMaintenanceMode)),
                "version": prod.version if prod else "", "build": prod.build if prod else "",
                "vendor": hw.vendor if hw else "", "model": hw.model if hw else "",
                "cpu_model": hw.cpuModel if hw else "", "cpu_sockets": hw.numCpuPkgs if hw else None,
                "cpu_cores": hw.numCpuCores if hw else None, "cpu_threads": hw.numCpuThreads if hw else None,
                "cpu_total_mhz": (hw.cpuMhz * hw.numCpuCores) if hw else None,
                "cpu_used_mhz": qs.overallCpuUsage, "mem_total_gb": round(hw.memorySize / GB, 1) if hw else None,
                "mem_used_gb": round((qs.overallMemoryUsage or 0) / 1024, 1),
                "nic_count": hw.numNics if hw else None, "vm_count": len(h.vm),
                "uptime_days": round(qs.uptime / 86400, 1) if qs.uptime else None,
                "datastores": csv(d.name for d in h.datastore), "networks": csv(n.name for n in h.network),
            })

        for vm in view(vim.VirtualMachine):
            try:
                s = vm.summary
                cfg, rt, qs, guest = s.config, s.runtime, s.quickStats, vm.guest
                disks, nics, cdrom = [], [], ""
                devices = vm.config.hardware.device if vm.config else []
                for d in devices:
                    if isinstance(d, vim.vm.device.VirtualDisk):
                        b = d.backing
                        fmt = "Thin Provision" if getattr(b, "thinProvisioned", False) else "Thick Provision"
                        ds = b.datastore.name if getattr(b, "datastore", None) else ""
                        disks.append(f"{d.deviceInfo.label}|{round(d.capacityInKB / 1024 / 1024, 1)}|{fmt}|{ds}")
                    elif isinstance(d, vim.vm.device.VirtualEthernetCard):
                        conn = "connected" if (d.connectable and d.connectable.connected) else "disconnected"
                        nics.append(f"{d.deviceInfo.label}|{d.deviceInfo.summary}|{conn}|{d.macAddress}")
                    elif isinstance(d, vim.vm.device.VirtualCdrom) and not cdrom:
                        cdrom = "Connected" if (d.connectable and d.connectable.connected) else "Disconnected"
                ips = []
                for n in (guest.net or []):
                    ips.extend(n.ipAddress or [])
                snaps = []

                def walk(nodes):
                    for n in nodes or []:
                        snaps.append(n.name)
                        walk(n.childSnapshotList)
                if vm.snapshot:
                    walk(vm.snapshot.rootSnapshotList)
                out["vms"].append({
                    "vcenter": vc["id"], "name": cfg.name, "datacenter": dc_name(vm),
                    "cluster": cluster_name(rt.host) if rt.host else "",
                    "host": rt.host.name if rt.host else "", "power_state": str(rt.powerState),
                    "overall_status": str(s.overallStatus), "guest_os": cfg.guestFullName,
                    "guest_family": guest.guestFamily or "", "tools_status": str(guest.toolsRunningStatus or ""),
                    "tools_version": guest.toolsVersion or "", "dns_name": guest.hostName or "",
                    "ip_addresses": ", ".join(ips), "cpu_count": cfg.numCpu,
                    "cpu_used_mhz": qs.overallCpuUsage, "memory_gb": round((cfg.memorySizeMB or 0) / 1024, 1),
                    "memory_active_mb": qs.guestMemoryUsage,
                    "storage_used_gb": round((s.storage.committed or 0) / GB, 2) if s.storage else None,
                    "storage_provisioned_gb": round(((s.storage.committed or 0) + (s.storage.uncommitted or 0)) / GB, 2) if s.storage else None,
                    "hw_version": vm.config.version if vm.config else "",
                    "disks": ";".join(disks), "nics": ";".join(nics), "cdrom": cdrom,
                    "datastores": csv(d.name for d in vm.datastore), "networks": csv(n.name for n in vm.network),
                    "snapshots": ";".join(snaps), "notes": cfg.annotation or "",
                    "uptime_days": round(qs.uptimeSeconds / 86400, 1) if qs.uptimeSeconds else None,
                    "is_template": int(bool(cfg.template)),
                })
            except Exception as exc:  # one odd VM must not break the whole run
                log(f"skipped a VM: {exc}")
        log(f"collected {len(out['clusters'])} clusters, {len(out['hosts'])} hosts, {len(out['vms'])} VMs from {host} ({vc['id']})")
        return out
    finally:
        Disconnect(si)


def merge(parts: list[dict]) -> dict:
    out = {"vcenter": [], "clusters": [], "hosts": [], "vms": []}
    for part in parts:
        for key in out:
            out[key].extend(part.get(key) or [])
    return out


if __name__ == "__main__":
    instances = vcenters()
    if not instances:
        raise SystemExit("No vCenter configured; add one on the Settings page or in config.yaml")
    mock = os.environ.get("VCO_MOCK") == "1"
    data = merge([sample(vc, i) if mock else live(vc) for i, vc in enumerate(instances)])
    payload = json.dumps(data)
    if os.environ.get("VCO_OUTPUT_FILE"):
        with open(os.environ["VCO_OUTPUT_FILE"], "w") as fh:
            fh.write(payload)
    else:
        print(payload)
