"""DBH Insights inventory sheets, ported from collect/js/sheets.js.

Columns, units, rounding and the vCLS / null rules match the website, so a sheet
collected here reads the same as the one in the browser and in its XLSX export.
Values vCenter doesn't report stay None rather than becoming 0 or "unknown".

Each sheet is stored in its own SQLite table. Table and column names are derived
from the sheet and column labels (vHost -> vhost, "# VMs total" -> num_vms_total);
the original labels travel with the data in the sheet_columns table.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any, Callable

from vsphere import Obj, at, b, moref, n, t, text, xsi

MODULE_VERSION = "0.1.0"
DBH_VERSION = "0.5.0"   # the DBH Insights release these sheets follow

# --------------------------------------------------------------------------- columns

AVG, NONE, SUM = "avg", "none", "sum"


def ratio_total(numerator: str, denominator: str, scale: float = 1) -> str:
    """Totals row: a ratio of two columns' sums (total Free % = free / capacity)."""
    return f"ratio:{numerator}|{denominator}|{scale:g}"


def text_col(label: str) -> tuple[str, str, str]:
    return (label, "text", NONE)


def num_col(label: str, total: str = SUM) -> tuple[str, str, str]:
    return (label, "number", total)


def bool_col(label: str) -> tuple[str, str, str]:
    return (label, "bool", SUM)


VI_SDK_SERVER = "VI SDK Server"
RESERVED = {"limit", "key", "order", "group", "index", "default", "check", "primary", "references",
            "values", "table", "to", "from", "select", "where", "set", "collected_at"}


def column_name(label: str) -> str:
    s = label.replace("#", " num ").replace("%", " pct ").replace("+", " plus ").lower()
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    if not s or not s[0].isalpha():
        s = "c_" + s
    return s + "_value" if s in RESERVED else s


def table_name(sheet: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", sheet.lower()).strip("_")


# --------------------------------------------------------------------------- math helpers

def round2(v: float) -> float:
    # JavaScript Math.round(v * 100) / 100 (half rounds up), not banker's rounding.
    return math.floor(v * 100 + 0.5) / 100


def gib(v: float | None) -> float | None:
    return None if v is None else round2(v / 1024 ** 3)


def mib(v: float | None) -> float | None:
    return None if v is None else round2(v / 1024 ** 2)


def percent(used: float | None, total: float | None) -> float | None:
    return round2(used / total * 100) if used is not None and total else None


def ratio(numerator: float, denominator: float | None) -> float | None:
    return round2(numerator / denominator) if denominator else None


def is_vcls(name: str) -> bool:
    return name.startswith("vCLS-")


def strip_prefix(s: str | None, prefix: str) -> str | None:
    if not s:
        return None
    return (s[len(prefix):] if s.startswith(prefix) else s) or None


# --------------------------------------------------------------------------- shared queries

VM_INVENTORY_PROPS = [
    "name", "config.template", "runtime.powerState", "runtime.host", "guest.ipAddress", "guest.hostName",
    "guest.guestFullName", "config.guestFullName", "guest.toolsVersionStatus", "guest.toolsRunningStatus",
    "config.createDate", "config.version", "config.annotation", "config.firmware",
    "config.bootOptions.efiSecureBootEnabled", "config.hardware.numCPU", "config.hardware.numCoresPerSocket",
    "config.hardware.memoryMB", "config.files.vmPathName", "config.changeVersion", "config.uuid",
    "summary.storage.committed", "summary.storage.uncommitted", "summary.quickStats.overallCpuUsage",
    "summary.runtime.maxCpuUsage", "summary.quickStats.guestMemoryUsage", "summary.config.memorySizeMB",
    # vCPU
    "config.cpuAllocation.shares.level", "config.cpuAllocation.shares.shares", "config.cpuAllocation.reservation",
    "config.cpuAllocation.limit", "config.cpuHotAddEnabled", "config.cpuHotRemoveEnabled",
    "summary.quickStats.staticCpuEntitlement", "summary.quickStats.distributedCpuEntitlement",
    # vMemory
    "config.memoryAllocation.shares.level", "config.memoryAllocation.shares.shares",
    "config.memoryAllocation.reservation", "config.memoryAllocation.limit", "config.memoryHotAddEnabled",
    "summary.quickStats.hostMemoryUsage", "summary.quickStats.privateMemory", "summary.quickStats.sharedMemory",
    "summary.quickStats.swappedMemory", "summary.quickStats.balloonedMemory", "summary.quickStats.compressedMemory",
    "summary.quickStats.consumedOverheadMemory", "summary.quickStats.staticMemoryEntitlement",
    "summary.quickStats.distributedMemoryEntitlement",
    # vPartition and vTools
    "guest.disk", "guest.toolsStatus", "guest.toolsVersion", "guest.guestState", "guest.guestId",
    "guest.guestFamily", "config.tools.toolsUpgradePolicy", "config.tools.syncTimeWithHost",
    "config.tools.afterPowerOn", "config.tools.afterResume",
]

VM_DEVICE_PROPS = ["name", "config.hardware.device", "snapshot.rootSnapshotList", "snapshot.currentSnapshot",
                   "layoutEx.file", "layoutEx.snapshot"]

HOST_PROPS = [
    "name", "overallStatus", "runtime.connectionState", "runtime.powerState", "runtime.inMaintenanceMode",
    "runtime.bootTime", "summary.hardware.vendor", "summary.hardware.model", "summary.hardware.cpuModel",
    "summary.hardware.cpuMhz", "summary.hardware.numCpuPkgs", "summary.hardware.numCpuCores",
    "summary.hardware.numCpuThreads", "summary.hardware.memorySize", "hardware.memoryTierInfo",
    "summary.hardware.numNics", "summary.hardware.numHBAs", "summary.hardware.uuid",
    "summary.quickStats.overallCpuUsage", "summary.quickStats.overallMemoryUsage", "summary.currentEVCModeKey",
    "summary.maxEVCModeKey", "capability.vmotionSupported", "capability.storageVMotionSupported",
    "config.product.fullName", "config.hyperThread.available", "config.hyperThread.active",
    "config.network.dnsConfig.domainName", "config.network.dnsConfig.address",
    "config.network.dnsConfig.searchDomain", "config.network.dnsConfig.dhcp",
    "config.dateTimeInfo.timeZone.name", "config.dateTimeInfo.timeZone.gmtOffset",
    "config.dateTimeInfo.ntpConfig.server", "config.service.service", "hardware.biosInfo.biosVersion",
    "hardware.biosInfo.releaseDate", "hardware.cpuPowerManagementInfo.currentPolicy",
    "hardware.systemInfo.serialNumber", "hardware.systemInfo.otherIdentifyingInfo",
    "config.storageDevice.hostBusAdapter", "config.network.pnic", "config.network.vnic",
    "config.network.vswitch", "config.network.portgroup",
]

# HA and DRS come from the cluster's SOAP configuration here (the website asks REST).
CLUSTER_PROPS = ["name", "host", "configuration.dasConfig.enabled", "configuration.drsConfig.enabled"]

DATASTORE_PROPS = ["name", "summary.type", "summary.capacity", "summary.freeSpace", "summary.accessible", "host", "vm"]

RESOURCE_POOL_PROPS = [
    "name", "owner", "overallStatus", "vm", "config.cpuAllocation.limit", "config.cpuAllocation.reservation",
    "config.cpuAllocation.shares.level", "config.cpuAllocation.shares.shares", "config.memoryAllocation.limit",
    "config.memoryAllocation.reservation", "config.memoryAllocation.shares.level",
    "config.memoryAllocation.shares.shares", "summary.quickStats.overallCpuUsage",
    "summary.quickStats.guestMemoryUsage", "summary.quickStats.hostMemoryUsage",
]

DVSWITCH_PROPS = ["name", "uuid", "summary.productInfo", "summary.numPorts", "summary.hostMember", "config.maxPorts",
                  "config.createTime"]

DVPORTGROUP_PROPS = ["name", "key", "config.numPorts", "config.type", "config.defaultPortConfig",
                     "config.distributedVirtualSwitch"]

HOST_COUNTERS = {
    "cpuPercent": "cpu.usage.average", "cpuMhz": "cpu.usagemhz.average", "memoryPercent": "mem.usage.average",
    "memoryConsumed": "mem.consumed.average", "network": "net.usage.average", "disk": "disk.usage.average",
}
VM_COUNTERS = {**HOST_COUNTERS, "memoryActive": "mem.active.average"}


class VcenterData:
    """Everything fetched from one vCenter, each query made once and shared."""

    def __init__(self, source: Any, server: str, username: str):
        self.source, self.server, self.username = source, server, username
        self._cache: dict[str, Any] = {}

    def once(self, key: str, run: Callable[[], Any]) -> Any:
        if key not in self._cache:
            self._cache[key] = run()
        return self._cache[key]

    def vm_inventory(self) -> list[Obj]:
        return self.once("vmInventory", lambda: self.source.retrieve("VirtualMachine", VM_INVENTORY_PROPS))

    def vm_devices(self) -> list[Obj]:
        return self.once("vmDevices", lambda: self.source.retrieve("VirtualMachine", VM_DEVICE_PROPS))

    def hosts(self) -> list[Obj]:
        return self.once("hosts", lambda: self.source.retrieve("HostSystem", HOST_PROPS))

    def clusters(self) -> list[Obj]:
        return self.once("clusters", lambda: self.source.retrieve("ClusterComputeResource", CLUSTER_PROPS))

    def datastores(self) -> list[Obj]:
        return self.once("datastores", lambda: self.source.retrieve("Datastore", DATASTORE_PROPS))

    def datacenters(self) -> list[Obj]:
        return self.once("datacenters", lambda: self.source.retrieve("Datacenter", ["name"]))

    def networks(self) -> dict[str, str]:
        """Network id (dvportgroup-..., network-...) -> name."""
        return self.once("networks", lambda: {o.moref: o.str("name") for o in self.source.retrieve("Network", ["name"])})

    def resource_pools(self) -> list[Obj]:
        return self.once("resourcePools", lambda: self.source.retrieve("ResourcePool", RESOURCE_POOL_PROPS))

    def dv_switches(self) -> list[Obj]:
        return self.once("dvSwitches", lambda: self.source.retrieve("VmwareDistributedVirtualSwitch", DVSWITCH_PROPS))

    def dv_portgroups(self) -> list[Obj]:
        return self.once("dvPortgroups", lambda: self.source.retrieve("DistributedVirtualPortgroup", DVPORTGROUP_PROPS))

    def about(self) -> Any:
        return self.once("about", self.source.about)

    def licenses(self) -> list[Any]:
        return self.once("licenses", self.source.licenses)

    def perf_samples(self, key: str, objects: list[Obj], wanted: dict[str, str]) -> dict[str, dict[str, Any]]:
        return self.once(key, lambda: self.source.perf_samples(objects, wanted))

    def host_names(self) -> dict[str, str]:
        return self.once("hostNames", lambda: {h.moref: h.str("name") for h in self.hosts()})

    def vm_totals_by_host(self) -> dict[str, dict[str, float]]:
        """Per-host rollup of registered VMs. vCLS VMs are excluded, as the vSphere UI does."""
        def run() -> dict[str, dict[str, float]]:
            totals: dict[str, dict[str, float]] = {}
            for vm in self.vm_inventory():
                name, host = vm.str("name"), vm.str("runtime.host")
                if not name or is_vcls(name) or not host:
                    continue
                tt = totals.setdefault(host, {"vmsTotal": 0, "vmsPoweredOn": 0, "vcpus": 0, "vramMiB": 0})
                tt["vmsTotal"] += 1
                if vm.str("runtime.powerState") == "poweredOn":
                    tt["vmsPoweredOn"] += 1
                tt["vcpus"] += vm.num("config.hardware.numCPU") or 0
                tt["vramMiB"] += vm.num("config.hardware.memoryMB") or 0
            return totals
        return self.once("vmTotals", run)

    def vms_by_moref(self) -> dict[str, Obj]:
        return self.once("vmsByMoref", lambda: {vm.moref: vm for vm in self.vm_inventory()})


def vm_context(vm: Obj, host_names: dict[str, str]) -> dict[str, Any] | None:
    """The per-VM columns repeated on every VM-derived sheet. None for vCLS VMs."""
    name = vm.str("name")
    if not name:
        raise ValueError(f"VirtualMachine {vm.moref} returned no name property")
    if is_vcls(name):
        return None
    host = vm.str("runtime.host")
    return {"name": name, "powerState": vm.str("runtime.powerState"), "template": vm.bool("config.template"),
            "host": host_names.get(host, host) if host else None, "annotation": vm.str("config.annotation")}


def ctx_for(dev: Obj, inventory: dict[str, Obj], host_names: dict[str, str]) -> dict[str, Any] | None:
    return vm_context(inventory.get(dev.moref, dev), host_names)


# --------------------------------------------------------------------------- host helpers

def memory_tier_bytes(host: Obj, tier_type: str) -> float | None:
    """Size of one memory tier (DRAM, NVMe). With tiering on, memorySize is DRAM plus NVMe."""
    tier = next((x for x in host.array("hardware.memoryTierInfo") if t(x, "type") == tier_type), None)
    return n(tier, "size") if tier is not None else None


def memory_tiering_type(host: Obj) -> str | None:
    types = [t(x, "type") for x in host.array("hardware.memoryTierInfo")]
    types = [x for x in types if x]
    return " + ".join(types) if types else None


def string_array(obj: Obj, prop: str) -> str | None:
    joined = ", ".join(text(e) for e in obj.array(prop) if text(e))
    return joined or None


def service_running(host: Obj, key: str) -> bool | None:
    service = next((s for s in host.array("config.service.service") if t(s, "key") == key), None)
    return b(service, "running") if service is not None else None


def identifying_info(host: Obj, key: str) -> str | None:
    entry = next((e for e in host.array("hardware.systemInfo.otherIdentifyingInfo")
                  if t(e, "identifierType/key") == key), None)
    value = t(entry, "identifierValue") if entry is not None else None
    return value if value and value != "Default string" else None


def wwn(value: Any) -> str | None:
    """A world wide name as vCenter reports it (a number) in the usual hex form."""
    if value is None:
        return None
    try:
        h = format(int(value) & 0xFFFFFFFFFFFFFFFF, "016x")
    except (TypeError, ValueError):
        return str(value)
    return ":".join(h[i:i + 2] for i in range(0, 16, 2))


def dv_bool(setting: Any, path: str) -> bool | None:
    """A distributed port group setting wraps its value: securityPolicy.allowPromiscuous.value."""
    return b(setting, f"{path}/value")


def dv_vlan(port_config: Any) -> str | None:
    """VLAN of a distributed port group: a single id, a trunk range, or private VLAN."""
    vlan = at(port_config, "vlan")
    if vlan is None:
        return None
    kind = xsi(vlan) or ""
    vid = getattr(vlan, "vlanId", None)
    if "Trunk" in kind or isinstance(vid, (list, tuple)):
        ranges = []
        for r in vid or []:
            start, end = t(r, "start"), t(r, "end")
            ranges.append(start if start == end else f"{start}-{end}")
        return f"trunk {', '.join(x for x in ranges if x)}" if ranges else None
    if "Pvlan" in kind or getattr(vlan, "pvlanId", None) is not None:
        pv = t(vlan, "pvlanId")
        return None if pv is None else f"pvlan {pv}"
    return text(vid)


# --------------------------------------------------------------------------- snapshot helpers

def flatten_snapshots(node: Any, out: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    out = [] if out is None else out
    out.append({"moref": moref(at(node, "snapshot")) or "", "name": t(node, "name"),
                "description": t(node, "description"), "createTime": t(node, "createTime"),
                "state": t(node, "state"), "quiesced": b(node, "quiesced")})
    for child in at(node, "childSnapshotList") or []:
        flatten_snapshots(child, out)
    return out


def format_timestamp(raw: str | None) -> str:
    """yyyy/MM/dd HH:mm:ss in UTC. Unparseable input passes through."""
    if not raw:
        return raw or ""
    try:
        d = datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return raw
    return d.strftime("%Y/%m/%d %H:%M:%S")


def folder_of(vm_path: str) -> str | None:
    """The folder of `[datastore] folder/name.vmx`; None for a VM at the datastore root."""
    i = vm_path.find("] ")
    if i < 0:
        return None
    rest = vm_path[i + 2:]
    j = rest.rfind("/")
    return None if j < 0 else rest[:j]


def devices(dev: Obj) -> list[Any]:
    return dev.array("config.hardware.device")


# --------------------------------------------------------------------------- sheets

SHEETS: list[dict[str, Any]] = []


def sheet(name: str, group: str, columns: list[tuple[str, str, str]]):
    def register(fn: Callable[[VcenterData], list[list[Any]]]):
        SHEETS.append({"name": name, "group": group, "columns": columns, "rows": fn})
        return fn
    return register


@sheet("vInfo", "Virtual machines", [
    text_col("VM"), text_col("Powerstate"), bool_col("Template"), text_col("DNS Name"), num_col("CPUs"),
    num_col("Cores p/s", NONE), num_col("Memory"), num_col("CPU Usage (%)", AVG), num_col("Memory Usage (%)", AVG),
    num_col("Provisioned GiB"), num_col("In Use GiB"), text_col("Primary IP Address"),
    text_col("OS according to the configuration file"), text_col("OS according to the VMware Tools"),
    text_col("Tools Version Status"), text_col("Tools Running Status"), text_col("Host"), text_col("Creation date"),
    text_col("HW version"), text_col("Firmware"), bool_col("EFI Secure boot"), text_col("Path"),
    text_col("Annotation"), text_col("Change Version"), text_col("VM UUID"),
])
def v_info(d: VcenterData) -> list[list[Any]]:
    hosts, rows = d.host_names(), []
    for vm in d.vm_inventory():
        name = vm.str("name")
        if not name:
            raise ValueError(f"VirtualMachine {vm.moref} returned no name property")
        if is_vcls(name):
            continue
        committed, uncommitted = vm.num("summary.storage.committed"), vm.num("summary.storage.uncommitted")
        host = vm.str("runtime.host")
        rows.append([
            name, vm.str("runtime.powerState"), vm.bool("config.template"), vm.str("guest.hostName"),
            vm.num("config.hardware.numCPU"), vm.num("config.hardware.numCoresPerSocket"),
            vm.num("config.hardware.memoryMB"),
            percent(vm.num("summary.quickStats.overallCpuUsage"), vm.num("summary.runtime.maxCpuUsage")),
            percent(vm.num("summary.quickStats.guestMemoryUsage"), vm.num("summary.config.memorySizeMB")),
            gib(committed + uncommitted) if committed is not None and uncommitted is not None else None,
            gib(committed), vm.str("guest.ipAddress"), vm.str("config.guestFullName"),
            vm.str("guest.guestFullName"), vm.str("guest.toolsVersionStatus"), vm.str("guest.toolsRunningStatus"),
            hosts.get(host, host) if host else None, vm.str("config.createDate"), vm.str("config.version"),
            vm.str("config.firmware"), vm.bool("config.bootOptions.efiSecureBootEnabled"),
            vm.str("config.files.vmPathName"), vm.str("config.annotation"), vm.str("config.changeVersion"),
            vm.str("config.uuid"),
        ])
    return rows


@sheet("vCPU", "Virtual machines", [
    text_col("VM"), text_col("Powerstate"), bool_col("Template"), num_col("CPUs"), num_col("Sockets"),
    num_col("Cores p/s", NONE), num_col("Overall MHz"), num_col("Max MHz", NONE), num_col("CPU Usage (%)", AVG),
    text_col("Level"), num_col("Shares", NONE), num_col("Reservation"), num_col("Limit", NONE),
    num_col("Entitlement"), num_col("DRS Entitlement"), bool_col("Hot Add"), bool_col("Hot Remove"),
    text_col("Host"), text_col("Annotation"),
])
def v_cpu(d: VcenterData) -> list[list[Any]]:
    hosts, rows = d.host_names(), []
    for vm in d.vm_inventory():
        ctx = vm_context(vm, hosts)
        if not ctx:
            continue
        cpus, cps = vm.num("config.hardware.numCPU"), vm.num("config.hardware.numCoresPerSocket")
        used, mx = vm.num("summary.quickStats.overallCpuUsage"), vm.num("summary.runtime.maxCpuUsage")
        rows.append([
            ctx["name"], ctx["powerState"], ctx["template"], cpus,
            (cpus / cps) if cpus is not None and cps else None, cps, used, mx, percent(used, mx),
            vm.str("config.cpuAllocation.shares.level"), vm.num("config.cpuAllocation.shares.shares"),
            vm.num("config.cpuAllocation.reservation"), vm.num("config.cpuAllocation.limit"),
            vm.num("summary.quickStats.staticCpuEntitlement"), vm.num("summary.quickStats.distributedCpuEntitlement"),
            vm.bool("config.cpuHotAddEnabled"), vm.bool("config.cpuHotRemoveEnabled"), ctx["host"], ctx["annotation"],
        ])
    return rows


@sheet("vMemory", "Virtual machines", [
    text_col("VM"), text_col("Powerstate"), bool_col("Template"), num_col("Size MiB"), num_col("Consumed MiB"),
    num_col("Active MiB"), num_col("Memory Usage (%)", AVG), num_col("Private MiB"), num_col("Shared MiB"),
    num_col("Swapped MiB"), num_col("Ballooned MiB"), num_col("Compressed MiB"), num_col("Consumed Overhead MiB"),
    num_col("Entitlement"), num_col("DRS Entitlement"), text_col("Level"), num_col("Shares", NONE),
    num_col("Reservation"), num_col("Limit", NONE), bool_col("Hot Add"), text_col("Host"), text_col("Annotation"),
])
def v_memory(d: VcenterData) -> list[list[Any]]:
    hosts, rows = d.host_names(), []
    for vm in d.vm_inventory():
        ctx = vm_context(vm, hosts)
        if not ctx:
            continue
        rows.append([
            ctx["name"], ctx["powerState"], ctx["template"], vm.num("config.hardware.memoryMB"),
            vm.num("summary.quickStats.hostMemoryUsage"), vm.num("summary.quickStats.guestMemoryUsage"),
            percent(vm.num("summary.quickStats.guestMemoryUsage"), vm.num("summary.config.memorySizeMB")),
            vm.num("summary.quickStats.privateMemory"), vm.num("summary.quickStats.sharedMemory"),
            vm.num("summary.quickStats.swappedMemory"), vm.num("summary.quickStats.balloonedMemory"),
            vm.num("summary.quickStats.compressedMemory"), vm.num("summary.quickStats.consumedOverheadMemory"),
            vm.num("summary.quickStats.staticMemoryEntitlement"),
            vm.num("summary.quickStats.distributedMemoryEntitlement"), vm.str("config.memoryAllocation.shares.level"),
            vm.num("config.memoryAllocation.shares.shares"), vm.num("config.memoryAllocation.reservation"),
            vm.num("config.memoryAllocation.limit"), vm.bool("config.memoryHotAddEnabled"), ctx["host"],
            ctx["annotation"],
        ])
    return rows


@sheet("vDisk", "Virtual machines", [
    text_col("VM"), text_col("Powerstate"), bool_col("Template"), text_col("Disk"), num_col("Disk Key", NONE),
    text_col("Disk UUID"), text_col("Disk Path"), num_col("Capacity MiB"), bool_col("Raw"), text_col("Disk Mode"),
    text_col("Sharing mode"), bool_col("Thin"), bool_col("Eagerly Scrub"), bool_col("Split"),
    bool_col("Write Through"), text_col("Level"), num_col("Shares", NONE), num_col("Reservation"),
    num_col("Limit", NONE), text_col("Controller"), num_col("Unit #", NONE), text_col("Raw LUN ID"),
    text_col("Raw Comp. Mode"), text_col("Host"), text_col("Annotation"),
])
def v_disk(d: VcenterData) -> list[list[Any]]:
    hosts, inventory, rows = d.host_names(), d.vms_by_moref(), []
    for dev in d.vm_devices():
        ctx = ctx_for(dev, inventory, hosts)
        if not ctx:
            continue
        everything = devices(dev)
        controllers = {t(x, "key"): t(x, "deviceInfo/label") for x in everything}
        for disk in (x for x in everything if xsi(x) == "VirtualDisk"):
            backing = at(disk, "backing")
            kib = n(disk, "capacityInKB")
            rows.append([
                ctx["name"], ctx["powerState"], ctx["template"], t(disk, "deviceInfo/label"), n(disk, "key"),
                t(backing, "uuid"), t(backing, "fileName"), None if kib is None else round2(kib / 1024),
                (xsi(backing) or "").startswith("VirtualDiskRawDiskMapping"), t(backing, "diskMode"),
                t(backing, "sharing"), b(backing, "thinProvisioned"), b(backing, "eagerlyScrub"),
                b(backing, "split"), b(backing, "writeThrough"), t(disk, "storageIOAllocation/shares/level"),
                n(disk, "storageIOAllocation/shares/shares"), n(disk, "storageIOAllocation/reservation"),
                n(disk, "storageIOAllocation/limit"), controllers.get(t(disk, "controllerKey")),
                n(disk, "unitNumber"), t(backing, "lunUuid"), t(backing, "compatibilityMode"), ctx["host"],
                ctx["annotation"],
            ])
    return rows


@sheet("vPartition", "Virtual machines", [
    text_col("VM"), text_col("Powerstate"), text_col("Disk"), num_col("Capacity MiB"), num_col("Consumed MiB"),
    num_col("Free MiB"), num_col("Free %", ratio_total("Free MiB", "Capacity MiB", 100)), text_col("Host"),
    text_col("Annotation"),
])
def v_partition(d: VcenterData) -> list[list[Any]]:
    """Guest filesystems, as VMware Tools reports them. VMs without Tools running have none."""
    hosts, rows = d.host_names(), []
    for vm in d.vm_inventory():
        ctx = vm_context(vm, hosts)
        if not ctx:
            continue
        for disk in vm.array("guest.disk"):
            cap, free = n(disk, "capacity"), n(disk, "freeSpace")
            rows.append([ctx["name"], ctx["powerState"], t(disk, "diskPath"), mib(cap),
                         mib(cap - free) if cap is not None and free is not None else None, mib(free),
                         percent(free, cap), ctx["host"], ctx["annotation"]])
    return rows


@sheet("vNetwork", "Virtual machines", [
    text_col("VM"), text_col("Powerstate"), bool_col("Template"), text_col("NIC label"), text_col("Adapter"),
    text_col("Network"), bool_col("Connected"), bool_col("Starts Connected"), text_col("Mac Address"),
    text_col("Type"), text_col("Host"), text_col("Annotation"),
])
def v_network(d: VcenterData) -> list[list[Any]]:
    hosts, inventory, rows = d.host_names(), d.vms_by_moref(), []
    try:
        names = d.networks()
    except Exception:  # names are a convenience; without them the backing key is shown
        names = {}
    for dev in d.vm_devices():
        ctx = ctx_for(dev, inventory, hosts)
        if not ctx:
            continue
        # Every virtual ethernet card, whatever its concrete type, carries a MAC address.
        for nic in (x for x in devices(dev) if getattr(x, "macAddress", None)):
            backing = at(nic, "backing")
            kind = xsi(backing) or ""
            if "DistributedVirtualPort" in kind:
                key = t(backing, "port/portgroupKey")
                network = names.get(key, key) if key else None
            elif "OpaqueNetwork" in kind:
                network = t(backing, "opaqueNetworkId")
            elif backing is not None:
                network = t(backing, "deviceName")
            else:
                network = None
            rows.append([ctx["name"], ctx["powerState"], ctx["template"], t(nic, "deviceInfo/label"),
                         strip_prefix(xsi(nic), "Virtual"), network, b(nic, "connectable/connected"),
                         b(nic, "connectable/startConnected"), t(nic, "macAddress"), t(nic, "addressType"),
                         ctx["host"], ctx["annotation"]])
    return rows


@sheet("vSnapshot", "Virtual machines", [
    text_col("VM"), text_col("Powerstate"), text_col("Name"), text_col("Description"), text_col("Date / time"),
    text_col("Filename"), num_col("Size MiB (vmsn)"), num_col("Size MiB (total)"), bool_col("Quiesced"),
    text_col("State"), bool_col("Is current"), text_col("Host"), text_col("Annotation"),
])
def v_snapshot(d: VcenterData) -> list[list[Any]]:
    hosts, inventory, rows = d.host_names(), d.vms_by_moref(), []
    for dev in d.vm_devices():
        ctx = ctx_for(dev, inventory, hosts)
        if not ctx:
            continue
        snaps = [s for root in dev.array("snapshot.rootSnapshotList") for s in flatten_snapshots(root)]
        if not snaps:
            continue
        files = {t(f, "key"): {"name": t(f, "name"), "size": n(f, "size")} for f in dev.array("layoutEx.file")}
        layout = {moref(at(s, "key")): {"data": t(s, "dataKey"), "memory": t(s, "memoryKey")}
                  for s in dev.array("layoutEx.snapshot")}
        current = dev.str("snapshot.currentSnapshot")
        for snap in snaps:
            keys = layout.get(snap["moref"], {})

            def file_of(key: str | None) -> dict[str, Any] | None:
                # A memoryKey of -1 means the snapshot captured no memory state.
                return files.get(key) if key and key != "-1" else None
            data_file, memory_file = file_of(keys.get("data")), file_of(keys.get("memory"))
            # The snapshot's own files (.vmsn plus .vmem); delta-disk growth isn't guessed at.
            total = ((data_file or {}).get("size") or 0) + ((memory_file or {}).get("size") or 0) \
                if data_file or memory_file else None
            rows.append([ctx["name"], ctx["powerState"], snap["name"], snap["description"], snap["createTime"],
                         (data_file or {}).get("name"), mib((data_file or {}).get("size")), mib(total),
                         snap["quiesced"], snap["state"], current == snap["moref"], ctx["host"], ctx["annotation"]])
    return rows


@sheet("vCD", "Virtual machines", [
    text_col("VM"), text_col("Powerstate"), text_col("Device"), bool_col("Connected"), bool_col("Starts Connected"),
    bool_col("Allow guest control"), text_col("Backing"), text_col("Device / ISO"), text_col("Host"),
    text_col("Annotation"),
])
def v_cd(d: VcenterData) -> list[list[Any]]:
    hosts, inventory, rows = d.host_names(), d.vms_by_moref(), []
    for dev in d.vm_devices():
        ctx = ctx_for(dev, inventory, hosts)
        if not ctx:
            continue
        for cd in (x for x in devices(dev) if xsi(x) == "VirtualCdrom"):
            backing = at(cd, "backing")
            kind = re.sub(r"^VirtualCdrom|BackingInfo$", "", xsi(backing) or "") or None
            rows.append([ctx["name"], ctx["powerState"], t(cd, "deviceInfo/label"), b(cd, "connectable/connected"),
                         b(cd, "connectable/startConnected"), b(cd, "connectable/allowGuestControl"), kind,
                         (t(backing, "fileName") or t(backing, "deviceName")) if backing is not None else None,
                         ctx["host"], ctx["annotation"]])
    return rows


@sheet("vUSB", "Virtual machines", [
    text_col("VM"), text_col("Powerstate"), text_col("Device"), text_col("Type"), bool_col("Connected"),
    bool_col("Auto connect"), text_col("Summary"), text_col("Host"), text_col("Annotation"),
])
def v_usb(d: VcenterData) -> list[list[Any]]:
    """USB controllers and any passed-through USB devices."""
    hosts, inventory, rows = d.host_names(), d.vms_by_moref(), []
    for dev in d.vm_devices():
        ctx = ctx_for(dev, inventory, hosts)
        if not ctx:
            continue
        for usb in (x for x in devices(dev) if (xsi(x) or "").startswith("VirtualUSB")):
            rows.append([ctx["name"], ctx["powerState"], (t(usb, "deviceInfo/label") or "").strip() or None,
                         re.sub(r"^Virtual", "", xsi(usb) or ""), b(usb, "connectable/connected"),
                         b(usb, "autoConnectDevices"), t(usb, "deviceInfo/summary"), ctx["host"], ctx["annotation"]])
    return rows


@sheet("vTools", "Virtual machines", [
    text_col("VM"), text_col("Powerstate"), text_col("Tools status"), text_col("Tools version status"),
    text_col("Tools running status"), text_col("Tools version"), text_col("Upgrade policy"),
    bool_col("Sync time with host"), bool_col("Run after power on"), bool_col("Run after resume"),
    text_col("Guest state"), text_col("Guest id"), text_col("Guest family"), text_col("DNS Name"), text_col("Host"),
    text_col("Annotation"),
])
def v_tools(d: VcenterData) -> list[list[Any]]:
    hosts, rows = d.host_names(), []
    for vm in d.vm_inventory():
        ctx = vm_context(vm, hosts)
        if not ctx:
            continue
        rows.append([ctx["name"], ctx["powerState"], vm.str("guest.toolsStatus"), vm.str("guest.toolsVersionStatus"),
                     vm.str("guest.toolsRunningStatus"), vm.str("guest.toolsVersion"),
                     vm.str("config.tools.toolsUpgradePolicy"), vm.bool("config.tools.syncTimeWithHost"),
                     vm.bool("config.tools.afterPowerOn"), vm.bool("config.tools.afterResume"),
                     vm.str("guest.guestState"), vm.str("guest.guestId"), vm.str("guest.guestFamily"),
                     vm.str("guest.hostName"), ctx["host"], ctx["annotation"]])
    return rows


@sheet("vHost", "Inventory", [
    text_col("Host"), text_col("Cluster"), text_col("Config status"), bool_col("in Maintenance Mode"),
    text_col("Connection state"), text_col("Power state"), text_col("CPU Model"), num_col("Speed", NONE),
    bool_col("HT Available"), bool_col("HT Active"), num_col("# CPU"), num_col("Cores per CPU", NONE),
    num_col("# Cores"), num_col("# CPU Threads"), num_col("CPU usage %", AVG), num_col("# Memory GiB"),
    text_col("Memory Tiering Type"), num_col("DRAM GiB"), num_col("NVMe Tier GiB"), num_col("Memory usage %", AVG),
    num_col("# NICs"), num_col("# HBAs"), num_col("# VMs total"), num_col("# VMs"),
    num_col("VMs per Core", ratio_total("# VMs total", "# Cores")), num_col("# vCPUs"),
    num_col("vCPUs per Core", ratio_total("# vCPUs", "# Cores")), num_col("vRAM GiB"), bool_col("VMotion support"),
    bool_col("Storage VMotion support"), text_col("Current EVC"), text_col("Max EVC"),
    text_col("Current CPU power man. policy"), text_col("ESX Version"), text_col("Boot time"),
    text_col("DNS Servers"), bool_col("DHCP"), text_col("Domain"), text_col("DNS Search Order"),
    text_col("NTP Server(s)"), bool_col("NTPD running"), text_col("Time Zone"), num_col("GMT Offset", NONE),
    text_col("Vendor"), text_col("Model"), text_col("Serial number"), text_col("Service tag"),
    text_col("BIOS Version"), text_col("BIOS Date"), text_col("UUID"),
])
def v_host(d: VcenterData) -> list[list[Any]]:
    totals = d.vm_totals_by_host()
    cluster_of = {moref(m): c.str("name") for c in d.clusters() for m in c.array("host")}
    rows = []
    for h in d.hosts():
        name = h.str("name")
        if not name:
            raise ValueError(f"HostSystem {h.moref} returned no name property")
        cores, sockets = h.num("summary.hardware.numCpuCores"), h.num("summary.hardware.numCpuPkgs")
        mem, mhz = h.num("summary.hardware.memorySize"), h.num("summary.hardware.cpuMhz")
        tt = totals.get(h.moref, {"vmsTotal": 0, "vmsPoweredOn": 0, "vcpus": 0, "vramMiB": 0})
        used_mib = h.num("summary.quickStats.overallMemoryUsage")   # MiB, while memorySize is bytes
        total_mib = math.floor(mem / 1024 / 1024) if mem else None
        rows.append([
            name, cluster_of.get(h.moref), h.str("overallStatus"), h.bool("runtime.inMaintenanceMode"),
            h.str("runtime.connectionState"), h.str("runtime.powerState"), h.str("summary.hardware.cpuModel"), mhz,
            h.bool("config.hyperThread.available"), h.bool("config.hyperThread.active"), sockets,
            math.trunc(cores / sockets) if cores is not None and sockets else None, cores,
            h.num("summary.hardware.numCpuThreads"),
            # Host CPU capacity is cores x per-core MHz.
            percent(h.num("summary.quickStats.overallCpuUsage"),
                    cores * mhz if cores is not None and mhz is not None else None),
            gib(mem), memory_tiering_type(h), gib(memory_tier_bytes(h, "DRAM")), gib(memory_tier_bytes(h, "NVMe")),
            percent(used_mib, total_mib), h.num("summary.hardware.numNics"), h.num("summary.hardware.numHBAs"),
            tt["vmsTotal"], tt["vmsPoweredOn"], ratio(tt["vmsTotal"], cores), tt["vcpus"], ratio(tt["vcpus"], cores),
            round2(tt["vramMiB"] / 1024), h.bool("capability.vmotionSupported"),
            h.bool("capability.storageVMotionSupported"), h.str("summary.currentEVCModeKey"),
            h.str("summary.maxEVCModeKey"), h.str("hardware.cpuPowerManagementInfo.currentPolicy"),
            h.str("config.product.fullName"), h.str("runtime.bootTime"),
            string_array(h, "config.network.dnsConfig.address"), h.bool("config.network.dnsConfig.dhcp"),
            h.str("config.network.dnsConfig.domainName"), string_array(h, "config.network.dnsConfig.searchDomain"),
            string_array(h, "config.dateTimeInfo.ntpConfig.server"), service_running(h, "ntpd"),
            h.str("config.dateTimeInfo.timeZone.name"), h.num("config.dateTimeInfo.timeZone.gmtOffset"),
            h.str("summary.hardware.vendor"), h.str("summary.hardware.model"), h.str("hardware.systemInfo.serialNumber"),
            identifying_info(h, "ServiceTag"), h.str("hardware.biosInfo.biosVersion"),
            h.str("hardware.biosInfo.releaseDate"), h.str("summary.hardware.uuid"),
        ])
    return rows


def cluster_flags(c: Obj) -> tuple[bool | None, bool | None]:
    return c.bool("configuration.dasConfig.enabled"), c.bool("configuration.drsConfig.enabled")


@sheet("vCluster", "Inventory", [
    text_col("Name"), num_col("NumHosts"), num_col("NumCpuCores"), num_col("NumCpuThreads"), num_col("DRAM GiB"),
    num_col("TotalMemory GiB"), bool_col("HA enabled"), bool_col("DRS enabled"), num_col("# VMs"),
    num_col("# vCPUs"), num_col("vCPUs per Core", ratio_total("# vCPUs", "NumCpuCores")),
])
def v_cluster(d: VcenterData) -> list[list[Any]]:
    totals, by_moref, rows = d.vm_totals_by_host(), {h.moref: h for h in d.hosts()}, []
    for c in d.clusters():
        name = c.str("name")
        if not name:
            raise ValueError(f"ClusterComputeResource {c.moref} returned no name")
        members = [by_moref[moref(m)] for m in c.array("host") if moref(m) in by_moref]

        def total(fn: Callable[[Obj], Any]) -> float:
            return sum((fn(h) or 0) for h in members)
        cores = total(lambda h: h.num("summary.hardware.numCpuCores"))
        vcpus = total(lambda h: totals.get(h.moref, {}).get("vcpus"))
        ha, drs = cluster_flags(c)
        rows.append([
            name, len(members), cores, total(lambda h: h.num("summary.hardware.numCpuThreads")),
            gib(total(lambda h: memory_tier_bytes(h, "DRAM") or h.num("summary.hardware.memorySize"))),
            gib(total(lambda h: h.num("summary.hardware.memorySize"))), ha, drs,
            total(lambda h: totals.get(h.moref, {}).get("vmsTotal")), vcpus, ratio(vcpus, cores),
        ])
    return rows


@sheet("vRP", "Inventory", [
    text_col("Resource pool"), text_col("Cluster"), text_col("Status"), num_col("# VMs"),
    num_col("CPU limit MHz", NONE), num_col("CPU reservation MHz"), text_col("CPU level"), num_col("CPU shares", NONE),
    num_col("CPU usage MHz"), num_col("Memory limit MiB", NONE), num_col("Memory reservation MiB"),
    text_col("Memory level"), num_col("Memory shares", NONE), num_col("Memory consumed MiB"),
    num_col("Memory active MiB"),
])
def v_rp(d: VcenterData) -> list[list[Any]]:
    cluster_names = {c.moref: c.str("name") for c in d.clusters()}
    rows = []
    for p in d.resource_pools():
        name = p.str("name")
        if not name:
            raise ValueError(f"ResourcePool {p.moref} returned no name property")
        owner = p.str("owner")
        rows.append([
            name, cluster_names.get(owner, owner) if owner else None, p.str("overallStatus"), len(p.array("vm")),
            p.num("config.cpuAllocation.limit"), p.num("config.cpuAllocation.reservation"),
            p.str("config.cpuAllocation.shares.level"), p.num("config.cpuAllocation.shares.shares"),
            p.num("summary.quickStats.overallCpuUsage"), p.num("config.memoryAllocation.limit"),
            p.num("config.memoryAllocation.reservation"), p.str("config.memoryAllocation.shares.level"),
            p.num("config.memoryAllocation.shares.shares"), p.num("summary.quickStats.hostMemoryUsage"),
            p.num("summary.quickStats.guestMemoryUsage"),
        ])
    return rows


def natural_key(s: str) -> list[Any]:
    return [int(x) if x.isdigit() else x.lower() for x in re.split(r"(\d+)", s)]


@sheet("vDatastore", "Inventory", [
    text_col("Name"), text_col("Type"), bool_col("Accessible"), num_col("# VMs", NONE), num_col("Capacity GiB"),
    num_col("In Use GiB"), num_col("Free GiB"), num_col("Free %", ratio_total("Free GiB", "Capacity GiB", 100)),
    num_col("# Hosts", NONE), text_col("Hosts"),
])
def v_datastore(d: VcenterData) -> list[list[Any]]:
    host_names, rows = d.host_names(), []
    for ds in d.datastores():
        name = ds.str("name")
        if not name:
            raise ValueError(f"Datastore {ds.moref} returned no name property")
        cap, free = ds.num("summary.capacity"), ds.num("summary.freeSpace")
        mounts = [moref(at(m, "key")) for m in ds.array("host")]
        mounts = [m for m in mounts if m]
        rows.append([
            name, ds.str("summary.type"), ds.bool("summary.accessible"), len(ds.array("vm")), gib(cap),
            gib(cap - free) if cap is not None and free is not None else None, gib(free), percent(free, cap),
            len(mounts), ", ".join(sorted(host_names.get(m, m) for m in mounts)) or None,
        ])
    return sorted(rows, key=lambda r: natural_key(r[0]))


@sheet("vHealth", "Health", [text_col("Name"), text_col("Message"), text_col("Message type")])
def v_health(d: VcenterData) -> list[list[Any]]:
    """Computed from inventory, not alarms: NTP, NTPD, folder-name, CD-ROM and snapshot checks."""
    inventory, rows = d.vms_by_moref(), []
    for h in d.hosts():
        name = h.str("name")
        if not name:
            raise ValueError(f"HostSystem {h.moref} returned no name property")
        if all(not text(s) for s in h.array("config.dateTimeInfo.ntpConfig.server")):
            rows.append([name, "NTP Server value is null!", "NTP"])
        # A host that doesn't report the service at all isn't running it.
        if service_running(h, "ntpd") is not True:
            rows.append([name, "NTPD service is not running!", "NTPD"])
    for dev in d.vm_devices():
        name = dev.str("name")
        if not name:
            raise ValueError(f"VirtualMachine {dev.moref} returned no name property")
        if is_vcls(name):
            continue
        vm = inventory.get(dev.moref)
        path = vm.str("config.files.vmPathName") if vm else None
        folder = folder_of(path) if path else None
        if folder is not None and folder != name:   # case-sensitive, like the website
            rows.append([name, f"Inconsistent Foldername! VMname = {name} Foldername = {folder}", "Foldername"])
        for cd in (x for x in devices(dev) if xsi(x) == "VirtualCdrom"):
            if b(cd, "connectable/connected") is True:
                rows.append([name, f"VM has a CDROM device connected! {t(cd, 'deviceInfo/label') or ''}", "CDROM"])
        for snap in (s for root in dev.array("snapshot.rootSnapshotList") for s in flatten_snapshots(root)):
            rows.append([name, f"VM has an active snapshot! {snap['name'] or ''} created on "
                               f"{format_timestamp(snap['createTime'])}", "Snapshot"])
    return rows


@sheet("vHBA", "Host network & storage", [
    text_col("Host"), text_col("Device"), text_col("Type"), text_col("Status"), text_col("Model"), text_col("Driver"),
    text_col("Driver version"), text_col("Firmware"), text_col("PCI"), text_col("Protocol"),
    text_col("WWN / iSCSI name"),
])
def v_hba(d: VcenterData) -> list[list[Any]]:
    rows = []
    for h in d.hosts():
        for hba in h.array("config.storageDevice.hostBusAdapter"):
            # Fibre Channel reports world wide names as numbers; iSCSI has a name instead.
            pwwn = at(hba, "portWorldWideName")
            identity = wwn(pwwn) if pwwn is not None else t(hba, "iScsiName")
            rows.append([h.str("name"), t(hba, "device"), strip_prefix(xsi(hba), "Host"), t(hba, "status"),
                         t(hba, "model"), t(hba, "driver"), t(hba, "driverVersion"), t(hba, "firmwareVersion"),
                         t(hba, "pci"), t(hba, "storageProtocol"), identity])
    return rows


@sheet("vNIC", "Host network & storage", [
    text_col("Host"), text_col("Network Device"), text_col("Driver"), text_col("Driver version"),
    text_col("Firmware"), text_col("PCI"), num_col("Speed Mb", NONE), bool_col("Duplex"), text_col("MAC Address"),
    bool_col("Wake on LAN"),
])
def v_nic(d: VcenterData) -> list[list[Any]]:
    rows = []
    for h in d.hosts():
        for nic in h.array("config.network.pnic"):
            rows.append([h.str("name"), t(nic, "device"), t(nic, "driver"), t(nic, "driverVersion"),
                         t(nic, "firmwareVersion"), t(nic, "pci"), n(nic, "linkSpeed/speedMb"),
                         b(nic, "linkSpeed/duplex"), t(nic, "mac"), b(nic, "wakeOnLanSupported")])
    return rows


@sheet("vSwitch", "Host network & storage", [
    text_col("Host"), text_col("Switch"), num_col("# Ports"), num_col("Free Ports"), num_col("MTU", NONE),
    bool_col("Promiscuous Mode"), bool_col("Mac Changes"), bool_col("Forged Transmits"), text_col("Uplinks"),
])
def v_switch(d: VcenterData) -> list[list[Any]]:
    """Standard switches only. Hosts that use distributed switches have none."""
    rows = []
    for h in d.hosts():
        for sw in h.array("config.network.vswitch"):
            uplinks = [str(p).split("-")[-1] for p in (at(sw, "pnic") or []) if p]
            rows.append([h.str("name"), t(sw, "name"), n(sw, "numPorts"), n(sw, "numPortsAvailable"), n(sw, "mtu"),
                         b(sw, "spec/policy/security/allowPromiscuous"), b(sw, "spec/policy/security/macChanges"),
                         b(sw, "spec/policy/security/forgedTransmits"), ", ".join(uplinks) or None])
    return rows


def first_bool(*values: bool | None) -> bool | None:
    return next((v for v in values if v is not None), None)


@sheet("vPort", "Host network & storage", [
    text_col("Host"), text_col("Port Group"), text_col("Switch"), num_col("VLAN", NONE), bool_col("Promiscuous Mode"),
    bool_col("Mac Changes"), bool_col("Forged Transmits"),
])
def v_port(d: VcenterData) -> list[list[Any]]:
    """Port groups on standard switches. They usually inherit security from the switch;
    computedPolicy is what applies."""
    rows = []
    for h in d.hosts():
        for pg in h.array("config.network.portgroup"):
            rows.append([
                h.str("name"), t(pg, "spec/name"), t(pg, "spec/vswitchName"), n(pg, "spec/vlanId"),
                first_bool(b(pg, "spec/policy/security/allowPromiscuous"), b(pg, "computedPolicy/security/allowPromiscuous")),
                first_bool(b(pg, "spec/policy/security/macChanges"), b(pg, "computedPolicy/security/macChanges")),
                first_bool(b(pg, "spec/policy/security/forgedTransmits"), b(pg, "computedPolicy/security/forgedTransmits")),
            ])
    return rows


@sheet("vSC_VMK", "Host network & storage", [
    text_col("Host"), text_col("Device"), text_col("Port Group"), text_col("MAC Address"), bool_col("DHCP"),
    text_col("IP Address"), text_col("Subnet mask"), text_col("Gateway"), num_col("MTU", NONE), bool_col("TSO"),
    text_col("Stack"),
])
def v_sc_vmk(d: VcenterData) -> list[list[Any]]:
    """VMkernel ports: management, vMotion, storage and so on."""
    try:
        dv_names = {pg.str("key"): pg.str("name") for pg in d.dv_portgroups()}
    except Exception:
        dv_names = {}
    rows = []
    for h in d.hosts():
        for vmk in h.array("config.network.vnic"):
            # Standard switches name the port group directly; distributed ones give a key.
            dv_key = t(vmk, "spec/distributedVirtualPort/portgroupKey")
            rows.append([h.str("name"), t(vmk, "device"),
                         t(vmk, "portgroup") or (dv_names.get(dv_key, dv_key) if dv_key else None),
                         t(vmk, "spec/mac"), b(vmk, "spec/ip/dhcp"), t(vmk, "spec/ip/ipAddress"),
                         t(vmk, "spec/ip/subnetMask"), t(vmk, "spec/ipRouteSpec/ipRouteConfig/defaultGateway"),
                         n(vmk, "spec/mtu"), b(vmk, "spec/tsoEnabled"), t(vmk, "spec/netStackInstanceKey")])
    return rows


@sheet("dvSwitch", "Distributed switch", [
    text_col("Switch"), text_col("Vendor"), text_col("Version"), num_col("# Ports"), num_col("Max Ports"),
    num_col("# Hosts", NONE), text_col("Created"), text_col("UUID"),
])
def dv_switch(d: VcenterData) -> list[list[Any]]:
    rows = []
    for sw in d.dv_switches():
        product = sw.get("summary.productInfo")
        rows.append([sw.str("name"), t(product, "vendor"), t(product, "version"), sw.num("summary.numPorts"),
                     sw.num("config.maxPorts"), len(sw.array("summary.hostMember")), sw.str("config.createTime"),
                     sw.str("uuid")])
    return rows


@sheet("dvPort", "Distributed switch", [
    text_col("Port Group"), text_col("Switch"), num_col("# Ports"), text_col("Type"), text_col("VLAN"),
    bool_col("Promiscuous Mode"), bool_col("Mac Changes"), bool_col("Forged Transmits"),
])
def dv_port(d: VcenterData) -> list[list[Any]]:
    switch_names = {sw.moref: sw.str("name") for sw in d.dv_switches()}
    rows = []
    for pg in d.dv_portgroups():
        config, sw = pg.get("config.defaultPortConfig"), pg.str("config.distributedVirtualSwitch")
        rows.append([pg.str("name"), switch_names.get(sw, sw) if sw else None, pg.num("config.numPorts"),
                     pg.str("config.type"), dv_vlan(config), dv_bool(config, "securityPolicy/allowPromiscuous"),
                     dv_bool(config, "securityPolicy/macChanges"), dv_bool(config, "securityPolicy/forgedTransmits")])
    return rows


def hundredths(v: Any) -> float | None:
    """vCenter reports percentages in hundredths of a percent."""
    return None if v is None else round2(v / 100)


@sheet("vHost Performance", "Performance", [
    text_col("Host"), num_col("CPU Usage (%)", AVG), num_col("CPU Usage MHz"), num_col("Memory Usage (%)", AVG),
    num_col("Memory Consumed GiB"), num_col("Network KBps"), num_col("Disk KBps"), text_col("Sampled"),
])
def v_host_performance(d: VcenterData) -> list[list[Any]]:
    """Live counters from vCenter, newest sample per host. Only connected hosts report."""
    hosts = d.hosts()
    connected = [h for h in hosts if h.str("runtime.connectionState") == "connected"]
    samples = d.perf_samples("hostPerf", connected, HOST_COUNTERS)
    rows = []
    for h in hosts:
        s = samples.get(h.moref, {})
        consumed = s.get("memoryConsumed")
        rows.append([h.str("name"), hundredths(s.get("cpuPercent")), s.get("cpuMhz"), hundredths(s.get("memoryPercent")),
                     None if consumed is None else round2(consumed / 1024 / 1024), s.get("network"), s.get("disk"),
                     s.get("sampledAt")])
    return rows


@sheet("vInfo Performance", "Performance", [
    text_col("VM"), text_col("Powerstate"), num_col("CPU Usage (%)", AVG), num_col("CPU Usage MHz"),
    num_col("Memory Usage (%)", AVG), num_col("Memory Active MiB"), num_col("Memory Consumed MiB"),
    num_col("Network KBps"), num_col("Disk KBps"), text_col("Host"), text_col("Sampled"),
])
def v_info_performance(d: VcenterData) -> list[list[Any]]:
    hosts = d.host_names()
    listed = [(vm, ctx) for vm in d.vm_inventory() if (ctx := vm_context(vm, hosts))]
    running = [vm for vm, ctx in listed if ctx["powerState"] == "poweredOn"]   # powered-off VMs have no counters
    samples = d.perf_samples("vmPerf", running, VM_COUNTERS)
    rows = []
    for vm, ctx in listed:
        s = samples.get(vm.moref, {})
        active, consumed = s.get("memoryActive"), s.get("memoryConsumed")
        rows.append([ctx["name"], ctx["powerState"], hundredths(s.get("cpuPercent")), s.get("cpuMhz"),
                     hundredths(s.get("memoryPercent")), None if active is None else round2(active / 1024),
                     None if consumed is None else round2(consumed / 1024), s.get("network"), s.get("disk"),
                     ctx["host"], s.get("sampledAt")])
    return rows


@sheet("vSource", "System", [
    text_col("Name"), text_col("Full name"), text_col("Vendor"), text_col("Version"), text_col("Build"),
    text_col("API version"), text_col("API type"), text_col("OS type"), text_col("Instance UUID"),
    text_col("License product"),
])
def v_source(d: VcenterData) -> list[list[Any]]:
    a = d.about()
    if a is None:
        return []
    return [[t(a, "name"), t(a, "fullName"), t(a, "vendor"), t(a, "version"), t(a, "build"), t(a, "apiVersion"),
             t(a, "apiType"), t(a, "osType"), t(a, "instanceUuid"), t(a, "licenseProductName")]]


@sheet("vLicense", "System", [
    text_col("Name"), text_col("Key"), text_col("Edition"), text_col("Cost unit"), num_col("Total", NONE),
    num_col("Used", NONE), num_col("Available", NONE), text_col("Expires"),
])
def v_license(d: VcenterData) -> list[list[Any]]:
    rows = []
    for lic in d.licenses():
        total, used = n(lic, "total"), n(lic, "used")
        props = {t(p, "key"): at(p, "value") for p in (at(lic, "properties") or [])}
        expiry = props.get("expirationDate", props.get("ExpirationDate"))
        rows.append([t(lic, "name"), t(lic, "licenseKey"), t(lic, "editionKey"), t(lic, "costUnit"), total, used,
                     total - used if total is not None and used is not None else None,
                     text(expiry) if expiry is not None else "Never"])
    return rows


@sheet("vMetaData", "System", [
    text_col("Tool"), text_col("Tool version"), text_col("Collected"), text_col("vCenter"),
    text_col("vCenter version"), text_col("Username"),
])
def v_metadata(d: VcenterData) -> list[list[Any]]:
    a = d.about()
    return [["DBH Insights", f"{DBH_VERSION} (vCommunity-Observability module {MODULE_VERSION})",
             datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z", d.server,
             f"{t(a, 'version')} build {t(a, 'build')}" if a is not None else None, d.username]]


@sheet("Cluster Capacity", "Overview", [
    text_col("Cluster"), bool_col("HA enabled"), bool_col("DRS enabled"), num_col("# Hosts"),
    num_col("# Hosts available"), num_col("# Cores"), num_col("DRAM GiB"), num_col("# VMs"), num_col("# Powered on"),
    num_col("# Powered off"), num_col("# Templates"), num_col("# vCPUs"), num_col("Running vCPUs"),
    num_col("vCPUs per Core", ratio_total("# vCPUs", "# Cores")),
    num_col("Running vCPUs per Core", ratio_total("Running vCPUs", "# Cores")), num_col("vRAM GiB"),
    num_col("Running vRAM GiB"), num_col("Running vRAM % of DRAM", ratio_total("Running vRAM GiB", "DRAM GiB", 100)),
    bool_col("N+1 memory (est.)"), num_col("VM Provisioned GiB"), num_col("VM In Use GiB"),
    # Datastores are often shared between clusters, so adding them up would count them twice.
    num_col("# Datastores", NONE), num_col("Datastore Capacity GiB", NONE), num_col("Datastore Free GiB", NONE),
    num_col("Datastore Free %", NONE),
])
def cluster_capacity(d: VcenterData) -> list[list[Any]]:
    """Capacity per cluster: what the hosts provide against what the VMs are assigned."""
    hosts, vms, datastores = d.hosts(), d.vm_inventory(), d.datastores()
    by_moref = {h.moref: h for h in hosts}
    groups = []
    for c in d.clusters():
        name = c.str("name")
        if not name:
            raise ValueError(f"ClusterComputeResource {c.moref} returned no name")
        ha, drs = cluster_flags(c)
        groups.append({"name": name, "ha": ha, "drs": drs,
                       "hosts": [by_moref[moref(m)] for m in c.array("host") if moref(m) in by_moref]})
    clustered = {h.moref for g in groups for h in g["hosts"]}
    standalone = [h for h in hosts if h.moref not in clustered]
    if standalone:
        groups.append({"name": "(standalone hosts)", "ha": None, "drs": None, "hosts": standalone})

    def dram_gib(h: Obj) -> float:
        return gib(memory_tier_bytes(h, "DRAM") or h.num("summary.hardware.memorySize")) or 0

    rows = []
    for g in groups:
        morefs = {h.moref for h in g["hosts"]}
        available = [h for h in g["hosts"]
                     if h.str("runtime.connectionState") == "connected" and not h.bool("runtime.inMaintenanceMode")]
        cores = sum(h.num("summary.hardware.numCpuCores") or 0 for h in g["hosts"])
        dram = round2(sum(dram_gib(h) for h in g["hosts"]))
        mine = [vm for vm in vms if vm.str("name") and not is_vcls(vm.str("name")) and vm.str("runtime.host") in morefs]
        templates = [vm for vm in mine if vm.bool("config.template")]
        real = [vm for vm in mine if not vm.bool("config.template")]
        running = [vm for vm in real if vm.str("runtime.powerState") == "poweredOn"]

        def vcpus(lst: list[Obj]) -> float:
            return sum(vm.num("config.hardware.numCPU") or 0 for vm in lst)

        def vram(lst: list[Obj]) -> float:
            return round2(sum(vm.num("config.hardware.memoryMB") or 0 for vm in lst) / 1024)
        running_vram = vram(running)
        # N+1: could the available hosts, minus the biggest, hold the running VMs' assigned memory?
        avail_dram = [dram_gib(h) for h in available]
        survivors = sum(avail_dram) - max([0, *avail_dram])
        n_plus_one = survivors >= running_vram if len(available) >= 2 else False
        committed = sum(vm.num("summary.storage.committed") or 0 for vm in real)
        uncommitted = sum(vm.num("summary.storage.uncommitted") or 0 for vm in real)
        mounted = [ds for ds in datastores if any(moref(at(m, "key")) in morefs for m in ds.array("host"))]
        ds_cap = gib(sum(ds.num("summary.capacity") or 0 for ds in mounted))
        ds_free = gib(sum(ds.num("summary.freeSpace") or 0 for ds in mounted))
        rows.append([
            g["name"], g["ha"], g["drs"], len(g["hosts"]), len(available), cores, dram, len(real), len(running),
            len(real) - len(running), len(templates), vcpus(real), vcpus(running), ratio(vcpus(real), cores),
            ratio(vcpus(running), cores), vram(real), running_vram, percent(running_vram, dram), n_plus_one,
            gib(committed + uncommitted), gib(committed), len(mounted), ds_cap, ds_free, percent(ds_free, ds_cap),
        ])
    return rows


# --------------------------------------------------------------------------- build

def all_columns(s: dict[str, Any]) -> list[tuple[str, str, str]]:
    return [*s["columns"], text_col(VI_SDK_SERVER)]


# Columns the API and dashboards filter or sort on (big inventories have thousands of rows).
INDEXES = {"vinfo": [["powerstate"]], "vdatastore": [["free_pct"]], "vpartition": [["free_pct"]]}


def manifest_tables() -> dict[str, dict[str, Any]]:
    """The `tables:` section of module.yaml, generated from the sheet definitions."""
    kinds = {"text": "text", "number": "real", "bool": "integer"}
    out = {}
    for s in SHEETS:
        tname = table_name(s["name"])
        out[tname] = ({"indexes": INDEXES[tname]} if tname in INDEXES else {}) | \
            {"columns": {column_name(lbl): kinds[k] for lbl, k, _ in all_columns(s)}}
    return out


def store_value(kind: str, value: Any) -> Any:
    if value is None:
        return None
    if kind == "bool":
        return int(bool(value))
    if kind == "number":
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    return value if isinstance(value, str) else str(value)


def collect(d: VcenterData) -> dict[str, list[dict[str, Any]]]:
    """Build every sheet. A sheet that fails is recorded and the others still load."""
    out: dict[str, list[dict[str, Any]]] = {"sheets": [], "sheet_columns": []}
    for position, s in enumerate(SHEETS):
        tname, cols = table_name(s["name"]), all_columns(s)
        names = [column_name(lbl) for lbl, _, _ in cols]
        error = None
        try:
            rows = [[*r, d.server] for r in s["rows"](d)]
        except Exception as exc:   # one sheet must not stop the others
            rows, error = [], f"{type(exc).__name__}: {exc}"
        out[tname] = [{names[i]: store_value(cols[i][1], r[i]) for i in range(len(cols))} for r in rows]
        out["sheets"].append({"position": position, "sheet": s["name"], "table_name": tname, "grp": s["group"],
                              "row_count": len(rows), "error": error})
        for i, (lbl, kind, total) in enumerate(cols):
            out["sheet_columns"].append({"sheet": s["name"], "table_name": tname, "position": i,
                                         "column_name": names[i], "label": lbl, "kind": kind, "total": total})
    return out


if __name__ == "__main__":
    import sys
    import yaml

    if "--tables" in sys.argv:
        print(yaml.safe_dump({"tables": manifest_tables()}, sort_keys=False, width=200))
