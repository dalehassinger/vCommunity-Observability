"""Write a small made-up RVTools-style workbook for testing the spreadsheet source.

    python ask/scripts/make_sample_rvtools.py sample-rvtools.xlsx

Three sheets (vInfo, vHost, vDatastore) with RVTools column names, 2 clusters,
4 hosts and 40 VMs. Every name is invented."""

from __future__ import annotations

import random
import sys

import openpyxl


def main(path: str) -> None:
    rnd = random.Random(7)
    wb = openpyxl.Workbook()
    hosts = [("esx01.lab.local", "CL-PROD"), ("esx02.lab.local", "CL-PROD"),
             ("esx03.lab.local", "CL-DEV"), ("esx04.lab.local", "CL-DEV")]
    ws = wb.active
    ws.title = "vInfo"
    ws.append(["VM", "Powerstate", "Template", "CPUs", "Memory", "Provisioned MiB", "In Use MiB",
               "OS according to the configuration file", "Datacenter", "Cluster", "Host", "VI SDK Server"])
    oses = ["Microsoft Windows Server 2022 (64-bit)", "Ubuntu Linux (64-bit)", "Red Hat Enterprise Linux 9 (64-bit)"]
    for i in range(1, 41):
        host, cluster = hosts[i % 4]
        cpus = rnd.choice([1, 2, 2, 4, 4, 8, 16])
        prov = rnd.choice([40, 60, 100, 200, 500]) * 1024
        ws.append([f"vm{i:02d}", "poweredOff" if i % 7 == 0 else "poweredOn", "True" if i == 40 else "False",
                   cpus, cpus * rnd.choice([2048, 4096]), prov, int(prov * rnd.uniform(.2, .9)),
                   rnd.choice(oses), "DC-LAB", cluster, host, "vcsa.lab.local"])
    ws = wb.create_sheet("vHost")
    ws.append(["Host", "Datacenter", "Cluster", "# CPU", "# Cores", "# Memory", "ESX Version", "in Maintenance Mode",
               "VI SDK Server"])
    for h, c in hosts:
        ws.append([h, "DC-LAB", c, 2, 32, 524288, "VMware ESXi 8.0.3 build-24022510", "False", "vcsa.lab.local"])
    ws = wb.create_sheet("vDatastore")
    ws.append(["Name", "Type", "# VMs", "Capacity MiB", "In Use MiB", "Free MiB", "Free %", "VI SDK Server"])
    for n, cap, used in (("DS-PROD-01", 4194304, 3355443), ("DS-DEV-01", 2097152, 629145), ("DS-ISO", 524288, 104857)):
        ws.append([n, "VMFS", 0, cap, used, cap - used, round(100 * (cap - used) / cap), "vcsa.lab.local"])
    wb.save(path)
    print(f"wrote {path}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "sample-rvtools.xlsx")
