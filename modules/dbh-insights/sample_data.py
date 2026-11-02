"""Sample vCenter for VCO_MOCK=1, shaped like a small home lab.

Objects mimic what pyVmomi returns (attribute access, vSphere type names), so the
sheet code runs unchanged and the whole module can be tried without a vCenter.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from vsphere import Obj

_classes: dict[str, type] = {}


def D(wsdl: str, **attrs: Any) -> Any:
    """A data object of vSphere type `wsdl`."""
    cls = _classes.get(wsdl)
    if cls is None:
        cls = _classes[wsdl] = type(wsdl, (), {"_wsdlName": wsdl})
    obj = cls()
    obj.__dict__.update(attrs)
    return obj


def R(moid: str) -> Any:
    """A managed object reference, as pyVmomi returns it (plain string without pyVmomi)."""
    try:
        from pyVmomi import vim
    except ImportError:
        return moid
    kinds = {"host": vim.HostSystem, "vm": vim.VirtualMachine, "domain": vim.ClusterComputeResource,
             "datastore": vim.Datastore, "resgroup": vim.ResourcePool, "dvs": vim.dvs.VmwareDistributedVirtualSwitch,
             "snapshot": vim.vm.Snapshot, "dvportgroup": vim.dvs.DistributedVirtualPortgroup}
    return kinds[moid.split("-")[0]](moid)


def RL(ids: list[str]) -> list[Any]:
    return [R(i) for i in ids]


NOW = datetime(2026, 11, 1, 18, 0, tzinfo=timezone.utc)
GiB = 1024 ** 3


class SampleSource:
    def __init__(self, server: str = "vcsa91.vcrocs.local"):
        self.server = server
        self._build()

    # Same interface as vsphere.LiveSource ---------------------------------------
    def retrieve(self, type_name: str, props: list[str]) -> list[Obj]:
        objects = self._objects.get(type_name, [])
        return [Obj(o.ref, {p: o.props[p] for p in props if p in o.props}) for o in objects]

    def about(self) -> Any:
        return D("AboutInfo", name="VMware vCenter Server", fullName="VMware vCenter Server 9.0.1 build 24957454",
                 vendor="VMware, Inc.", version="9.0.1", build="24957454", apiVersion="9.0.1.0", apiType="VirtualCenter",
                 osType="linux-x64", instanceUuid="7c8f2c1e-5b1a-4c55-9e9b-3f1d2e6a9b10",
                 licenseProductName="VMware VirtualCenter Server")

    def licenses(self) -> list[Any]:
        return [
            D("LicenseManagerLicenseInfo", name="VMware Cloud Foundation", licenseKey="XXXXX-XXXXX-XXXXX-XXXXX-4V2J1",
              editionKey="vcf.cores", costUnit="core", total=128, used=24,
              properties=[D("KeyAnyValue", key="expirationDate", value=datetime(2027, 3, 31, tzinfo=timezone.utc))]),
            D("LicenseManagerLicenseInfo", name="vCenter Server Standard", licenseKey="XXXXX-XXXXX-XXXXX-XXXXX-0H8K2",
              editionKey="vc.standard.instance", costUnit="server", total=1, used=1, properties=[]),
        ]

    def perf_samples(self, objects: list[Obj], wanted: dict[str, str]) -> dict[str, dict[str, Any]]:
        stamp = (NOW - timedelta(seconds=20)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        out = {}
        for i, o in enumerate(objects):
            host = o.moref.startswith("host-")
            out[o.moref] = {
                "cpuPercent": (3100 + i * 1450) if host else (180 + i * 97) % 6000,
                "cpuMhz": (5700 + i * 2100) if host else 90 + i * 41,
                "memoryPercent": (6400 + i * 900) if host else (1500 + i * 230) % 9000,
                "memoryConsumed": (170 + i * 20) * 1024 * 1024 if host else (2048 + i * 300) * 1024,
                "memoryActive": (600 + i * 53) * 1024,
                "network": (2400 + i * 610) if host else 12 + i * 7,
                "disk": (5100 + i * 830) if host else 40 + i * 11,
                "sampledAt": stamp,
            }
        return {k: {c: v[c] for c in [*wanted, "sampledAt"] if c in v} for k, v in out.items()}

    # Inventory --------------------------------------------------------------------
    def _build(self) -> None:
        hosts_def = [
            # moref, name, cluster, maint, cpu used MHz, mem used MiB, NVMe tier
            ("host-1001", "esx9-01.vcrocs.local", "domain-c10", False, 9100, 212000, True),
            ("host-1002", "esx9-02.vcrocs.local", "domain-c20", False, 7400, 171000, False),
            ("host-1003", "esx9-03.vcrocs.local", "domain-c20", False, 11800, 240000, False),
        ]
        hosts = []
        for i, (mid, name, _cl, maint, cpu, mem, nvme) in enumerate(hosts_def):
            dram = 256 * GiB
            tiers = [D("HostMemoryTierInfo", type="DRAM", size=dram)]
            if nvme:
                tiers.append(D("HostMemoryTierInfo", type="NVMe", size=256 * GiB))
            pnics = [D("PhysicalNic", device=f"vmnic{j}", driver="ixgben" if j < 2 else "i40en",
                       driverVersion="1.15.1.0" if j < 2 else "2.6.1.0", firmwareVersion="0x80000d67",
                       pci=f"0000:{65 + j:02x}:00.0", mac=f"ac:1f:6b:2c:{i:02x}:{j:02x}",
                       linkSpeed=D("PhysicalNicLinkInfo", speedMb=10000, duplex=True) if j != 3 else None,
                       wakeOnLanSupported=j < 2) for j in range(4)]
            hbas = [D("HostBlockHba", device="vmhba0", status="online", model="Lewisburg SATA AHCI Controller",
                      driver="vmw_ahci", driverVersion="2.0.17", pci="0000:00:17.0", storageProtocol="scsi"),
                    D("HostInternetScsiHba", device="vmhba64", status="online", model="iSCSI Software Adapter",
                      driver="iscsi_vmk", driverVersion="1.0.0", iScsiName=f"iqn.1998-01.com.vmware:esx9-0{i + 1}-4f1a2c3b",
                      storageProtocol="scsi")]
            if i == 0:
                hbas.append(D("HostFibreChannelHba", device="vmhba2", status="link-down",
                              model="QLogic QLE2692 16Gb FC", driver="qlnativefc", driverVersion="5.4.81.2",
                              firmwareVersion="9.10.00", pci="0000:3b:00.0", storageProtocol="fcp",
                              portWorldWideName=2377900603251621000 + i))
            sec = D("HostNetworkSecurityPolicy", allowPromiscuous=False, macChanges=False, forgedTransmits=False)
            vswitch = D("HostVirtualSwitch", name="vSwitch0", numPorts=2560, numPortsAvailable=2549, mtu=1500,
                        pnic=["key-vim.host.PhysicalNic-vmnic0", "key-vim.host.PhysicalNic-vmnic1"],
                        spec=D("HostVirtualSwitchSpec", policy=D("HostNetworkPolicy", security=sec)))
            portgroups = [
                D("HostPortGroup", spec=D("HostPortGroupSpec", name="Management Network", vswitchName="vSwitch0",
                                          vlanId=0, policy=D("HostNetworkPolicy", security=None)),
                  computedPolicy=D("HostNetworkPolicy", security=sec)),
                D("HostPortGroup", spec=D("HostPortGroupSpec", name="VM Network", vswitchName="vSwitch0", vlanId=100,
                                          policy=D("HostNetworkPolicy", security=D("HostNetworkSecurityPolicy",
                                                                                    allowPromiscuous=True))),
                  computedPolicy=D("HostNetworkPolicy", security=sec)),
            ]
            vnics = [
                D("HostVirtualNic", device="vmk0", portgroup="Management Network",
                  spec=D("HostVirtualNicSpec", mac=f"ac:1f:6b:2c:{i:02x}:00", mtu=1500, tsoEnabled=True,
                         netStackInstanceKey="defaultTcpipStack",
                         ip=D("HostIpConfig", dhcp=False, ipAddress=f"192.168.6.{101 + i}", subnetMask="255.255.255.0"),
                         ipRouteSpec=D("HostVirtualNicIpRouteSpec", ipRouteConfig=D("HostIpRouteConfig",
                                                                                    defaultGateway="192.168.6.1")))),
                D("HostVirtualNic", device="vmk1", portgroup="",
                  spec=D("HostVirtualNicSpec", mac=f"00:50:56:6a:{i:02x}:01", mtu=9000, tsoEnabled=True,
                         netStackInstanceKey="vmotion",
                         distributedVirtualPort=D("DistributedVirtualSwitchPortConnection", portgroupKey="dvportgroup-31"),
                         ip=D("HostIpConfig", dhcp=False, ipAddress=f"172.16.10.{11 + i}", subnetMask="255.255.255.0"))),
            ]
            ntp = ["pool.ntp.org"] if i != 2 else []
            hosts.append(Obj(R(mid), {
                "name": name, "overallStatus": "red" if i == 2 else "green",
                "runtime.connectionState": "connected", "runtime.powerState": "poweredOn",
                "runtime.inMaintenanceMode": maint, "runtime.bootTime": NOW - timedelta(days=41 if i < 2 else 3, hours=5),
                "summary.hardware.vendor": "Supermicro", "summary.hardware.model": "SYS-E300-9D-8CN8TP",
                "summary.hardware.cpuModel": "Intel(R) Xeon(R) D-2146NT CPU @ 2.30GHz", "summary.hardware.cpuMhz": 2300,
                "summary.hardware.numCpuPkgs": 1, "summary.hardware.numCpuCores": 8,
                "summary.hardware.numCpuThreads": 16, "summary.hardware.memorySize": dram * (2 if nvme else 1),
                "hardware.memoryTierInfo": tiers, "summary.hardware.numNics": 4, "summary.hardware.numHBAs": len(hbas),
                "summary.hardware.uuid": f"00000000-0000-0000-0000-ac1f6b2c00{i:02x}",
                "summary.quickStats.overallCpuUsage": cpu, "summary.quickStats.overallMemoryUsage": mem,
                "summary.currentEVCModeKey": "intel-skylake", "summary.maxEVCModeKey": "intel-skylake",
                "capability.vmotionSupported": True, "capability.storageVMotionSupported": True,
                "config.product.fullName": "VMware ESXi 9.0.1 build-24957456",
                "config.hyperThread.available": True, "config.hyperThread.active": True,
                "config.network.dnsConfig.domainName": "vcrocs.local",
                "config.network.dnsConfig.address": ["192.168.6.2", "192.168.6.3"],
                "config.network.dnsConfig.searchDomain": ["vcrocs.local"], "config.network.dnsConfig.dhcp": False,
                "config.dateTimeInfo.timeZone.name": "UTC", "config.dateTimeInfo.timeZone.gmtOffset": 0,
                "config.dateTimeInfo.ntpConfig.server": ntp,
                "config.service.service": [D("HostService", key="ntpd", running=i != 2),
                                           D("HostService", key="TSM-SSH", running=False)],
                "hardware.biosInfo.biosVersion": "2.6", "hardware.biosInfo.releaseDate": datetime(2024, 4, 9, tzinfo=timezone.utc),
                "hardware.cpuPowerManagementInfo.currentPolicy": "Balanced",
                "hardware.systemInfo.serialNumber": f"S41{i}8765X",
                "hardware.systemInfo.otherIdentifyingInfo": [
                    D("HostSystemIdentificationInfo", identifierValue=f"SVT-{i}A7", identifierType=D("ElementDescription", key="ServiceTag")),
                ],
                "config.storageDevice.hostBusAdapter": hbas, "config.network.pnic": pnics, "config.network.vnic": vnics,
                "config.network.vswitch": [vswitch], "config.network.portgroup": portgroups,
            }))

        clusters = [
            Obj(R("domain-c10"), {"name": "CL-01", "host": RL(["host-1001"]), "configuration.dasConfig.enabled": False,
                               "configuration.drsConfig.enabled": True}),
            Obj(R("domain-c20"), {"name": "CL-02", "host": RL(["host-1002", "host-1003"]),
                               "configuration.dasConfig.enabled": True, "configuration.drsConfig.enabled": True}),
        ]

        datastores_def = [
            ("datastore-51", "SYN-HDD", "NFS", 12288, 5120, ["host-1001", "host-1002", "host-1003"]),
            ("datastore-52", "SYN-NVMe", "NFS41", 3584, 610, ["host-1001", "host-1002", "host-1003"]),
            ("datastore-53", "vsanDatastore", "vsan", 6144, 2950, ["host-1002", "host-1003"]),
            ("datastore-54", "esx9-01-local", "VMFS", 931, 512, ["host-1001"]),
        ]

        vm_names_cl01 = ["collector", "DC-02", "lic91", "minion01", "minion02", "MPB2", "offDepot", "OPS91",
                         "POWERSHELL-HOST", "ROCKY-Postgres", "SaltMaster", "TWINGATE", "UniFi",
                         "VCF-Installer-911", "vcf91installer", "vcf91sddc", "veeam-01"]
        vm_names_cl02 = ["nsx02", "vcsa91", "vsp01-b2hr2", "vsp01-c2td7", "vsp01-lhkgg", "vsp01-lzfms",
                         "DC-03", "TWINGATE-02", "ubuntu-template"]
        vms, vms_by_ds, vm_on_host1 = [], {d[0]: [] for d in datastores_def}, set()
        all_names = vm_names_cl01 + vm_names_cl02 + ["vCLS-6a1f2b3c"]
        for i, name in enumerate(all_names):
            mid = f"vm-{2000 + i}"
            vcls = name.startswith("vCLS-")
            template = name == "ubuntu-template"
            host = "host-1001" if name in vm_names_cl01 else ("host-1002" if i % 2 else "host-1003")
            windows = name.startswith(("DC-", "veeam", "POWERSHELL", "lic"))
            on = not template and name not in ("offDepot", "VCF-Installer-911", "minion02")
            cpus = 4 if i % 3 else 2
            mem_mb = [4, 8, 16, 12][i % 4] * 1024
            disk_gb = [40, 60, 100, 250][i % 4]
            ds_id, ds_name = ("datastore-53", "vsanDatastore") if host != "host-1001" and i % 3 == 0 else ("datastore-51", "SYN-HDD")
            vms_by_ds[ds_id].append(mid)
            folder = name if name != "MPB2" else "mpb2"          # one inconsistent folder for vHealth
            vmx = f"[{ds_name}] {folder}/{name}.vmx"
            committed = int((disk_gb * 0.4 + i) * GiB)
            uncommitted = int(disk_gb * GiB + mem_mb * 1024 * 1024) - committed
            guest_full = "Microsoft Windows Server 2022 (64-bit)" if windows else "Ubuntu Linux (64-bit)"
            ip = f"192.168.100.{40 + i}"
            snap_tree = None
            if name in ("UniFi", "SaltMaster"):
                created = NOW - timedelta(days=12 + i)
                child = D("VirtualMachineSnapshotTree", snapshot=R(f"snapshot-{i}2"), name="after-patch",
                          description="", createTime=created + timedelta(days=2), state="poweredOn", quiesced=True,
                          childSnapshotList=[])
                snap_tree = [D("VirtualMachineSnapshotTree", snapshot=R(f"snapshot-{i}1"), name="before-upgrade",
                               description="Before the controller upgrade" if name == "UniFi" else "",
                               createTime=created, state="poweredOff", quiesced=False,
                               childSnapshotList=[child] if name == "UniFi" else [])]
            disk_backing = D("VirtualDiskFlatVer2BackingInfo", fileName=f"[{ds_name}] {folder}/{name}.vmdk",
                             diskMode="persistent", sharing="sharingNone", thinProvisioned=i % 4 != 2,
                             eagerlyScrub=False if i % 4 != 2 else True, split=False, writeThrough=False,
                             uuid=f"6000C29{i:01x}-{i:04x}-4a1b-9c2d-0e1f2a3b4c{i:02x}")
            devices = [
                D("VirtualIDEController", key=200, deviceInfo=D("Description", label="IDE 0", summary="IDE 0")),
                D("ParaVirtualSCSIController", key=1000, deviceInfo=D("Description", label="SCSI controller 0",
                                                                     summary="VMware paravirtual SCSI")),
                D("VirtualDisk", key=2000, controllerKey=1000, unitNumber=0, capacityInKB=disk_gb * 1024 * 1024,
                  deviceInfo=D("Description", label="Hard disk 1", summary=f"{disk_gb * 1024 * 1024:,} KB"),
                  backing=disk_backing,
                  storageIOAllocation=D("StorageIOAllocationInfo", limit=-1, reservation=0,
                                        shares=D("SharesInfo", level="normal", shares=1000))),
                D("VirtualVmxnet3" if not windows else "VirtualE1000e", key=4000, macAddress=f"00:50:56:af:{i:02x}:97",
                  addressType="assigned", deviceInfo=D("Description", label="Network adapter 1", summary="VM Network"),
                  backing=D("VirtualEthernetCardDistributedVirtualPortBackingInfo",
                            port=D("DistributedVirtualSwitchPortConnection", portgroupKey="dvportgroup-30"))
                  if host != "host-1001" else D("VirtualEthernetCardNetworkBackingInfo", deviceName="VM Network"),
                  connectable=D("VirtualDeviceConnectInfo", connected=on, startConnected=True, allowGuestControl=True)),
                D("VirtualCdrom", key=3000, controllerKey=200, unitNumber=0,
                  deviceInfo=D("Description", label="CD/DVD drive 1", summary="Remote device"),
                  backing=D("VirtualCdromIsoBackingInfo", fileName="[SYN-HDD] ISO/ubuntu-24.04.iso")
                  if i % 5 == 0 else D("VirtualCdromRemotePassthroughBackingInfo", deviceName=""),
                  connectable=D("VirtualDeviceConnectInfo", connected=on and i % 5 == 0, startConnected=i % 5 == 0,
                                allowGuestControl=True)),
            ]
            if i % 4 == 1:
                devices.append(D("VirtualUSBXHCIController", key=14000, autoConnectDevices=True,
                                 deviceInfo=D("Description", label="USB xHCI controller ", summary="Auto connect Enabled")))
            if i == 6:
                devices.append(D("VirtualDisk", key=2001, controllerKey=1000, unitNumber=1, capacityInKB=500 * 1024 * 1024,
                                 deviceInfo=D("Description", label="Hard disk 2", summary="500 GB"),
                                 backing=D("VirtualDiskRawDiskMappingVer1BackingInfo",
                                           fileName=f"[{ds_name}] {name}/{name}_1.vmdk", lunUuid="0200000000600a0980",
                                           compatibilityMode="physicalMode", diskMode="independent_persistent",
                                           sharing="sharingNone"),
                                 storageIOAllocation=D("StorageIOAllocationInfo", limit=-1, reservation=0,
                                                       shares=D("SharesInfo", level="normal", shares=1000))))
            layout_files = [D("VirtualMachineFileLayoutExFileInfo", key=0, name=vmx, size=3400)]
            layout_snaps = []
            if snap_tree:
                for k, sid in enumerate([f"snapshot-{i}1", f"snapshot-{i}2"]):
                    layout_files.append(D("VirtualMachineFileLayoutExFileInfo", key=10 + k * 2,
                                          name=f"[{ds_name}] {folder}/{name}-Snapshot{k + 1}.vmsn", size=48_000 + k * 9000))
                    layout_files.append(D("VirtualMachineFileLayoutExFileInfo", key=11 + k * 2,
                                          name=f"[{ds_name}] {folder}/{name}-Snapshot{k + 1}.vmem", size=mem_mb * 1024 * 1024))
                    layout_snaps.append(D("VirtualMachineFileLayoutExSnapshotLayout", key=R(sid), dataKey=10 + k * 2,
                                          memoryKey=11 + k * 2 if k == 1 else -1))
            guest_disks = []
            if on and not vcls:
                for path, cap in ((("C:\\", disk_gb),) if windows else (("/", disk_gb * 0.8), ("/boot", 1.8))):
                    cap_b = int(cap * GiB)
                    guest_disks.append(D("GuestDiskInfo", diskPath=path, capacity=cap_b,
                                         freeSpace=int(cap_b * (0.08 if name == "ROCKY-Postgres" else 0.35 + (i % 5) / 20))))
            props = {
                "name": name, "config.template": template, "runtime.powerState": "poweredOn" if on else "poweredOff",
                "runtime.host": R(host), "guest.ipAddress": ip if on else None, "guest.hostName": name.lower() if on else None,
                "guest.guestFullName": guest_full if on else None, "config.guestFullName": guest_full,
                "guest.toolsVersionStatus": "guestToolsCurrent" if i % 7 else "guestToolsNeedUpgrade",
                "guest.toolsRunningStatus": "guestToolsRunning" if on else "guestToolsNotRunning",
                "config.createDate": NOW - timedelta(days=200 - i * 6), "config.version": "vmx-22",
                "config.annotation": "Home lab network controller" if name == "UniFi" else "",
                "config.firmware": "efi" if windows or i % 2 else "bios",
                "config.bootOptions.efiSecureBootEnabled": windows,
                "config.hardware.numCPU": 1 if vcls else cpus, "config.hardware.numCoresPerSocket": 1 if vcls else cpus // 2 or 1,
                "config.hardware.memoryMB": 128 if vcls else mem_mb, "config.files.vmPathName": vmx,
                "config.changeVersion": f"2026-10-{1 + i % 28:02d}T12:00:00.000000Z",
                "config.uuid": f"4211{i:04x}-aaaa-bbbb-cccc-0000000{i:05x}",
                "summary.storage.committed": committed, "summary.storage.uncommitted": uncommitted,
                "summary.quickStats.overallCpuUsage": (150 + i * 37) if on else 0,
                "summary.runtime.maxCpuUsage": cpus * 2300, "summary.quickStats.guestMemoryUsage": (600 + i * 53) if on else 0,
                "summary.config.memorySizeMB": mem_mb,
                "config.cpuAllocation.shares.level": "normal", "config.cpuAllocation.shares.shares": cpus * 1000,
                "config.cpuAllocation.reservation": 0 if name != "vcsa91" else 2000, "config.cpuAllocation.limit": -1,
                "config.cpuHotAddEnabled": i % 3 == 0, "config.cpuHotRemoveEnabled": False,
                "summary.quickStats.staticCpuEntitlement": cpus * 2300 if on else 0,
                "summary.quickStats.distributedCpuEntitlement": (150 + i * 37) if on else 0,
                "config.memoryAllocation.shares.level": "normal", "config.memoryAllocation.shares.shares": mem_mb * 10,
                "config.memoryAllocation.reservation": 0, "config.memoryAllocation.limit": -1,
                "config.memoryHotAddEnabled": i % 3 == 0,
                "summary.quickStats.hostMemoryUsage": int(mem_mb * 0.9) if on else 0,
                "summary.quickStats.privateMemory": int(mem_mb * 0.85) if on else 0,
                "summary.quickStats.sharedMemory": 12 if on else 0, "summary.quickStats.swappedMemory": 0,
                "summary.quickStats.balloonedMemory": 512 if name == "OPS91" else 0, "summary.quickStats.compressedMemory": 0,
                "summary.quickStats.consumedOverheadMemory": 40 if on else 0,
                "summary.quickStats.staticMemoryEntitlement": mem_mb if on else 0,
                "summary.quickStats.distributedMemoryEntitlement": int(mem_mb * 0.6) if on else 0,
                "guest.disk": guest_disks, "guest.toolsStatus": "toolsOk" if on else "toolsNotRunning",
                "guest.toolsVersion": "13312", "guest.guestState": "running" if on else "notRunning",
                "guest.guestId": "windows2022srvNext_64Guest" if windows else "ubuntu64Guest",
                "guest.guestFamily": "windowsGuest" if windows else "linuxGuest",
                "config.tools.toolsUpgradePolicy": "manual", "config.tools.syncTimeWithHost": False,
                "config.tools.afterPowerOn": True, "config.tools.afterResume": True,
                "config.hardware.device": devices,
                "snapshot.rootSnapshotList": snap_tree,
                "snapshot.currentSnapshot": R(f"snapshot-{i}2" if name == "UniFi" else f"snapshot-{i}1") if snap_tree else None,
                "layoutEx.file": layout_files, "layoutEx.snapshot": layout_snaps,
            }
            vms.append(Obj(R(mid), props))
            if host == "host-1001":
                vm_on_host1.add(mid)

        datastores = [Obj(R(mid), {
            "name": name, "summary.type": kind, "summary.capacity": cap * GiB, "summary.freeSpace": free * GiB,
            "summary.accessible": True, "host": [D("DatastoreHostMount", key=R(h)) for h in mounts],
            "vm": RL(vms_by_ds[mid])}) for mid, name, kind, cap, free, mounts in datastores_def]

        pools = [
            Obj("resgroup-11", {"name": "Resources", "owner": R("domain-c10"), "overallStatus": "green",
                                "vm": [R(v.moref) for v in vms if v.moref in vm_on_host1],
                                "config.cpuAllocation.limit": -1, "config.cpuAllocation.reservation": 18400,
                                "config.cpuAllocation.shares.level": "normal", "config.cpuAllocation.shares.shares": 4000,
                                "config.memoryAllocation.limit": -1, "config.memoryAllocation.reservation": 230000,
                                "config.memoryAllocation.shares.level": "normal", "config.memoryAllocation.shares.shares": 163840,
                                "summary.quickStats.overallCpuUsage": 9100, "summary.quickStats.guestMemoryUsage": 41000,
                                "summary.quickStats.hostMemoryUsage": 212000}),
            Obj("resgroup-21", {"name": "Resources", "owner": R("domain-c20"), "overallStatus": "green", "vm": [],
                                "config.cpuAllocation.limit": -1, "config.cpuAllocation.reservation": 36800,
                                "config.cpuAllocation.shares.level": "normal", "config.cpuAllocation.shares.shares": 4000,
                                "config.memoryAllocation.limit": -1, "config.memoryAllocation.reservation": 460000,
                                "config.memoryAllocation.shares.level": "normal", "config.memoryAllocation.shares.shares": 163840,
                                "summary.quickStats.overallCpuUsage": 19200, "summary.quickStats.guestMemoryUsage": 70000,
                                "summary.quickStats.hostMemoryUsage": 411000}),
            Obj("resgroup-v40", {"name": "VCF-Mgmt", "owner": R("domain-c20"), "overallStatus": "green",
                                 "vm": RL(["vm-2017", "vm-2018"]), "config.cpuAllocation.limit": -1,
                                 "config.cpuAllocation.reservation": 4000, "config.cpuAllocation.shares.level": "high",
                                 "config.cpuAllocation.shares.shares": 8000, "config.memoryAllocation.limit": 65536,
                                 "config.memoryAllocation.reservation": 16384, "config.memoryAllocation.shares.level": "high",
                                 "config.memoryAllocation.shares.shares": 327680, "summary.quickStats.overallCpuUsage": 3100,
                                 "summary.quickStats.guestMemoryUsage": 9800, "summary.quickStats.hostMemoryUsage": 30100}),
        ]

        dvs = [Obj("dvs-28", {"name": "DSwitch-CL02", "uuid": "50 1a 2b 3c 4d 5e 6f 70-81 92 a3 b4 c5 d6 e7 f8",
                              "summary.productInfo": D("DistributedVirtualSwitchProductSpec", vendor="VMware, Inc.", version="9.0.0"),
                              "summary.numPorts": 72, "summary.hostMember": RL(["host-1002", "host-1003"]),
                              "config.maxPorts": 2147483647, "config.createTime": datetime(2025, 9, 14, 16, 2, tzinfo=timezone.utc)})]

        def pg_config(vlan: Any, promiscuous: bool = False) -> Any:
            return D("VMwareDVSPortSetting", vlan=vlan, securityPolicy=D(
                "DVSSecurityPolicy", allowPromiscuous=D("BoolPolicy", value=promiscuous),
                macChanges=D("BoolPolicy", value=False), forgedTransmits=D("BoolPolicy", value=False)))
        portgroups = [
            Obj("dvportgroup-30", {"name": "DPG-VM-100", "key": "dvportgroup-30", "config.numPorts": 32,
                                   "config.type": "earlyBinding", "config.distributedVirtualSwitch": R("dvs-28"),
                                   "config.defaultPortConfig": pg_config(D("VmwareDistributedVirtualSwitchVlanIdSpec", vlanId=100))}),
            Obj("dvportgroup-31", {"name": "DPG-vMotion", "key": "dvportgroup-31", "config.numPorts": 8,
                                   "config.type": "earlyBinding", "config.distributedVirtualSwitch": R("dvs-28"),
                                   "config.defaultPortConfig": pg_config(D("VmwareDistributedVirtualSwitchVlanIdSpec", vlanId=20))}),
            Obj("dvportgroup-32", {"name": "DSwitch-CL02-DVUplinks-29", "key": "dvportgroup-32", "config.numPorts": 4,
                                   "config.type": "earlyBinding", "config.distributedVirtualSwitch": R("dvs-28"),
                                   "config.defaultPortConfig": pg_config(D("VmwareDistributedVirtualSwitchTrunkVlanSpec",
                                                                           vlanId=[D("NumericRange", start=0, end=4094)]))}),
        ]
        networks = [Obj("network-12", {"name": "VM Network"}), Obj("network-13", {"name": "Management Network"}),
                    *[Obj(p.moref, {"name": p.props["name"]}) for p in portgroups]]

        self._objects = {
            "VirtualMachine": vms, "HostSystem": hosts, "ClusterComputeResource": clusters, "Datastore": datastores,
            "Datacenter": [Obj("datacenter-3", {"name": "DC-01"})], "ResourcePool": pools, "Network": networks,
            "VmwareDistributedVirtualSwitch": dvs, "DistributedVirtualPortgroup": portgroups,
        }
