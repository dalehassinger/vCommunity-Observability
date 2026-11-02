# vCommunity-Observability

An open-source framework that collects data from **vCenter** and **VCF Operations**, stores it locally in SQLite, and makes it useful in two ways:

- **Custom dashboards** served by the app itself, styled after VCF Operations (Clarity look, light and dark theme). No Grafana and no CDN, so it works at dark sites.
- **REST endpoints for VCF Operations Management Pack Builder**, so MP Builder can pull the data back into VCF Operations.

The core stays small. Every feature is an **add-on module** that can be written in **Python or PowerShell**, switched on or off, and patched on its own.

> Status: prototype (v0.1.0). Seven modules are included. Every module has been tested with built-in sample data; live collection against a lab vCenter has not been verified for every module yet. See [What's next](#whats-next).

![DBH Insights dashboard](docs/screenshots/dbh-insights.png)

## Contents

- [Requirements and design goals](#requirements-and-design-goals)
- [How it fits together](#how-it-fits-together)
- [Quick start](#quick-start)
- [Using the web UI](#using-the-web-ui)
- [Modules included](#modules-included)
- [Configuration reference](#configuration-reference)
- [Docker image and runtime](#docker-image-and-runtime)
- [Using it from MP Builder](#using-it-from-mp-builder)
- [Core API](#core-api)
- [Writing a module](#writing-a-module)
- [Security](#security)
- [Patching and updates](#patching-and-updates)
- [Dark sites](#dark-sites)
- [Troubleshooting](#troubleshooting)
- [Testing](#testing)
- [Repository layout](#repository-layout)
- [What's next](#whats-next)

## Requirements and design goals

| Goal | How it is met |
| --- | --- |
| Cross-platform | One container image (amd64 and arm64, including Apple Silicon), or run directly with Python 3.11+ on Windows, macOS or Linux |
| No Grafana | Dashboards are server-rendered HTML from the app itself |
| Works offline / at dark sites | No CDN links or runtime downloads; Swagger UI is vendored; images can be saved as tarballs |
| Open source | FastAPI, SQLite, APScheduler, pyVmomi, Jinja2, PowerShell 7 (PowerCLI is the one accepted non-open-source exception) |
| Easy to patch | Locked, hashed dependencies; layered Dockerfile; each module is a folder that can be replaced on its own |
| Language choice | Modules can be Python **or** PowerShell; the core never forces one |

## How it fits together

```
 vCenter / VCF Operations / any REST source
            │  (pyVmomi, PowerCLI, REST)
            ▼
 ┌──────────────────── vCommunity-Observability container ────────────────────┐
 │  Scheduler ──runs──▶ module collectors (.py or .ps1) ──JSON──▶ SQLite      │
 │      │                                                       (1 db/module) │
 │      └──runs──▶ scheduled exports (XLSX/CSV) ──▶ /app/exports              │
 │  FastAPI ──▶ /api/<module>/...      REST for MP Builder (token auth)       │
 │          ├─▶ /dash/<module>/...     dashboards (server-rendered HTML)      │
 │          ├─▶ /export/<module>/...   file downloads                         │
 │          └─▶ /settings, /modules, /docs                                    │
 └────────────────────────────────────────────────────────────────────────────┘
```

- **One container, one job.** Uvicorn (FastAPI) is the single main process. The scheduler runs inside it, and collectors and exports are short-lived child processes that start, write JSON and exit. SQLite is one file per module, so a single container avoids cross-container locking and keeps dark-site installs to one image. Split it only if you move to Postgres, need HA on Kubernetes, or want PowerCLI in its own image.
- **Collectors** are plain scripts. The core passes connection details in environment variables and reads one JSON object back. That is the whole contract, which is why a module can be Python or PowerShell.
- **Storage** is one SQLite file per module in `data/`, plus `data/_core.db` for core state (run history, module switches and order, settings saved in the UI, export schedules).

## Quick start

### Try it with sample data (no vCenter needed)

```bash
python3 -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp config/config.example.yaml config/config.yaml
VCO_MOCK=1 uvicorn core.app:create_app --factory --port 8080
```

```powershell
# PowerShell equivalent
$env:VCO_MOCK = '1'
uvicorn core.app:create_app --factory --port 8080
```

Open http://localhost:8080. A **Sample data** badge shows in the header while mock mode is on. The `host-health` module needs PowerShell 7 (`pwsh`) on the PATH; PowerCLI is only needed for live data.

### Run against your lab with Docker

```bash
cp config/config.example.yaml config/config.yaml   # set your vCenter host and user
cp .env.example .env                               # set passwords and the API token
mkdir -p data exports extra-modules && chmod 777 exports
docker compose up -d --build
```

Open http://localhost:8080. Passwords and tokens are never written to `config.yaml`: any key ending in `_env` names an environment variable (set in `.env`) holding the real value. After the first start you can change the vCenter address, user, password and collection intervals on the **Settings** page instead.

Works with Docker Desktop and **Rancher Desktop** (set Container Engine to `dockerd (moby)`, then `docker context use rancher-desktop`). With the `containerd` engine use `nerdctl compose up -d --build` instead.

## Using the web UI

The left sidebar lists every enabled module's dashboards (in the order set on the Modules page), then **Modules & API**, **Settings** and **API docs (Swagger)**. The moon icon in the header switches light and dark theme; the choice is remembered in the browser.

### Home

A tile for every dashboard of every enabled module, in menu order.

### Modules & API

![Modules page](docs/screenshots/modules.png)

- Every module with its version, language, collectors, last run, row count and any error.
- **On/Off switch** per module. Off stops its collectors and hides its dashboards and API endpoints (they return 404). Saved in `data/_core.db`, so it survives restarts and upgrades.
- **↑ ↓** buttons set the menu order used by the sidebar, home page and this page. New modules appear at the bottom.
- **Run collectors now** runs a module's collectors immediately (dashboards have a **Refresh** button that does the same).
- The full list of each module's REST endpoints and downloads.

### Settings

![Settings page](docs/screenshots/settings.png)

**Connections.** Edit the vCenter and VCF Operations address, username, password and TLS check. **Save and test** saves and checks the login straight away. Values saved here are stored in `data/_core.db`, win over `config.yaml` and `.env`, and apply from the next collection without a restart. **Reset to config.yaml** drops them. Leaving the password blank keeps the current one.

**Collection intervals.** How often each collector runs, 1 to 10080 minutes. A new interval starts counting from the moment you save.

**Scheduled export.** One schedule that runs a module export (for example the DBH Insights XLSX, or one sheet as CSV) and writes it to a folder:

- **Daily, weekly or monthly** (day of week, or day of month 1 to 28 or the last day), at a time and time zone you pick. The default time zone comes from `TZ`.
- **Folder** under `/app/exports`, which Compose maps to `./exports` next to `docker-compose.yml`. **Browse…** opens a folder picker where you can open folders, go up a level and create a new folder. (A browser can't hand a web app a real path on your computer, and the container only sees the exports folder, so the picker browses that folder.)
- **Keep newest N** files; older ones made by this schedule are deleted. 0 keeps all.
- Once saved, the card shows a summary (schedule, export, folder, next run, last run) with **Edit**, **Run now**, **Pause** and **Remove**. **Edit** opens the form filled in with the current values; **Save changes** updates the same schedule.
- Every save is **verified**: the schedule is read back from the database and compared with what you entered, the folder is write-tested, and the scheduler is checked for the job. A green **Schedule saved and verified** box lists those checks and the next run. If anything fails, a red message in the card says what, and the form keeps what you typed.
- Files are written as `.part` and renamed when complete, so a half-written file never appears.

![Schedule saved and verified](docs/screenshots/save-verified.png)

When API tokens are configured, every save on this page needs one of them (there is no UI login yet). The token field sits at the bottom of each card.

### API docs

`/docs` is Swagger UI (vendored, works offline) for every endpoint. `/openapi.json` is the machine-readable spec.

## Modules included

| Module | Language | What it collects and shows | API endpoints (`/api/<module>...`) |
| --- | --- | --- | --- |
| `datastore-capacity` | Python (pyVmomi) | Capacity, free, provisioned and VM count per datastore, with 90 days of history for a runway estimate (appears after an hour of history). Widget dashboard | `/datastores`, `/datastores/critical`, `/runway` |
| `host-health` | PowerShell (PowerCLI) | Host status, alarms, maintenance mode, CPU and memory %, uptime, version. Widget dashboard with VCF Operations-style KPI cards | `/hosts`, `/hosts/unhealthy`, `/clusters` |
| `vcenter-inventory` | Python (pyVmomi) | Clusters, hosts and VMs (config, usage, disks, NICs, IPs, snapshots, notes) in a vCenter-style inventory browser with a tree, object pages and tabs | `/vms`, `/vms/powered-off`, `/vms/with-snapshots`, `/hosts`, `/clusters` |
| `github-status` | Python (requests) | GitHub service status from githubstatus.com (any Atlassian Statuspage site via the `url` setting): summary cards, one status card per service, open and recent incidents, maintenance. Needs internet or `HTTPS_PROXY` | `/status`, `/components`, `/components/degraded`, `/incidents`, `/incidents/active`, `/maintenances` |
| `dbh-insights` | Python (pyVmomi) | The [DBH Insights](https://github.com/DBH-Insights/DBH-Insights) inventory: 27 sheets plus Cluster Capacity. Insights and Sheets pages, **Export XLSX** and CSV per sheet | `/<sheet>` for every sheet (`/vinfo`, `/vhost`, `/cluster-capacity`...), `/sheets`, `/vhealth/by-type`, `/export/xlsx`, `/export/csv?sheet=vInfo` |
| `dbh-topology` | Python (pyVmomi) | DBH Insights **Topology**: hosts by cluster, datastores and every mount between them, with **Download HTML** | `/hosts`, `/datastores`, `/mounts`, `/single-host-datastores` |
| `dbh-impact` | Python (pyVmomi) | DBH Insights **Impact**: what stops if one host or datastore fails, and whether HA could restart it | `/hosts`, `/datastores`, `/critical`, `/single-points-of-failure` |

All vCenter modules use the shared `vcenter` connection. Configure one or many vCenters in `config.yaml` (`connections.vcenter` flat form, or `instances:`) or on the Settings page. Collectors receive every enabled instance as `VCO_VCENTERS` (JSON) and stamp rows with a `vcenter` id so names and morefs do not collide. A module can limit itself with settings `vcenters: [id, ...]`.

### vSphere Inventory

![vSphere Inventory, host page](docs/screenshots/inventory-host.png)

A tree of datacenter, clusters, hosts and VMs on the left; object pages with summary cards and tabs (Hosts, VMs, Datastores, Networks, Snapshots) on the right. Tags and storage policies are not collected yet (they need the vSphere REST API).

### Host Health

![Host Health](docs/screenshots/host-health.png)

The reference PowerShell module: `Collect-HostHealth.ps1` uses PowerCLI and shows how a `.ps1` collector follows the same contract as a Python one.

### GitHub Status

![GitHub Status](docs/screenshots/github-status-cards.png)

An overall status banner, a summary row (services, not operational, under maintenance, open incidents, maintenance windows, operational %), then a card per service with a coloured top edge and status ring. A service with an open incident links to it. Open and recent incidents are listed below.

### DBH Insights

`modules/dbh-insights` is a Python port of the DBH Insights website's query and export code (`collect/js/sheets.js`, `vim.js`, `xlsx.js`). The same columns, units, rounding and rules (vCLS VMs left out, "not reported" stays blank rather than 0) are used, but the data is collected on a schedule by the container with pyVmomi instead of from the browser through the desktop helper, so no helper app is needed and it works at dark sites.

- **Sheets:** vInfo, vCPU, vMemory, vDisk, vPartition, vNetwork, vSnapshot, vCD, vUSB, vTools, vHost, vCluster, vRP, vDatastore, vHealth, vHBA, vNIC, vSwitch, vPort, vSC_VMK, dvSwitch, dvPort, vHost Performance, vInfo Performance, vSource, vLicense, vMetaData, plus Cluster Capacity.
- **Insights** page: hosts, cores, DRAM, storage, VMs, vCPU:core ratio, storage utilisation, capacity by datastore type, fullest datastores, cluster capacity with the N+1 memory check, and vHealth findings.
- **Sheets** page: every sheet with sort, filter, paging, the **totals row** (sums, averages and ratios worked out the same way as the website) and **Group by**.

  ![DBH Sheets grouped](docs/screenshots/dbh-sheets-groupby.png)

- **Export XLSX:** one workbook with every sheet, Verdana 9pt, black header row, frozen first row and column, AutoFilter, real Excel dates, in the inventory tool's sheet order. Also at `/api/dbh-insights/export/xlsx` (token required) and as a scheduled export.
- Large inventories are read with the PropertyCollector and continuation tokens, so nothing is silently truncated. A property an older vCenter doesn't know is dropped and logged, and its column stays empty.
- The website's API Explorer is not ported. Multi-vCenter collection is supported here (Settings / `VCO_VCENTERS`); the website's combined multi-VC UI chrome is not replicated sheet-for-sheet.

To change a sheet, edit `sheets.py`, then run `python sheets.py --tables` in the module folder and paste the output over the `tables:` section of `module.yaml`. The smoke test fails if they don't match.

### DBH Topology

![DBH Topology](docs/screenshots/dbh-topology.png)

Port of the Topology view (`collect/js/insights.js` and `report.js`). Hosts are grouped by cluster (plus standalone hosts) on the left, datastores on the right with a capacity bar, and a line for each mount, coloured by datastore type (VMFS, NFS, vSAN, vVol, other). Hover a host or datastore to isolate its connections. Datastore and host tables sit below the map. **Download HTML** saves a standalone report, in the website's dark style, for emailing or archiving.

### DBH Impact

![DBH Impact](docs/screenshots/dbh-impact.png)

Port of the Impact view (`collect/js/impact.js`). Pick a host or datastore under **If this fails…**:

- **Host:** how many running VMs stop, whether vSphere HA would restart them (standalone host, HA off, no surviving hosts, or not enough uncommitted memory all mean they stay down), memory and vCPUs to restart, hosts left in the cluster, spare memory after the failure, datastores only that host can reach, and VMs on other hosts that use that storage.
- **Datastore:** how many running VMs crash, which have every file there versus split across datastores, which hosts mount it, and the capacity that goes offline.
- A table of **every host and datastore, worst first** (critical, warning, low).

Each collection answers the question for every host and datastore, so the answers are also REST endpoints for MP Builder (`/api/dbh-impact/hosts` gives severity, VMs running, memory to restart and headroom per host). Memory figures use assigned VM memory, which is what an HA restart admits against, so they are estimates.

`dbh-topology` and `dbh-impact` share one collector library, `fabric.py`. Each module keeps an identical copy so it stays self-contained; the smoke test fails if the copies drift. HA and DRS state come from the same vSphere session (`configuration.dasConfig.enabled`), so no extra credentials are needed.

## Configuration reference

### `config/config.yaml`

| Key | Meaning |
| --- | --- |
| `data_dir` | Where SQLite files go (default `data`; `/app/data` in Docker). `VCO_DATA_DIR` overrides it |
| `modules_dirs` | Folders scanned for modules. Later folders win, so a module in `/modules` overrides the built-in copy |
| `mock` | `true` = built-in sample data, no vCenter needed. `VCO_MOCK=1` overrides it |
| `connections.<name>` | `host`, `user`, `password_env` (name of the env var holding the password), `verify_ssl`, plus anything else a module needs (for example `auth_source` for VCF Operations). Included: `vcenter`, `vcfops`. For several vCenters, set `connections.vcenter.instances` to a list of `{id, label, host, user, password_env, verify_ssl}` (or add them on Settings) |
| `api.tokens` | List of `{name, token_env}`. Callers send a token as `Authorization: Bearer <token>` or `X-API-Key: <token>`. With no tokens, the API is open |
| `modules.<name>.enabled` | Default on/off (the switch on the Modules page wins) |
| `modules.<name>.settings` | Module settings, passed to collectors as `VCO_SETTINGS`. For example `vcenter_name` / `vcenters` for the DBH modules, `url` for `github-status` |

Precedence for module on/off: `enabled_by_default` in `module.yaml` → `modules.<name>.enabled` in `config.yaml` → the switch. Precedence for connection values: `config.yaml` and `.env` → values saved on the Settings page.

### `.env`

| Variable | Meaning |
| --- | --- |
| `VCO_VCENTER_PASSWORD` | vCenter password (named by `password_env` in `config.yaml`) |
| `VCO_VCFOPS_PASSWORD` | VCF Operations password |
| `VCO_API_TOKEN_MPBUILDER` | API token for MP Builder and scripts. Use a long random string |
| `VCO_MOCK` | `1` = sample data |
| `PWSH_VERSION` | PowerShell build in the `core-pwsh` image (default 7.6.9 LTS) |
| `DOTNET_HWINTRINSIC` | `0` turns off .NET CPU fast paths for VMs that hide CPU features (see [Troubleshooting](#troubleshooting)). Default 1 |

### Other environment variables

| Variable | Meaning |
| --- | --- |
| `VCO_CONFIG` | Path to `config.yaml` (default `config/config.yaml`) |
| `VCO_EXPORT_DIR` | Root folder for scheduled exports (default `exports`; `/app/exports` in Docker) |
| `TZ` | Default time zone offered for scheduled exports (for example `America/New_York`) |

## Docker image and runtime

Two image targets from one `Dockerfile`:

| Target | Contents | Use |
| --- | --- | --- |
| `core` | Python 3.13 slim, the app and its locked dependencies | Python modules only (smallest) |
| `core-pwsh` | `core` plus PowerShell 7 and VMware PowerCLI | Default in `docker-compose.yml`; needed for `host-health` and other `.ps1` modules |

- Runs as a non-root user (`vco`, uid 10001), listens on port 8080, with a health check on `/api/core/health`.
- The right PowerShell build (arm64 or x64) is picked automatically. Pin PowerCLI with the `POWERCLI_VERSION` build argument.
- Compose volumes: `./data` → `/app/data` (SQLite, survives upgrades), `./exports` → `/app/exports` (scheduled exports), `./extra-modules` → `/modules` (new or patched modules), `./config/config.yaml` read-only.
- `init: true` adds a tiny init process that reaps finished collector processes and passes stop signals to the app.
- `restart: unless-stopped`.

Common commands:

```bash
docker compose up -d --build                 # build and start (after any code or module change)
docker compose logs -f                       # follow the logs
docker compose build --no-cache              # full rebuild, for example after changing PWSH_VERSION
docker compose down                          # stop (data, exports and settings stay on disk)
```

## Using it from MP Builder

1. In MP Builder, create a source with base URL `http://<host>:8080`.
2. Authentication: custom header `X-API-Key: <token>` (or `Authorization: Bearer <token>`).
3. Add requests for endpoints such as `/api/host-health/hosts` or `/api/dbh-impact/hosts`. Every endpoint returns:

```json
{ "module": "host-health", "endpoint": "/hosts", "collected_at": "2026-11-01 17:46:13",
  "count": 6, "items": [ { "name": "esx01.vcrocs.local", "cpu_usage_pct": 42.5, ... } ] }
```

4. Use `items` as the list path and `name` as the object identifier.

The full list of endpoints is on the **Modules** page, in Swagger at `/docs`, and in `/openapi.json`.

## Core API

| Method and path | Auth | Purpose |
| --- | --- | --- |
| `GET /api/core/health` | none | Liveness check (used by the container health check) |
| `GET /api/core/modules` | token | Every module with its status, collectors, last runs and endpoints |
| `PUT /api/core/modules/{module}/enabled?enabled=false` | token | The on/off switch |
| `PUT /api/core/modules/order` | token | Menu order, body is a JSON list of module names |
| `GET /api/{module}/...` | token | Module endpoints declared in `module.yaml` |
| `GET /api/{module}/export/{name}` | token | Module file downloads for scripts |
| `GET /export/{module}/{name}` | none (UI) | The same downloads for the UI |

```bash
curl -X PUT -H "X-API-Key: $TOKEN" "http://localhost:8080/api/core/modules/github-status/enabled?enabled=false"
curl -X PUT -H "X-API-Key: $TOKEN" -H "Content-Type: application/json" \
  -d '["vcenter-inventory", "host-health", "github-status"]' http://localhost:8080/api/core/modules/order
```

## Writing a module

A module is a folder with a `module.yaml` and one or more collector scripts:

```
modules/my-module/
  module.yaml
  collect.py        # or Collect-Something.ps1
  templates/        # optional custom pages
```

**`module.yaml`** declares everything. No web code is needed:

```yaml
name: my-module
version: 0.1.0
core_api: 1                       # module contract version
description: What this module does

collectors:
  - name: things
    script: collect.py            # .py runs with Python, .ps1 with pwsh
    interval_minutes: 15
    timeout_seconds: 300
    connections: [vcenter]        # which connection details to pass in
    tables: [things]

tables:
  things:
    mode: snapshot                # snapshot = replace each run, append = keep history (retention_days, default 90)
    columns: {name: text, value: real}

api:                              # GET /api/my-module/things
  - path: /things
    table: things
  - path: /things/high
    query: SELECT * FROM things WHERE value > 90

dashboards:                       # /dash/my-module/overview
  - slug: overview
    title: My Module
    widgets:
      - {type: kpi, title: Things, query: SELECT COUNT(*) FROM things}
      - {type: bar, title: Value, max: 100, warn: 75, crit: 90,
         query: SELECT name AS label, value FROM things}
      - {type: table, title: All things, query: SELECT * FROM things}
```

Every table gets a `collected_at` column automatically. Schema changes are additive: new columns are added on start.

**Custom page templates.** When the widget grid isn't enough (trees, maps, object pages, tabs), a dashboard can name a Jinja template in the module's `templates/` folder plus named read-only queries. The template extends the core `base.html` and gets `data.<query>` rows, the URL query string as `q`, `last_collected`, `run_errors` and `query_errors`. Named queries run only when the template reads them, so a page can declare one per table and pay for one. See `vcenter-inventory`, `dbh-topology` and `dbh-impact` for full examples:

```yaml
dashboards:
  - slug: inventory
    title: vSphere Inventory
    template: inventory.html          # modules/vcenter-inventory/templates/inventory.html
    queries:
      vms: SELECT * FROM vms ORDER BY name
      hosts: SELECT * FROM hosts
```

**Collector contract** (same for Python and PowerShell):

| | |
| --- | --- |
| Input | Environment variables: `VCO_VCENTER_HOST`, `VCO_VCENTER_USER`, `VCO_VCENTER_PASSWORD`, `VCO_VCENTER_VERIFY_SSL` (first/selected vCenter; and `VCO_VCFOPS_*` etc. for each other connection listed), `VCO_VCENTERS` (JSON array of every selected vCenter instance when `connections` includes `vcenter`), `VCO_SETTINGS` (module settings as JSON), `VCO_MODULE`, `VCO_MOCK` (`1` = return sample data) |
| Output | One JSON object keyed by table name, `{"things": [{"name": "a", "value": 1}]}`, written to the file in `VCO_OUTPUT_FILE` (recommended, so stray console output such as PowerCLI warnings can't corrupt it) or printed to stdout |
| Logs | stderr. In PowerShell use `[Console]::Error.WriteLine()`, not `Write-Host` |
| Failure | Non-zero exit code. The error shows on the Modules page and the module's dashboard |

Manifest queries are `SELECT`-only and run on a read-only SQLite connection.

**pyVmomi tip:** never compare a managed object with `==` against a string (`vm.runtime.host == ""`). pyVmomi's `__eq__` assumes both sides are managed objects and raises `AttributeError: 'str' object has no attribute '_moId'`. Use `getattr(obj, "_moId", None)` instead.

**Exports (file downloads).** A module can offer files built on demand from its data, such as an XLSX workbook or a CSV, written in Python or PowerShell:

```yaml
exports:
  - name: xlsx
    title: Export XLSX
    script: export.py               # or Export-Report.ps1
    filename: my-module-{timestamp}.xlsx
    media_type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet
  - name: csv
    script: export.py
    params: [sheet]                 # allowed URL parameters, usable in the filename as {sheet}
    filename: my-module-{sheet}.csv
    media_type: text/csv
```

The script gets `VCO_DB` (the module's SQLite file; open it read-only), `VCO_EXPORT` (which export), `VCO_PARAMS` (the allowed URL parameters as JSON) and `VCO_SETTINGS`, and writes the file to `VCO_OUTPUT_FILE`. Every export can also be picked on the Settings page as the scheduled export.

**Installing a module:** copy the folder into the `/modules` volume (`./extra-modules` with Compose) and restart. A module in `/modules` with the same name overrides the built-in copy, which is also how a patched module is shipped to a dark site. Built-in modules are baked into the image, so changes to `modules/` need `docker compose up -d --build`.

## Security

- The REST API requires a token whenever `api.tokens` is set. Tokens are compared in constant time.
- The web UI has no login yet. Changes on the Settings page need an API token when tokens are configured; dashboards and **Run now** are open. Keep the port on a management network until UI login lands.
- Secrets live in `.env`, never in `config.yaml`. A password saved on the Settings page is stored **unencrypted** in `data/_core.db`, so protect the `data` folder as you would `.env`.
- The container runs as a non-root user. Collectors only receive the connections they list in `module.yaml`.
- `verify_ssl: false` is the default for lab use; turn it on where vCenter has a trusted certificate.

## Patching and updates

- `requirements.in` lists direct dependencies. `requirements.txt` is the lock file with hashes. To patch one package: `uv pip compile --generate-hashes --upgrade-package fastapi requirements.in -o requirements.txt` (or the same flags with `pip-compile`), rebuild, and release a new image tag.
- The Docker build uses `--require-hashes`, so only the exact locked packages install.
- Layer order (OS → Python deps → PowerShell → app code) keeps rebuilds fast; patching a Python package doesn't re-download PowerShell.
- PowerShell is pinned with `PWSH_VERSION`, PowerCLI optionally with `POWERCLI_VERSION`.
- A single module is patched by dropping its folder into `/modules` (see above).
- SQLite data lives on the `/app/data` volume and survives image upgrades.

## Dark sites

- No runtime internet access is needed: the UI has no JavaScript frameworks or CDN links, and Swagger UI's files are vendored into the image. (`github-status` is the one module that needs internet, by design; switch it off at a dark site.)
- Build a bundle on a connected machine: `./scripts/New-OfflineBundle.ps1 -Version 0.1.0 -Target core-pwsh`. It saves the image as a gzipped tarball with a `.sha256`.
- At the dark site: verify the `.sha256`, then `docker load -i vco-core-pwsh-0.1.0.tar.gz` and `docker compose up -d`.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| Scheduled export won't save: "Can't write to /app/exports/..." | The host `./exports` folder isn't writable by the container user. Run `chmod 777 exports` (or `chown 10001 exports`) |
| "Not saved: enter a valid API token" | Enter the token from `.env` (`VCO_API_TOKEN_MPBUILDER`) in the card's API token field |
| `failed to connect to the docker API at unix:///.../docker.sock` | The Docker CLI points at an engine that isn't running. `docker context ls`, then `docker context use rancher-desktop` (or start Docker Desktop) |
| Image build fails in `core-pwsh` with `Illegal instruction` (exit 132) | PowerShell's .NET runtime hit a CPU feature the VM doesn't expose (seen on new Apple Silicon). Use the default `PWSH_VERSION=7.6.9`; if it still fails, try `DOTNET_HWINTRINSIC=0`, `PWSH_VERSION=7.4.13` in `.env`, or set Rancher Desktop's VM emulation to VZ. Or build `target: core` to skip PowerShell |
| `AttributeError: 'str' object has no attribute '_moId'` in a collector | A managed object was compared with `==`; see the pyVmomi tip under [Writing a module](#writing-a-module) |
| vCenter password is empty | `config.yaml` must have `password_env: VCO_VCENTER_PASSWORD` (the variable's name), with the password in `.env`, or set it on the Settings page |
| Dashboard shows "collector failed" | The message is the collector's stderr. **Run collectors now** on the Modules page (or **Refresh** on the dashboard) retries; `docker compose logs -f` shows more |
| Changes to modules or templates don't show | Built-in modules are in the image: `docker compose up -d --build` |

## Testing

```bash
python scripts/smoke_test.py     # runs every module with sample data, checks every endpoint, page, export and Settings flow
```

The smoke test also checks that the DBH sheet tables match `module.yaml`, that the two `fabric.py` copies are identical, that Impact gives the same answers as the original website for the sample lab, and that schedules save, verify, edit, run, pause and remove correctly.

## Repository layout

```
core/                   FastAPI app, scheduler, module loader, runner, storage
  app.py                routes: dashboards, API, exports, Settings, Modules
  config.py             config.yaml + _env secret resolution
  connections.py        connection fields and "Save and test"
  export_schedule.py    scheduled exports: validation, triggers, folder picker, pruning
  modules.py            module.yaml parsing and validation
  runner.py             runs collectors and exports as subprocesses
  storage.py            SQLite: one db per module + _core.db
  templates/            base layout, home, dashboards, Modules, Settings
  static/               CSS and vendored Swagger UI
modules/                built-in modules (one folder each)
config/                 config.example.yaml
scripts/                smoke_test.py, New-OfflineBundle.ps1
docs/screenshots/       screenshots used in this README
Dockerfile              core and core-pwsh image targets
docker-compose.yml      single-container deployment
requirements.in/.txt    direct dependencies / hashed lock file
.env.example            secrets and build settings template
```

## What's next

- Login for the dashboard UI (the API already requires a token; the UI and the "Run now" button are open in this prototype)
- A VCF Operations example module (alerts or recommendations via the Suite API)
- Script-backed API routes (a `.py` or `.ps1` handler for an endpoint, not just a query)
- CI: Dependabot or Renovate, `pip-audit`, and Trivy image scans
- Trend charts (line widget) using `append` tables

## Changelog

| Date | Change |
| --- | --- |
| 2026-11-02 | Multi-vCenter support (Settings instances, `VCO_VCENTERS`, `vcenter` identity on rows); `dbh-topology` and `dbh-impact` modules; GitHub Status redesigned with VCF Operations-style cards; PowerShell 7.6.9 LTS default with `PWSH_VERSION` and `DOTNET_HWINTRINSIC` settings; `init: true` in Compose; this README rewritten |
| 2026-11-01 | Settings page (connections, intervals); scheduled export with folder picker, edit and verified save; `dbh-insights` module with XLSX/CSV export; module on/off switches and menu order; offline Swagger UI; `vcenter-inventory` and `github-status` modules; VCF Operations look with light and dark theme; first prototype with `datastore-capacity` and `host-health` |
