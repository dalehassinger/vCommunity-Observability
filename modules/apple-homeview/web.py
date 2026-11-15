"""Web extension of the Apple HomeView module.

Routes (no token needed, read-only, like the dashboards):
  GET /homeview                  the full-screen display (static page, fills itself from the JSON)
  GET /homeview/static/<file>    its CSS and JS
  GET /ui/homeview/data          one JSON document with everything the pages show

The data comes from the other modules' SQLite tables (dbh-insights, datastore-capacity,
github-status) through the core's read-only storage. A module that is off or has never
collected is reported as unavailable, and the display leaves its pages out.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from fastapi.responses import FileResponse, HTMLResponse

HERE = Path(__file__).parent

# How many rows the display gets at most per list (the page splits them into screens, or
# shows 10 / 25 / 50 / 100 per page when picked in the header's Show menu).
MAX_HOSTS = 100
MAX_CLUSTERS = 100
MAX_DATASTORES = 100
MAX_FINDINGS = 100
MAX_VMS = 100


def _num(v: Any, digits: int = 1) -> float | None:
    try:
        return round(float(v), digits)
    except (TypeError, ValueError):
        return None


def register(app, ctx) -> None:
    core = ctx.core

    def module_on(name: str) -> bool:
        mod = core.modules.get(name)
        return bool(mod and mod.available)

    def q(module: str, sql: str) -> list[dict[str, Any]]:
        try:
            return core.storage.query(module, sql)
        except Exception:  # noqa: BLE001 - table not created yet: treat as no data
            return []

    def last_ok(module: str) -> str | None:
        runs = core.storage.last_runs()
        times = [r["finished_at"] for (m, _), r in runs.items() if m == module and r["status"] == "ok"]
        return max(times) if times else None

    # -- DBH Insights ------------------------------------------------------------------
    def dbh() -> dict[str, Any]:
        if not module_on("dbh-insights"):
            return {"available": False, "why": "DBH Insights module is off"}
        hosts = q("dbh-insights", "SELECT host, cluster, connection_state, power_state, in_maintenance_mode, config_status, "
                  "num_cores, num_cpu_threads, cpu_usage_pct, dram_gib, num_memory_gib, memory_usage_pct, num_vms, "
                  "num_vms_total, vcpus_per_core, esx_version, vendor, model, boot_time FROM vhost ORDER BY cluster, host")
        if not hosts:
            return {"available": False, "why": "DBH Insights has not collected yet"}
        vms = q("dbh-insights", "SELECT vm, powerstate, template, cpus, memory, cpu_usage_pct, memory_usage_pct, "
                "tools_version_status, tools_running_status, host, os_according_to_the_vmware_tools AS os, "
                "os_according_to_the_configuration_file AS os_cfg, primary_ip_address AS ip FROM vinfo")
        clusters = q("dbh-insights", "SELECT * FROM cluster_capacity ORDER BY cluster")
        datastores = q("dbh-insights", "SELECT name, type, accessible, num_vms, capacity_gib, in_use_gib, free_gib, free_pct "
                       "FROM vdatastore ORDER BY free_pct")
        health = q("dbh-insights", "SELECT name, message, message_type FROM vhealth ORDER BY message_type, name")
        snaps = q("dbh-insights", "SELECT vm, name, date_time, size_mib_total FROM vsnapshot ORDER BY date_time")
        source = q("dbh-insights", "SELECT name, full_name, version, build, vi_sdk_server FROM vsource")

        cores = sum(h["num_cores"] or 0 for h in hosts) or 1
        dram = sum(h["dram_gib"] or 0 for h in hosts) or 1
        cpu = sum((h["cpu_usage_pct"] or 0) * (h["num_cores"] or 0) for h in hosts) / cores
        mem = sum((h["memory_usage_pct"] or 0) * (h["dram_gib"] or 0) for h in hosts) / dram
        real = [v for v in vms if not v["template"]]
        on = [v for v in real if v["powerstate"] == "poweredOn"]
        by_type: dict[str, int] = {}
        for f in health:
            by_type[f["message_type"] or "Other"] = by_type.get(f["message_type"] or "Other", 0) + 1
        ds_cap = sum(d["capacity_gib"] or 0 for d in datastores)
        ds_used = sum(d["in_use_gib"] or 0 for d in datastores)
        busiest = sorted(on, key=lambda v: v["cpu_usage_pct"] or 0, reverse=True)[:MAX_VMS]
        busiest_mem = sorted(on, key=lambda v: v["memory_usage_pct"] or 0, reverse=True)[:MAX_VMS]

        def vm_row(v: dict[str, Any]) -> dict[str, Any]:
            return {"name": v["vm"], "host": v["host"], "cpus": int(v["cpus"] or 0), "mem_gib": _num((v["memory"] or 0) / 1024),
                    "cpu_pct": _num(v["cpu_usage_pct"]), "mem_pct": _num(v["memory_usage_pct"]),
                    "os": v["os"] or v["os_cfg"] or ""}
        return {
            "available": True,
            "collected_at": last_ok("dbh-insights"),
            "vcenters": [{"name": s["vi_sdk_server"], "product": s["full_name"], "version": s["version"], "build": s["build"]}
                         for s in source],
            "totals": {
                "hosts": len(hosts),
                "hosts_connected": sum(1 for h in hosts if h["connection_state"] == "connected"),
                "hosts_maintenance": sum(1 for h in hosts if h["in_maintenance_mode"]),
                "cores": int(cores), "dram_gib": _num(dram, 0),
                "cpu_pct": _num(cpu), "mem_pct": _num(mem),
                "vms": len(real), "vms_on": len(on), "vms_off": len(real) - len(on),
                "templates": sum(1 for v in vms if v["template"]),
                "vcpus_on": int(sum(v["cpus"] or 0 for v in on)),
                "vram_on_gib": _num(sum(v["memory"] or 0 for v in on) / 1024, 0),
                "tools_upgrade": sum(1 for v in on if v["tools_version_status"] == "guestToolsNeedUpgrade"),
                "tools_not_running": sum(1 for v in on if v["tools_running_status"] != "guestToolsRunning"),
                "snapshots": len(snaps),
                "datastores": len(datastores),
                "ds_capacity_gib": _num(ds_cap, 0), "ds_used_gib": _num(ds_used, 0),
                "ds_used_pct": _num(100 * ds_used / ds_cap) if ds_cap else None,
                "findings": len(health),
                "clusters": len(clusters),
            },
            "hosts": [{
                "name": h["host"], "cluster": h["cluster"], "state": h["connection_state"], "power": h["power_state"],
                "maintenance": bool(h["in_maintenance_mode"]), "status": h["config_status"],
                "cores": int(h["num_cores"] or 0), "threads": int(h["num_cpu_threads"] or 0),
                "cpu_pct": _num(h["cpu_usage_pct"]), "mem_pct": _num(h["memory_usage_pct"]),
                "dram_gib": _num(h["dram_gib"], 0), "vms": int(h["num_vms"] or 0), "vms_total": int(h["num_vms_total"] or 0),
                "vcpu_core": _num(h["vcpus_per_core"], 2), "version": re.sub(r"^VMware ESXi\s*", "ESX ", h["esx_version"] or ""),
                "model": " ".join(x for x in (h["vendor"], h["model"]) if x),
            } for h in hosts[:MAX_HOSTS]],
            "clusters": [{
                "name": c["cluster"], "ha": bool(c["ha_enabled"]), "drs": bool(c["drs_enabled"]),
                "hosts": int(c["num_hosts"] or 0), "hosts_available": int(c["num_hosts_available"] or 0),
                "cores": int(c["num_cores"] or 0), "dram_gib": _num(c["dram_gib"], 0),
                "vms": int(c["num_vms"] or 0), "vms_on": int(c["num_powered_on"] or 0),
                "vcpu_core": _num(c["running_vcpus_per_core"], 2),
                "vram_pct": _num(c["running_vram_pct_of_dram"]),
                "n1": bool(c["n_plus_1_memory_est"]),
                "ds_free_pct": _num(c["datastore_free_pct"]),
                "ds_capacity_gib": _num(c["datastore_capacity_gib"], 0),
            } for c in clusters[:MAX_CLUSTERS]],
            "busiest_vms": [vm_row(v) for v in busiest],          # powered-on VMs, highest CPU first
            "busiest_vms_mem": [vm_row(v) for v in busiest_mem],  # the same, highest memory first
            "snapshots": [{"vm": s["vm"], "name": s["name"], "date": s["date_time"], "mib": _num(s["size_mib_total"])}
                          for s in snaps[:10]],
            "datastores": [{"name": d["name"], "type": d["type"], "capacity_gib": _num(d["capacity_gib"], 0),
                            "used_pct": _num(100 - (d["free_pct"] or 0)), "free_gib": _num(d["free_gib"], 0),
                            "vms": int(d["num_vms"] or 0), "accessible": bool(d["accessible"])} for d in datastores],
            "findings_by_type": sorted(({"type": k, "count": n} for k, n in by_type.items()), key=lambda x: -x["count"]),
            "findings": [{"object": f["name"], "message": f["message"], "type": f["message_type"]} for f in health[:MAX_FINDINGS]],
        }

    # -- Datastore Capacity ------------------------------------------------------------
    def storage() -> dict[str, Any]:
        if not module_on("datastore-capacity"):
            return {"available": False, "why": "Datastore Capacity module is off"}
        rows = q("datastore-capacity", "SELECT vcenter, name, datacenter, type, capacity_gb, free_gb, used_gb, used_pct, "
                 "provisioned_pct, vm_count, accessible FROM datastores ORDER BY used_pct DESC")
        if not rows:
            return {"available": False, "why": "Datastore Capacity has not collected yet"}
        runway: dict[tuple, Any] = {}
        mod = core.modules.get("datastore-capacity")
        sql = next((r.get("query") for r in (mod.api if mod else []) if r.get("path") == "/runway"), None)
        if sql:
            for r in q("datastore-capacity", sql):
                runway[(r["vcenter"], r["name"])] = {"days": r.get("runway_days"), "growth": r.get("growth_gb_per_day")}
        cap = sum(r["capacity_gb"] or 0 for r in rows)
        free = sum(r["free_gb"] or 0 for r in rows)
        return {
            "available": True, "collected_at": last_ok("datastore-capacity"),
            "totals": {"count": len(rows), "capacity_tb": _num(cap / 1024), "free_tb": _num(free / 1024),
                       "used_pct": _num(100 * (cap - free) / cap) if cap else None,
                       "lowest_free_pct": _num(min(100 - (r["used_pct"] or 0) for r in rows)),
                       "over_80": sum(1 for r in rows if (r["used_pct"] or 0) >= 80),
                       "vms": sum(r["vm_count"] or 0 for r in rows)},
            "items": [{
                "name": r["name"], "datacenter": r["datacenter"], "type": r["type"],
                "capacity_tb": _num((r["capacity_gb"] or 0) / 1024, 2), "free_tb": _num((r["free_gb"] or 0) / 1024, 2),
                "used_pct": _num(r["used_pct"]), "provisioned_pct": _num(r["provisioned_pct"]),
                "vms": r["vm_count"], "accessible": bool(r["accessible"]),
                "runway_days": (runway.get((r["vcenter"], r["name"])) or {}).get("days"),
                "growth_gb_day": (runway.get((r["vcenter"], r["name"])) or {}).get("growth"),
            } for r in rows[:MAX_DATASTORES]],
        }

    # -- GitHub Status -------------------------------------------------------------------
    def github() -> dict[str, Any]:
        if not module_on("github-status"):
            return {"available": False, "why": "GitHub Status module is off"}
        page = q("github-status", "SELECT * FROM page_status LIMIT 1")
        if not page:
            return {"available": False, "why": "GitHub Status has not collected yet"}
        return {
            "available": True, "collected_at": last_ok("github-status"),
            "status": {k: page[0].get(k) for k in ("name", "indicator", "description", "updated_at")},
            "components": q("github-status", "SELECT name, status FROM components ORDER BY position, name LIMIT 12"),
            "incidents": q("github-status", "SELECT name, status, impact, started_at, updated_at, latest_update, components "
                           "FROM incidents WHERE active = 1 ORDER BY started_at DESC LIMIT 3"),
            "resolved": q("github-status", "SELECT name, impact, resolved_at FROM incidents WHERE active = 0 "
                          "ORDER BY resolved_at DESC LIMIT 3"),
            "maintenances": q("github-status", "SELECT name, status, scheduled_for, scheduled_until, components "
                              "FROM maintenances ORDER BY scheduled_for LIMIT 2"),
        }

    @app.get("/ui/homeview/data", include_in_schema=False)
    def data() -> dict[str, Any]:
        ctx.check_available()
        cycle = getattr(core, "homeview_cycle_seconds", None)
        return {"generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "mock": bool(core.cfg.mock),
                "cycle_seconds": cycle() if cycle else 20,
                "layout": core.homeview_layout() if hasattr(core, "homeview_layout") else None,
                "dbh": dbh(), "storage": storage(), "github": github()}

    @app.get("/homeview", response_class=HTMLResponse, include_in_schema=False)
    def display():
        ctx.check_available()
        return FileResponse(HERE / "static" / "homeview.html", media_type="text/html", headers={"Cache-Control": "no-cache"})

    @app.get("/homeview/static/{name}", include_in_schema=False)
    def static(name: str):
        if not re.fullmatch(r"[\w.-]+\.(js|css)", name) or not (HERE / "static" / name).is_file():
            raise HTTPException(404, "Not found")
        return FileResponse(HERE / "static" / name, headers={"Cache-Control": "no-cache"})
