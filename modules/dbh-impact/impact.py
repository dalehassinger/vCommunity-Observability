"""Impact ("what depends on this?"): what stops working if one host or one datastore fails.

Python port of DBH Insights' collect/js/impact.js. The collector answers the question
for every host and every datastore once per run, so the page and the REST API read
finished answers instead of recomputing them.

Capacity verdicts use *assigned* memory, which is what an HA restart admits against,
not live consumption. They are estimates and the page says so.
"""

from __future__ import annotations

import json
import re
from typing import Any

from fabric import round2


def _natural(s: str) -> list:
    # localeCompare(..., { numeric: true }): esx9-2 before esx9-10, case-insensitive.
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", s)]


def by_name(x: dict) -> list:
    return _natural(x["name"])


def is_running(vm: dict) -> bool:
    return vm["power_state"] == "poweredOn" and not vm["template"]


def vm_order(vm: dict) -> tuple:
    """Powered-on VMs first, then by name: the ones that actually stop are read first."""
    return (0 if is_running(vm) else 1, _natural(vm["name"]))


def sum_by(items, fn) -> float:
    return round2(sum((fn(i) or 0) for i in items))


def num(v: Any, digits: int = 0) -> str:
    if v is None:
        return "—"
    return f"{v:,.{digits}f}"


def size(g: float | None) -> str:
    if g is None:
        return "—"
    return f"{num(g / 1024, 2)} TiB" if g >= 1024 else f"{num(g, 1)} GiB"


def plural(n: int, word: str, many: str | None = None) -> str:
    return f"{n} {word if n == 1 else (many or word + 's')}"


def power_label(vm: dict) -> str:
    if vm["template"]:
        return "Template"
    return {"poweredOn": "Powered on", "suspended": "Suspended"}.get(vm["power_state"], "Powered off")


def on_off(v: int | None) -> str:
    return "Unknown" if v is None else "Enabled" if v else "Disabled"


# --------------------------------------------------------------------------- the fabric graph

def fabric(tables: dict[str, list[dict]]) -> dict[str, Any]:
    """Join the fabric tables into linked objects, like impact.js fetchFabric()."""
    hosts = {h["moref"]: {**h, "vms": [], "datastores": []} for h in tables["fabric_hosts"]}
    clusters = {}
    for c in tables["fabric_clusters"]:
        clusters[c["moref"]] = {**c, "hosts": sorted((h for h in hosts.values() if h["cluster_moref"] == c["moref"]), key=by_name)}
    vms = {v["moref"]: {**v, "host": hosts.get(v["host_moref"]), "datastores": []} for v in tables["fabric_vms"]}
    for vm in vms.values():
        if vm["host"]:
            vm["host"]["vms"].append(vm)
    datastores = {d["moref"]: {**d, "hosts": [], "vms": []} for d in tables["fabric_datastores"]}
    for m in tables["fabric_mounts"]:
        ds, host = datastores.get(m["datastore_moref"]), hosts.get(m["host_moref"])
        if ds and host:
            ds["hosts"].append(host)
            host["datastores"].append(ds)
    for link in tables["fabric_vm_datastores"]:
        ds, vm = datastores.get(link["datastore_moref"]), vms.get(link["vm_moref"])
        if ds and vm:
            ds["vms"].append(vm)
            vm["datastores"].append(ds)
    sources = tables.get("fabric_source") or [{}]
    server = sources[0].get("server") or "vCenter"
    servers = {r.get("vcenter") or "": r.get("server") or server for r in sources}
    return {"server": server, "servers": servers, "hosts": sorted(hosts.values(), key=by_name),
            "clusters": clusters, "datastores": sorted(datastores.values(), key=by_name)}


# --------------------------------------------------------------------------- the two questions

def host_impact(f: dict, host: dict) -> dict:
    """What a single host failing would take with it."""
    cluster = f["clusters"].get(host["cluster_moref"]) if host["cluster_moref"] else None
    vms = sorted(host["vms"], key=vm_order)
    running = [v for v in vms if is_running(v)]
    templates = [v for v in vms if v["template"]]
    need_mem = sum_by(running, lambda v: v["memory_gib"])
    need_vcpu = sum(v["vcpu"] for v in running)
    # DRAM, not the tiered total: a restart needs real memory.
    mem = lambda h: h["dram_gib"] if h["dram_gib"] is not None else h["memory_gib"]  # noqa: E731

    # Hosts that could take the load: same cluster, connected, not already in maintenance.
    siblings = [h for h in (cluster["hosts"] if cluster else []) if h["moref"] != host["moref"]]
    survivors = [h for h in siblings if h["connection_state"] == "connected" and not h["in_maintenance"]]
    survivor_mem = sum_by(survivors, mem)
    survivor_cores = sum(h["cores"] or 0 for h in survivors)
    committed = sum_by(survivors, lambda h: sum_by([v for v in h["vms"] if is_running(v)], lambda v: v["memory_gib"]))
    headroom = round2(survivor_mem - committed)
    fits = bool(survivors) and headroom >= need_mem

    # Storage only this host presents: local disks, or a LUN nobody else has mounted.
    lost = sorted((d for d in host["datastores"] if len(d["hosts"]) == 1), key=by_name)
    seen, stranded = set(), []
    for d in lost:
        for v in d["vms"]:
            if v["moref"] not in seen and v["host"] is not host:
                seen.add(v["moref"])
                stranded.append(v)
    stranded.sort(key=vm_order)

    n = len(running)
    vm_word = plural(n, "running VM")
    severity, headline = "low", f"No virtual machines are registered on {host['name']}."
    detail = "Losing this host would take compute capacity out of the cluster, but nothing would stop running."
    if running or lost:
        if not cluster:
            severity, headline = "crit", f"{vm_word} would stay down."
            detail = f"{host['name']} is a standalone host, so there is no HA to restart anything. Each VM would need a manual recovery."
        elif cluster["ha_enabled"] == 0:
            severity, headline = "crit", f"{vm_word} would stay down."
            detail = f"vSphere HA is turned off on {cluster['name']}, so nothing would restart automatically."
        elif not survivors:
            severity, headline = "crit", f"{vm_word} would stay down."
            detail = f"No other host in {cluster['name']} is connected and out of maintenance, so there is nowhere to restart them."
        elif not fits:
            severity, headline = "crit", f"{vm_word} would restart — but the cluster looks short of memory."
            detail = (f"They need {size(need_mem)}; the {len(survivors)} remaining "
                      f"host{' has' if len(survivors) == 1 else 's have'} about {size(headroom)} uncommitted.")
        else:
            severity, headline = "warn", f"{vm_word} would restart elsewhere."
            detail = (f"vSphere HA{' (state unknown)' if cluster['ha_enabled'] is None else ''} would restart them on the "
                      f"{plural(len(survivors), 'remaining host')} in {cluster['name']}, which have about {size(headroom)} "
                      "uncommitted memory. Expect a reboot, not a live migration.")
        if lost:
            severity = "crit"
            detail += (f" {len(lost)} datastore{' is' if len(lost) == 1 else 's are'} mounted by this host alone "
                       "and would go offline with it.")

    lists = []
    if lost:
        lists.append({"title": "Datastores this host alone can reach",
                      "note": "No other host has these mounted, so they go offline with the host.",
                      "columns": ["Datastore", "Type", "VMs", "Used"],
                      "rows": [[d["name"], d["kind"] or "", {"num": len(d["vms"])}, {"num": size(d["used_gib"])}] for d in lost]})
    if stranded:
        lists.append({"title": "VMs on other hosts using that storage",
                      "note": "Registered elsewhere, but their files live on a datastore only this host presents.",
                      "columns": ["Virtual machine", "Power", "Registered on"],
                      "rows": [[v["name"], power_label(v), v["host"]["name"] if v["host"] else "—"] for v in stranded]})

    in_cluster = bool(cluster)
    return {
        "kind": "host", "moref": host["moref"], "vcenter": host.get("vcenter") or "",
        "server": f.get("servers", {}).get(host.get("vcenter") or "", f["server"]), "title": host["name"],
        "subtitle": f"Host in {cluster['name']}" if cluster else "Standalone host",
        "severity": severity, "headline": headline, "detail": detail,
        "status": f"{plural(len(vms), 'VM')} registered · {n} running · {size(need_mem)} to restart",
        "kpis": [
            ["VMs affected", num(len(vms)), f"{num(n)} running · {plural(len(templates), 'template')}"],
            ["Memory to restart", size(need_mem), f"{num(need_vcpu)} vCPUs assigned"],
            ["Hosts left in cluster", num(len(survivors)) if in_cluster else "—",
             f"{num(survivor_cores)} cores · {size(survivor_mem)}" if in_cluster else "No cluster"],
            ["Spare memory after failure", size(headroom) if in_cluster and survivors else "—",
             "Enough for the restarts" if fits else "Short of what is needed" if in_cluster and survivors else "Nothing to restart onto"],
        ],
        "facts": [
            ["Cluster", cluster["name"] if cluster else "Standalone"],
            ["vSphere HA", on_off(cluster["ha_enabled"]) if cluster else "Not applicable"],
            ["DRS", on_off(cluster["drs_enabled"]) if cluster else "Not applicable"],
            ["Host state", f"{host['connection_state'] or 'unknown'}{' · in maintenance' if host['in_maintenance'] else ''}"],
            ["Cores / memory", f"{num(host['cores'])} cores · {size(mem(host))}"],
            ["Datastores mounted", num(len(host["datastores"]))],
        ],
        "lists": lists,
        "table": {"title": "Virtual machines on this host",
                  "columns": ["Virtual machine", "Power", "vCPU", "Memory", "Storage"],
                  "rows": [[v["name"], power_label(v), {"num": num(v["vcpu"])}, {"num": size(v["memory_gib"])},
                            ", ".join(d["name"] for d in v["datastores"]) or "—"] for v in vms],
                  "empty": "No virtual machines are registered on this host."},
        # Plain numbers for the REST API (MP Builder metrics).
        "metrics": {"vms_affected": len(vms), "vms_running": n, "memory_to_restart_gib": need_mem,
                    "vcpus_to_restart": need_vcpu, "hosts_left": len(survivors) if in_cluster else None,
                    "spare_memory_gib": headroom if in_cluster and survivors else None,
                    "datastores_lost": len(lost), "restart_fits": int(fits) if in_cluster else None},
    }


def datastore_impact(f: dict, ds: dict) -> dict:
    """What a single datastore going offline would take with it."""
    vms = sorted(ds["vms"], key=vm_order)
    running = [v for v in vms if is_running(v)]
    wholly = [v for v in vms if len(v["datastores"]) == 1]
    spanning = [v for v in vms if len(v["datastores"]) > 1]
    clusters = sorted({(v["host"]["cluster"] if v["host"] else None) or "Standalone hosts" for v in vms})
    memory = sum_by(running, lambda v: v["memory_gib"])
    n = len(running)

    severity, headline = "low", f"Nothing is stored on {ds['name']}."
    detail = "No registered VM has files here, so losing it would cost capacity only."
    if vms:
        severity = "crit" if running else "warn"
        headline = (f"{plural(n, 'running VM')} would crash." if running
                    else f"{plural(len(vms), 'registered VM')} would become unusable.")
        split = (f" {len(wholly)} VM{' has' if len(wholly) == 1 else 's have'} every file here; {len(spanning)} also use "
                 "other datastores and would lose only part of themselves." if spanning
                 else " Every one of them has all its files here.")
        detail = (f"Their disks live here, so HA cannot help: the storage itself is the failure.{split}" if running
                  else f"They are powered off, so nothing stops now — but none could be started until the datastore is back.{split}")

    lists = []
    if ds["hosts"]:
        lists.append({"title": "Hosts that mount it", "note": "These hosts would see the datastore disappear.",
                      "columns": ["Host", "Cluster", "VMs here"],
                      "rows": [[h["name"], h["cluster"] or "Standalone", {"num": sum(1 for v in vms if v["host"] is h)}]
                               for h in sorted(ds["hosts"], key=by_name)]})
    accessible = ds["accessible"]
    return {
        "kind": "datastore", "moref": ds["moref"], "vcenter": ds.get("vcenter") or "",
        "server": f.get("servers", {}).get(ds.get("vcenter") or "", f["server"]), "title": ds["name"],
        "subtitle": f"{ds['kind'] or 'Datastore'} · mounted by {plural(len(ds['hosts']), 'host')}",
        "severity": severity, "headline": headline, "detail": detail,
        "status": f"{plural(len(vms), 'VM')} stored here · {n} running · "
                  + ("size unknown" if ds["used_gib"] is None else f"{size(ds['used_gib'])} used"),
        "kpis": [
            ["VMs affected", num(len(vms)), f"{num(n)} running now"],
            ["Entirely on this datastore", num(len(wholly)), f"{num(len(spanning))} also use other datastores"],
            ["Hosts that mount it", num(len(ds["hosts"])), ", ".join(clusters) if clusters else "No VMs registered"],
            ["Capacity offline", size(ds["capacity_gib"]),
             "" if ds["used_gib"] is None else f"{size(ds['used_gib'])} used · {size(ds['free_gib'])} free"],
        ],
        "facts": [
            ["Type", ds["kind"] or "Unknown"],
            ["Accessible now", "Unknown" if accessible is None else "Yes" if accessible else "No"],
            ["Capacity", size(ds["capacity_gib"])],
            ["Used", size(ds["used_gib"])],
            ["Running VM memory", size(memory)],
            ["Single-host storage", f"Yes — only {ds['hosts'][0]['name']}" if len(ds["hosts"]) == 1 else "No"],
        ],
        "lists": lists,
        "table": {"title": "Virtual machines with files here",
                  "columns": ["Virtual machine", "Power", "Registered on", "Cluster", "Scope"],
                  "rows": [[v["name"], power_label(v), v["host"]["name"] if v["host"] else "—",
                            (v["host"]["cluster"] if v["host"] else None) or "Standalone",
                            "All files here" if len(v["datastores"]) == 1 else f"Split across {len(v['datastores'])} datastores"]
                           for v in vms],
                  "empty": "No registered VM has files on this datastore."},
        "metrics": {"vms_affected": len(vms), "vms_running": n, "vms_wholly_here": len(wholly),
                    "hosts_mounting": len(ds["hosts"]), "running_memory_gib": memory},
    }


SEVERITY_RANK = {"crit": 0, "warn": 1, "low": 2}


def impact_rows(tables: dict[str, list[dict]]) -> list[dict]:
    """One row per host and datastore: summary columns for the API, the full answer as JSON."""
    f = fabric(tables)
    answers = [host_impact(f, h) for h in f["hosts"]] + [datastore_impact(f, d) for d in f["datastores"]]
    rows = []
    for a in answers:
        m = a["metrics"]
        # Prefer the fabric object's vcenter stamp; fall back to a multi-VC moref prefix.
        vc = a.get("vcenter")
        if not vc:
            moref = a["moref"] or ""
            vc = moref.split(":", 1)[0] if ":" in moref and not moref.startswith(("host-", "datastore-", "vm-", "domain-")) else ""
        rows.append({
            "vcenter": vc, "target": f"{a['kind']}:{a['moref']}", "kind": a["kind"], "moref": a["moref"],
            "name": a["title"],
            "subtitle": a["subtitle"], "severity": a["severity"], "severity_rank": SEVERITY_RANK[a["severity"]],
            "headline": a["headline"], "detail": a["detail"], "status": a["status"],
            "vms_affected": m["vms_affected"], "vms_running": m["vms_running"],
            "memory_gib": m.get("memory_to_restart_gib", m.get("running_memory_gib")),
            "hosts_left": m.get("hosts_left"), "spare_memory_gib": m.get("spare_memory_gib"),
            "restart_fits": m.get("restart_fits"), "datastores_lost": m.get("datastores_lost"),
            "vms_wholly_here": m.get("vms_wholly_here"), "hosts_mounting": m.get("hosts_mounting"),
            "report": json.dumps({k: a[k] for k in ("kind", "server", "title", "subtitle", "severity", "headline",
                                                     "detail", "status", "kpis", "facts", "lists", "table")}),
        })
    return rows
