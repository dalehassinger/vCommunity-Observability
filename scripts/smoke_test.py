"""Smoke test: run the app with sample data and check every endpoint and page.

    VCO_MOCK=1 python scripts/smoke_test.py
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("VCO_MOCK", "1")

from fastapi.testclient import TestClient  # noqa: E402

from core.app import create_app  # noqa: E402
from core.config import Config  # noqa: E402

root = Path(__file__).resolve().parents[1]
cfg = Config(
    data_dir=Path(tempfile.mkdtemp()), modules_dirs=[root / "modules"], mock=True,
    connections={"vcenter": {"host": "mock"}}, api_tokens=["test-token"],
)
app = create_app(cfg, start_scheduler=False)
core = app.state.core
for mod in core.modules.values():
    if mod.problems:
        print(f"  skip {mod.name}: {mod.problems[0]}")
        continue
    for result in core.run_module(mod):
        assert result["status"] == "ok", (mod.name, result)

client = TestClient(app)
auth = {"Authorization": "Bearer test-token"}
assert client.get("/api/core/modules").status_code == 401, "API must require a token"
mods = client.get("/api/core/modules", headers=auth).json()
print("modules:", [m["name"] for m in mods["items"]])
# Modules with runtime problems (no pwsh) stay listed but their APIs/dashboards 404.
runnable = {m.name for m in core.modules.values() if not m.problems}
for m in mods["items"]:
    if m["name"] not in runnable:
        print(f"  skip api {m['name']}: not runnable here")
        continue
    for ep in m["endpoints"]:
        r = client.get(ep, headers={"X-API-Key": "test-token"})
        assert r.status_code == 200, (ep, r.text)
        print(f"  {ep}: {r.json()['count']} items")
inv = "/dash/vcenter-inventory/inventory"
pages = ["/", "/overview", "/modules", "/docs", "/about", "/dash/datastore-capacity/overview",
         inv, inv + "?type=vcenter&tab=hosts", inv + "?type=vcenter&tab=vms",
         inv + "?type=cluster&name=CL-01", inv + "?type=cluster&name=CL-02&tab=hosts", inv + "?type=cluster&name=CL-01&tab=vms",
         inv + "?type=host&name=esx9-01.vcrocs.local", inv + "?type=host&name=esx9-01.vcrocs.local&tab=vms",
         inv + "?type=host&name=esx9-03.vcrocs.local&tab=datastores",
         inv + "?type=vm&name=UniFi", inv + "?type=vm&name=offDepot", inv + "?type=vm&name=UniFi&tab=snapshots",
         inv + "?type=vm&name=UniFi&tab=networks", inv + "?type=vm&name=does-not-exist",
         "/dash/github-status/status", "/dash/dbh-insights/insights", "/dash/dbh-insights/sheets",
         "/dash/dbh-insights/sheets?sheet=vhost", "/dash/dbh-insights/sheets?sheet=cluster_capacity",
         "/dash/dbh-insights/sheets?sheet=nope"]
if "host-health" in runnable:
    pages.insert(4, "/dash/host-health/overview")
for page in pages:
    r = client.get(page)
    assert r.status_code == 200 and "Query failed" not in r.text, (page, r.status_code, r.text[:300])
    # /modules lists runtime problems (e.g. missing pwsh) as alert-danger; other pages must be clean.
    if page != "/modules":
        assert "alert-danger" not in r.text, (page, r.text[:300])
    print(f"  page {page}: ok")
openapi_paths = client.get("/openapi.json").json()["paths"]
if "host-health" in runnable:
    assert "/api/host-health/hosts" in openapi_paths
else:
    assert "/api/datastore-capacity/datastores" in openapi_paths
# Module on/off: switching off hides the dashboard and API, switching on restores them.
r = client.put("/api/core/modules/github-status/enabled?enabled=false", headers=auth)
assert r.status_code == 200 and r.json()["enabled"] is False, r.text
assert client.get("/api/github-status/components", headers=auth).status_code == 404
assert client.get("/dash/github-status/status").status_code == 404
assert "GitHub Status" not in client.get("/overview").text
assert core.storage.module_states()["github-status"] is False
r = client.post("/modules/github-status/toggle", follow_redirects=False)
assert r.status_code == 303 and core.modules["github-status"].enabled
assert client.get("/api/github-status/components", headers=auth).status_code == 200
print("  module on/off: ok")
# Home: "/" opens DBH Insights; with dbh-insights off it falls back to All dashboards.
r = client.get("/", follow_redirects=False)
assert r.status_code == 302 and r.headers["location"] == "/dash/dbh-insights/insights", r.headers
client.put("/api/core/modules/dbh-insights/enabled?enabled=false", headers=auth)
assert client.get("/", follow_redirects=False).headers["location"] == "/overview"
client.put("/api/core/modules/dbh-insights/enabled?enabled=true", headers=auth)
assert client.get("/", follow_redirects=False).headers["location"] == "/dash/dbh-insights/insights"
print("  home: ok")
# Module order: move down/up and set via API; the sidebar follows it.
names = list(core.modules)
client.post(f"/modules/{names[0]}/move?dir=down", follow_redirects=False)
assert list(core.modules)[:2] == [names[1], names[0]], list(core.modules)
r = client.put("/api/core/modules/order", headers=auth, json=["github-status", "vcenter-inventory"])
assert r.status_code == 200 and r.json()["order"][:2] == ["github-status", "vcenter-inventory"], r.text
home = client.get("/overview").text
assert home.index("GitHub Status") < home.index("vSphere Inventory"), "sidebar order"
if "Host Health" in home:
    assert home.index("vSphere Inventory") < home.index("Host Health"), "sidebar order"
assert core.storage.module_order()[:2] == ["github-status", "vcenter-inventory"]
assert client.put("/api/core/modules/order", headers=auth, json=["nope"]).status_code == 400
print("  module order: ok")
# DBH Insights: manifest tables match sheets.py, every sheet collected, exports work.
import io, zipfile  # noqa: E401,E402
sys.path.insert(0, str(root / "modules" / "dbh-insights"))
import sheets as dbh_sheets  # noqa: E402
mod = core.modules["dbh-insights"]
for tname, tdef in dbh_sheets.manifest_tables().items():
    assert mod.tables[tname]["columns"] == tdef["columns"], f"module.yaml tables out of date for {tname}: run python sheets.py --tables"
status = core.storage.query("dbh-insights", "SELECT sheet, row_count, error FROM sheets")
assert len(status) == 28 and not [s for s in status if s["error"]], status
r = client.get("/export/dbh-insights/xlsx")
assert r.status_code == 200 and r.headers["content-type"].startswith("application/vnd.openxml"), r.text[:300]
assert "attachment; filename=\"dbh-insights-" in r.headers["content-disposition"]
z = zipfile.ZipFile(io.BytesIO(r.content))
wb = z.read("xl/workbook.xml").decode()
assert wb.index('name="vInfo"') < wb.index('name="vCPU"') < wb.index('name="vHost"') < wb.index('name="Cluster Capacity"'), "sheet order"
assert wb.count("<sheet ") == 28
r = client.get("/api/dbh-insights/export/csv?sheet=vHost", headers=auth)
assert r.status_code == 200 and r.text.lstrip("\ufeff").startswith("Host,Cluster,Config status"), r.text[:200]
assert client.get("/api/dbh-insights/export/csv", headers=auth).status_code == 500
assert client.get("/api/dbh-insights/export/xlsx").status_code == 401
print("  dbh-insights: 28 sheets, xlsx and csv exports ok")
# DBH Topology and Impact: both modules keep an identical copy of fabric.py.
assert (root / "modules/dbh-topology/fabric.py").read_text() == (root / "modules/dbh-impact/fabric.py").read_text(), \
    "modules/dbh-topology/fabric.py and modules/dbh-impact/fabric.py must stay identical"
page = client.get("/dash/dbh-topology/topology").text
assert 'id="topo-data"' in page and "esx9-01.vcrocs.local" in page and "alert-danger" not in page
mounts = client.get("/api/dbh-topology/mounts", headers=auth).json()["items"]
assert {"host": "esx9-01.vcrocs.local", "cluster": "CL-01", "datastore": "ESX9-01-2TB", "kind": "VMFS"} in mounts
assert [d["datastore"] for d in client.get("/api/dbh-topology/single-host-datastores", headers=auth).json()["items"]] == ["ESX9-01-2TB", "ESX9-04-local"]
hosts = {h["name"]: h for h in client.get("/api/dbh-impact/hosts", headers=auth).json()["items"]}
# Same answer as the DBH Insights Impact screenshot: HA off on CL-01, 13 running VMs stay down, 106 GiB.
h1 = hosts["esx9-01.vcrocs.local"]
assert (h1["severity"], h1["vms_running"], h1["memory_to_restart_gib"], h1["datastores_lost"]) == ("crit", 13, 106.0, 1), h1
assert h1["headline"] == "13 running VMs would stay down." and "vSphere HA is turned off on CL-01" in h1["detail"]
assert hosts["esx9-02.vcrocs.local"]["severity"] == "warn" and hosts["esx9-02.vcrocs.local"]["restart_fits"] == 1
assert "standalone host" in hosts["esx9-04.vcrocs.local"]["detail"]
dss = {d["name"]: d for d in client.get("/api/dbh-impact/datastores", headers=auth).json()["items"]}
assert dss["SYN-SSD-04"]["vms_wholly_here"] == 2 and dss["SYN-SSD-04"]["vms_affected"] == 4
assert dss["ESX9-04-local"]["severity"] == "warn" and "powered off" in dss["ESX9-04-local"]["detail"]
assert not core.storage.query("dbh-impact", "SELECT name FROM fabric_vms WHERE name LIKE 'vCLS-%'"), "vCLS VMs are excluded"
for target in ["", "?target=host:host-12", "?target=datastore:datastore-24", "?target=nope"]:
    r = client.get("/dash/dbh-impact/impact" + target)
    assert r.status_code == 200 and "alert-danger" not in r.text and 'id="imp-report"' in r.text, (target, r.text[:300])
assert '"title": "SYN-SSD-04"' in client.get("/dash/dbh-impact/impact?target=datastore:datastore-24").text
print("  dbh-topology and dbh-impact: ok")
# Settings page: edit a connection and intervals from the UI.
assert client.get("/settings").status_code == 200
assert "vCenter connections" in client.get("/settings").text
core.cfg.connections["vcenter"]["password"] = "from-env"
core.cfg.connections["vcenter"]["instances"][0]["password"] = "from-env"
form = {"host": "vc2.lab.local", "user": "svc@vsphere.local", "password": "", "verify_ssl": "1", "action": "save"}
assert "Not saved" in client.post("/settings/connections/vcenter", data=form).text, "token required"
assert core.cfg.connections["vcenter"]["host"] == "mock"
r = client.post("/settings/connections/vcenter", data={**form, "token": "test-token"}, follow_redirects=False)
assert r.status_code == 303, r.text
vc = core.cfg.connections["vcenter"]
assert vc["host"] == "vc2.lab.local" and vc["password"] == "from-env" and vc["verify_ssl"] == "true", vc
client.post("/settings/connections/vcenter", data={**form, "password": "NewPass1!", "token": "test-token"})
assert core.cfg.connections["vcenter"]["password"] == "NewPass1!"
from core.runner import _env  # noqa: E402
env = _env(core.cfg, core.modules["dbh-insights"], core.modules["dbh-insights"].collectors[0])
assert env["VCO_VCENTER_HOST"] == "vc2.lab.local" and env["VCO_VCENTER_PASSWORD"] == "NewPass1!"
assert "VCO_VCENTERS" in env and "vc2.lab.local" in env["VCO_VCENTERS"]
r = client.post("/settings/connections/vcenter", data={**form, "action": "test", "token": "test-token"})
assert "Sample data mode" in r.text, r.text[:400]
page = client.get("/settings").text
assert "NewPass1!" not in page and "edited here" in page, "password must never be echoed"
assert core.storage.vcenter_instances()[0]["host"] == "vc2.lab.local"
client.post("/settings/connections/vcenter/reset", data={"token": "test-token"})
assert core.cfg.connections["vcenter"]["host"] == "mock"
assert not core.storage.vcenter_instances() and not core.storage.connection_settings()
r = client.post("/settings/intervals", data={"dbh-insights:sheets": "45", "token": "test-token"})
assert "1 changed" in r.text and core.modules["dbh-insights"].collectors[0].interval_minutes == 45
assert core.storage.collector_intervals()[("dbh-insights", "sheets")] == 45
r = client.post("/settings/intervals", data={"dbh-insights:sheets": "0", "token": "test-token"})
assert "use 1 to 10080" in r.text
print("  settings: ok")
# Multi-vCenter: add a second instance, re-collect, assert identity columns and namespaced morefs.
add = {"is_new": "1", "id": "lab2", "label": "Lab Two", "host": "vcsa-lab2.example.com",
       "user": "admin@vsphere.local", "password": "Lab2Pass!", "verify_ssl": "1", "action": "save",
       "token": "test-token"}
assert "Not saved" in client.post("/settings/vcenters", data={k: v for k, v in add.items() if k != "token"}).text
r = client.post("/settings/vcenters", data=add, follow_redirects=False)
assert r.status_code == 303, r.text
ids = [i["id"] for i in core.vcenter_list()]
assert ids == ["default", "lab2"], ids
env = _env(core.cfg, core.modules["datastore-capacity"], core.modules["datastore-capacity"].collectors[0])
vcs = __import__("json").loads(env["VCO_VCENTERS"])
assert [v["id"] for v in vcs] == ["default", "lab2"] and vcs[1]["password"] == "Lab2Pass!"
for mod_name in ("datastore-capacity", "vcenter-inventory", "dbh-topology", "dbh-impact", "dbh-insights"):
    for result in core.run_module(core.modules[mod_name]):
        assert result["status"] == "ok", (mod_name, result)
ds = core.storage.query("datastore-capacity", "SELECT vcenter, COUNT(*) AS n FROM datastores GROUP BY vcenter ORDER BY vcenter")
assert {r["vcenter"]: r["n"] for r in ds} == {"default": 5, "lab2": 5}, ds
hosts = core.storage.query("vcenter-inventory", "SELECT vcenter, COUNT(*) AS n FROM hosts GROUP BY vcenter ORDER BY vcenter")
assert {r["vcenter"]: r["n"] for r in hosts}["lab2"] >= 1
fab = core.storage.query("dbh-topology", "SELECT moref FROM fabric_hosts WHERE vcenter = 'lab2' LIMIT 1")
assert fab and fab[0]["moref"].startswith("lab2:"), fab
imp = core.storage.query("dbh-impact", "SELECT target FROM impact WHERE vcenter = 'default' AND name = 'esx9-01.vcrocs.local'")
assert imp and imp[0]["target"] == "host:default:host-11", imp
sheets = core.storage.query("dbh-insights", "SELECT vcenter, COUNT(*) AS n FROM sheets GROUP BY vcenter ORDER BY vcenter")
assert {r["vcenter"]: r["n"] for r in sheets} == {"default": 28, "lab2": 28}, sheets
# Per-module selection: only lab2.
core.modules["datastore-capacity"].settings["vcenters"] = ["lab2"]
env = _env(core.cfg, core.modules["datastore-capacity"], core.modules["datastore-capacity"].collectors[0])
assert [v["id"] for v in __import__("json").loads(env["VCO_VCENTERS"])] == ["lab2"]
core.modules["datastore-capacity"].settings.pop("vcenters", None)
assert "lab2: removed" in client.post("/settings/vcenters/lab2/delete", data={"token": "test-token"}).text
assert [i["id"] for i in core.vcenter_list()] == ["default"]
client.post("/settings/vcenters/reset", data={"token": "test-token"})
assert core.cfg.connections["vcenter"]["host"] == "mock" and not core.storage.vcenter_instances()
print("  multi-vcenter: ok")
# Scheduled export: one schedule, shown in the form; saving again updates it.
os.environ["VCO_EXPORT_DIR"] = str(cfg.data_dir / "exports")
sched = {"export": "dbh-insights:xlsx", "folder": "dbh", "frequency": "weekly", "weekday": "tue",
         "monthday": "1", "at_time": "06:30", "timezone": "America/New_York", "keep": "2", "token": "test-token"}
r = client.post("/settings/exports", data={**sched, "token": ""})
assert "Not saved: enter a valid API token" in r.text and 'value="dbh"' in r.text and 'value="06:30"' in r.text, "form keeps what was typed"
assert not core.storage.export_schedules()
assert "Time must be" in client.post("/settings/exports", data={**sched, "at_time": "25:00"}).text
r = client.post("/settings/exports", data=sched)
assert "Schedule saved and verified" in r.text and "Export XLSX: Every Tuesday at 06:30 America/New_York" in r.text, r.text[:600]
assert "every value matches" in r.text and "Next run" in r.text
# A save that doesn't stick is reported, not shown as success
_orig = core.storage.update_export_schedule
core.storage.update_export_schedule = lambda sid, values: None
r = client.post("/settings/exports", data={**sched, "at_time": "07:45"})
core.storage.update_export_schedule = _orig
assert "Not saved: the database has a different at time" in r.text and "saved and verified" not in r.text, r.text[:800]
assert 'value="07:45"' in r.text, "form keeps what was typed after a failed verify"
page = client.get("/settings").text
assert 'value="dbh"' in page and '<option value="tue" selected>' in page and "Every Tuesday at 06:30" in page, "saved values shown"
sid = core.storage.export_schedules()[0]["id"]
import time as _t  # noqa: E402
for _ in range(3):
    r = client.post(f"/settings/exports/{sid}/run", data={"token": "test-token"})
    assert "wrote dbh-insights-" in r.text, r.text[:600]
    _t.sleep(1.1)
files = sorted((cfg.data_dir / "exports" / "dbh").glob("dbh-insights-*.xlsx"))
assert len(files) == 2, files   # keep newest 2
assert zipfile.ZipFile(files[-1]).read("xl/workbook.xml")
csv_s = {**sched, "export": "dbh-insights:csv", "frequency": "monthly", "monthday": "last", "param_sheet": ""}
assert "needs sheet" in client.post("/settings/exports", data=csv_s).text
r = client.post("/settings/exports", data={**csv_s, "param_sheet": "vHost"})
assert "Monthly on the last day" in r.text and "Schedule saved and verified" in r.text, r.text[:400]
assert len(core.storage.export_schedules()) == 1, "saving again updates the one schedule"
assert 'value="vHost"' in client.get("/settings").text
assert "wrote dbh-insights-vHost-" in client.post(f"/settings/exports/{sid}/run", data={"token": "test-token"}).text
assert "paused" in client.post(f"/settings/exports/{sid}/toggle", data={"token": "test-token"}).text
client.post(f"/settings/exports/{sid}/delete", data={"token": "test-token"})
assert not core.storage.export_schedules()
from core import export_schedule as xs  # noqa: E402
assert xs.describe({"frequency": "monthly", "monthday": "last", "at_time": "01:00", "timezone": "UTC"}) == "Monthly on the last day at 01:00 UTC"
print("  scheduled exports: ok")
# Folder picker: list, create (token needed), refuse escapes
d = client.get("/settings/folders").json()
assert "dirs" in d and d["path"] == "" and d["parent"] is None, d
assert client.post("/settings/folders", data={"path": "", "name": "picked", "token": ""}).status_code == 403
d = client.post("/settings/folders", data={"path": "", "name": "picked", "token": "test-token"}).json()
assert d["path"] == "picked" and d["parent"] == "", d
d = client.post("/settings/folders", data={"path": "picked", "name": "sub one", "token": "test-token"}).json()
assert d["path"] == "picked/sub one" and d["parent"] == "picked", d
assert "picked" in client.get("/settings/folders").json()["dirs"]
assert client.get("/settings/folders", params={"path": "../.."}).status_code == 400
assert client.post("/settings/folders", data={"path": "", "name": "../x", "token": "test-token"}).status_code == 400
assert 'id="x-browse"' in client.get("/settings").text
# Edit an existing schedule: summary + Edit button, form opens with saved values, saving changes it
client.post("/settings/exports", data=sched)
page = client.get("/settings").text
assert 'id="x-edit"' in page and "Create schedule" not in page and "Save changes" in page
assert 'id="add-export" class="sched-form is-editing" hidden' in page, "form is closed until Edit is clicked"
page = client.get("/settings?edit=1").text
assert 'id="add-export" class="sched-form is-editing" >' in page and 'value="06:30"' in page
r = client.post("/settings/exports", data={**sched, "at_time": "18:15", "frequency": "daily"})
assert "Schedule saved and verified" in r.text and "Daily at 18:15" in r.text
assert len(core.storage.export_schedules()) == 1 and core.storage.export_schedules()[0]["at_time"] == "18:15"
client.post(f"/settings/exports/{core.storage.export_schedules()[0]['id']}/delete", data={"token": "test-token"})
assert "Create schedule" in client.get("/settings").text
# Apps on / off (./control/apps.json shared with Ask): identical copies, pause and resume.
import filecmp, tempfile  # noqa: E401,E402
assert filecmp.cmp(root / "core" / "control.py", root / "ask" / "app" / "control.py", shallow=False), "control.py copies differ"
from core import control as ctl  # noqa: E402
os.environ["VCO_CONTROL_FILE"] = tempfile.mkdtemp() + "/apps.json"
assert ctl.is_on("observability") and ctl.is_on("ask")
r = client.post("/settings/apps", data={"ask": "1"}, follow_redirects=False)
assert r.status_code == 303 and "valid+API+token" in r.headers["location"], r.headers
assert ctl.is_on("observability")
r = client.post("/settings/apps", data={"ask": "1", "token": "test-token"}, follow_redirects=False)
assert r.status_code == 303 and not ctl.is_on("observability") and ctl.is_on("ask")
assert "is paused" in client.get("/overview").text
assert core.run_module(next(m for m in core.modules.values() if m.available)) == []
r = client.post(f"/modules/{next(m.name for m in core.modules.values() if m.available)}/run", follow_redirects=False)
assert r.status_code == 303 and "where=apps" in r.headers["location"]
r = client.put("/api/core/apps/ask?on=false", headers=auth)
assert r.status_code == 200 and not ctl.is_on("ask")
client.post("/settings/apps", data={"observability": "1", "ask": "1", "token": "test-token"})
assert ctl.is_on("observability") and ctl.is_on("ask") and "is paused" not in client.get("/overview").text
assert client.get("/api/core/apps", headers=auth).json()["writable"]
os.environ["VCO_CONTROL_FILE"] = "/nonexistent/apps.json"
r = client.post("/settings/apps", data={"ask": "1", "token": "test-token"}, follow_redirects=True)
assert "not mounted" in r.text and ctl.is_on("observability")
del os.environ["VCO_CONTROL_FILE"]
print("  apps on/off: ok")
# Performance: one dashboard on Ask; this container serves its counters and links/redirects there.
raw = client.get("/perf/container").json()
assert raw["app"] == "observability" and "net_rx" in raw, raw
ask = os.environ.get("VCO_ASK_URL", "")
r = client.get("/performance", follow_redirects=False)
if ask:
    assert r.status_code == 302 and r.headers["location"] == ask.rstrip("/") + "/performance", r.headers
    assert f'href="{ask}/performance"' in client.get("/overview").text
else:
    assert r.status_code == 404 and "/performance" not in client.get("/overview").text
print("  performance: ok")
print("ALL OK")
