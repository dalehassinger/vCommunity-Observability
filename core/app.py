"""FastAPI application: REST API for MP Builder plus the dashboard UI.

Everything the UI needs (CSS, no JS frameworks, no CDN) ships inside the image,
so the app works the same at connected and dark sites.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Depends, FastAPI, HTTPException, Request, Security
from fastapi.security import APIKeyHeader
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import CORE_API, CORE_VERSION
from .config import Config, load_config
from .connections import LABELS, as_bool, fields_for, test_connection
from . import export_schedule as xs
from .modules import Module, discover
from .runner import run_collector, run_export
from .storage import Storage, utcnow
from .vcenters import mirror_primary, normalize_vcenter, valid_id

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("vco")
HERE = Path(__file__).parent
def asset_version(*folders: Path) -> str:
    """Short fingerprint of the static files (names, sizes, times). Added to script and
    stylesheet URLs as ?v=..., so browsers fetch new files after an update instead of
    running a cached copy."""
    import hashlib
    h = hashlib.sha1()
    for folder in folders:
        for p in sorted(Path(folder).rglob("*")):
            if p.is_file():
                st = p.stat()
                h.update(f"{p.relative_to(folder)}:{st.st_size}:{st.st_mtime_ns};".encode())
    return h.hexdigest()[:10]

REPO_URL = "https://github.com/dalehassinger/vCommunity-Observability"


def find_readme() -> Path | None:
    """README.md for the About page: /app/README.md in the image, the repo root otherwise."""
    for p in (os.environ.get("VCO_README"), HERE.parent / "README.md"):
        if p and Path(p).is_file():
            return Path(p)
    return None


class LazyQueries:
    """Named dashboard queries that run only when a template reads them, so a
    page can declare many queries (one per sheet, say) and pay for one."""

    def __init__(self, storage: Storage, module: str, queries: dict[str, str], errors: list[str]):
        self._storage, self._module, self._queries, self._errors = storage, module, queries, errors
        self._cache: dict[str, list[dict[str, Any]]] = {}

    def __getitem__(self, name: str) -> list[dict[str, Any]]:
        if name not in self._queries:
            raise KeyError(name)
        if name not in self._cache:
            try:
                self._cache[name] = self._storage.query(self._module, self._queries[name])
            except Exception as exc:
                self._cache[name] = []
                self._errors.append(f"{name}: {exc}")
        return self._cache[name]

    def __getattr__(self, name: str) -> list[dict[str, Any]]:
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None

    def __contains__(self, name: object) -> bool:
        return name in self._queries

    def get(self, name: str, default: Any = None) -> Any:
        return self[name] if name in self._queries else default

    def keys(self):
        return self._queries.keys()


class Core:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.storage = Storage(cfg.data_dir)
        self.modules: dict[str, Module] = discover(cfg.modules_dirs, cfg.modules)
        self.scheduler = BackgroundScheduler(timezone="UTC")
        # Normalize vcenter to always expose instances[] (file may still be flat).
        if "vcenter" in cfg.connections:
            cfg.connections["vcenter"] = mirror_primary(normalize_vcenter(cfg.connections["vcenter"]))
        # Connection details as config.yaml/.env give them, kept for "Reset".
        self.file_connections = {
            k: ({**v, "instances": [dict(i) for i in v["instances"]]} if isinstance(v.get("instances"), list) else dict(v))
            for k, v in cfg.connections.items()
        }
        for name, values in self.storage.connection_settings().items():
            if name in cfg.connections and name != "vcenter":
                cfg.connections[name].update(values)
        # UI-managed vCenter list wins over config.yaml when any row was saved.
        ui_vcs = self.storage.vcenter_instances()
        if ui_vcs:
            cfg.connections["vcenter"] = mirror_primary([
                {**row, "enabled": bool(row.get("enabled", 1))} for row in ui_vcs
            ])
        elif "vcenter" in cfg.connections:
            # Flat field overlays from the old single-card Settings form still apply.
            saved_vc = self.storage.connection_settings().get("vcenter") or {}
            if saved_vc:
                flat = {**{k: v for k, v in cfg.connections["vcenter"].items() if k != "instances"}, **saved_vc}
                cfg.connections["vcenter"] = mirror_primary(normalize_vcenter(flat))
        intervals = self.storage.collector_intervals()
        # Precedence: module.yaml default < config.yaml < switch set in the UI/API.
        saved = self.storage.module_states()
        for mod in self.modules.values():
            if mod.name in saved:
                mod.enabled = saved[mod.name]
            for c in mod.collectors:
                c.interval_minutes = intervals.get((mod.name, c.name), c.interval_minutes)
            self.storage.ensure_schema(mod)
        self._apply_order(self.storage.module_order())

    def _apply_order(self, names: list[str]) -> None:
        """Reorder modules: saved order first, any new modules after, A-Z."""
        rank = {n: i for i, n in enumerate(names)}
        ordered = sorted(self.modules.values(), key=lambda m: (rank.get(m.name, len(rank)), m.name))
        self.modules = {m.name: m for m in ordered}

    def set_order(self, names: list[str]) -> None:
        self._apply_order(names)
        self.storage.set_module_order(list(self.modules))

    def move(self, name: str, step: int) -> None:
        names = list(self.modules)
        i = names.index(name)
        j = max(0, min(len(names) - 1, i + step))
        names.insert(j, names.pop(i))
        self.set_order(names)

    def start(self) -> None:
        # Jobs exist for every runnable module; disabled ones are skipped at run
        # time, so a module can be switched on or off without a restart.
        for mod in self.modules.values():
            if mod.problems:
                continue
            for c in mod.collectors:
                self.scheduler.add_job(
                    self._scheduled_run, "interval", minutes=c.interval_minutes,
                    args=[mod, c], id=f"{mod.name}:{c.name}", max_instances=1, coalesce=True,
                )
        for sched in self.storage.export_schedules():
            if sched["enabled"]:
                self.schedule_export(sched)
        self.scheduler.start()
        # First collection right away, in the background, so startup stays fast.
        threading.Thread(target=self.run_all, daemon=True).start()

    def _scheduled_run(self, mod: Module, c) -> None:
        if mod.available:
            run_collector(self.cfg, self.storage, mod, c)

    def run_all(self) -> None:
        for mod in self.modules.values():
            if mod.available:
                self.run_module(mod)

    def run_module(self, mod: Module) -> list[dict]:
        return [run_collector(self.cfg, self.storage, mod, c) for c in mod.collectors]

    def set_connection(self, name: str, values: dict[str, str]) -> None:
        self.storage.set_connection_settings(name, values)
        self.cfg.connections[name].update(values)
        log.info("connection %s updated from the UI (%s)", name, ", ".join(sorted(values)))

    def reset_connection(self, name: str) -> None:
        self.storage.reset_connection_settings(name)
        raw = self.file_connections.get(name, {})
        self.cfg.connections[name] = (
            {**raw, "instances": [dict(i) for i in raw["instances"]]}
            if isinstance(raw.get("instances"), list) else dict(raw)
        )
        log.info("connection %s reset to config.yaml", name)

    def apply_vcenters(self, instances: list[dict[str, Any]]) -> None:
        self.storage.set_vcenter_instances(instances)
        self.cfg.connections["vcenter"] = mirror_primary(instances)
        log.info("vCenter instances updated from the UI (%s)", ", ".join(i["id"] for i in instances))

    def upsert_vcenter(self, inst: dict[str, Any]) -> None:
        self.storage.upsert_vcenter_instance(inst)
        self.cfg.connections["vcenter"] = mirror_primary([
            {**row, "enabled": bool(row.get("enabled", 1))} for row in self.storage.vcenter_instances()
        ])
        log.info("vCenter instance %s saved", inst["id"])

    def delete_vcenter(self, vid: str) -> None:
        self.storage.delete_vcenter_instance(vid)
        rows = self.storage.vcenter_instances()
        if rows:
            self.cfg.connections["vcenter"] = mirror_primary([
                {**row, "enabled": bool(row.get("enabled", 1))} for row in rows
            ])
        else:
            raw = self.file_connections.get("vcenter", {})
            self.cfg.connections["vcenter"] = (
                {**raw, "instances": [dict(i) for i in raw["instances"]]}
                if isinstance(raw.get("instances"), list) else mirror_primary(normalize_vcenter(raw))
            )
        log.info("vCenter instance %s deleted", vid)

    def reset_vcenters(self) -> None:
        self.storage.reset_vcenter_instances()
        self.storage.reset_connection_settings("vcenter")
        raw = self.file_connections.get("vcenter", {})
        self.cfg.connections["vcenter"] = (
            {**raw, "instances": [dict(i) for i in raw["instances"]]}
            if isinstance(raw.get("instances"), list) else mirror_primary(normalize_vcenter(raw))
        )
        log.info("vCenter instances reset to config.yaml")

    def vcenter_list(self) -> list[dict[str, Any]]:
        return normalize_vcenter(self.cfg.connections.get("vcenter") or {})

    def set_interval(self, mod: Module, c, minutes: int) -> None:
        if minutes == c.interval_minutes:
            return
        c.interval_minutes = minutes
        self.storage.set_collector_interval(mod.name, c.name, minutes)
        job = self.scheduler.get_job(f"{mod.name}:{c.name}") if self.scheduler.running else None
        if job:
            job.reschedule("interval", minutes=minutes)
        log.info("%s/%s now runs every %d min", mod.name, c.name, minutes)

    # -- scheduled exports ------------------------------------------------------
    def schedule_export(self, sched: dict) -> None:
        self.scheduler.add_job(self.run_scheduled_export, xs.trigger(sched), args=[sched["id"]],
                               id=f"export:{sched['id']}", replace_existing=True, max_instances=1,
                               coalesce=True, misfire_grace_time=3600)

    def unschedule_export(self, sid: int) -> None:
        if self.scheduler.get_job(f"export:{sid}"):
            self.scheduler.remove_job(f"export:{sid}")

    def run_scheduled_export(self, sid: int) -> dict:
        import json
        sched = next((x for x in self.storage.export_schedules() if x["id"] == sid), None)
        if not sched:
            return {"status": "error", "message": "schedule not found"}
        mod = self.modules.get(sched["module"])
        exp = next((e for e in (mod.exports if mod else []) if e.name == sched["export"]), None)
        try:
            if not mod or not exp:
                raise RuntimeError(f"export {sched['module']}/{sched['export']} no longer exists")
            if not mod.available:
                raise RuntimeError(f"module {mod.name} is switched off")
            params = json.loads(sched["params"] or "{}")
            body = run_export(self.cfg, self.storage, mod, exp, params)
            folder = xs.resolve_folder(sched["folder"])
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / xs.filename_for(exp.filename, params, sched["timezone"])
            tmp = target.with_name(target.name + ".part")
            tmp.write_bytes(body)
            tmp.replace(target)          # never leave a half-written file under the real name
            removed = xs.prune(folder, exp.filename, params, int(sched["keep"] or 0))
            msg = f"wrote {target.name} ({len(body) // 1024} KB)" + (f", removed {len(removed)} old" if removed else "")
            self.storage.record_export_run(sid, "ok", msg, str(target))
            log.info("scheduled export %s: %s", sid, msg)
            return {"status": "ok", "message": msg}
        except Exception as exc:
            self.storage.record_export_run(sid, "error", str(exc), None)
            log.error("scheduled export %s failed: %s", sid, exc)
            return {"status": "error", "message": str(exc)}

    def set_enabled(self, mod: Module, enabled: bool) -> None:
        mod.enabled = enabled
        self.storage.set_module_state(mod.name, enabled)
        log.info("module %s %s", mod.name, "enabled" if enabled else "disabled")
        if enabled and mod.available:
            threading.Thread(target=self.run_module, args=[mod], daemon=True).start()


def create_app(cfg: Config | None = None, start_scheduler: bool = True) -> FastAPI:
    core = Core(cfg or load_config())
    if not core.cfg.api_tokens:
        log.warning("No API tokens configured: the /api endpoints are open. Set api.tokens in config.yaml.")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_scheduler:
            core.start()
        yield
        if core.scheduler.running:
            core.scheduler.shutdown(wait=False)

    # FastAPI's default Swagger UI loads from a CDN, which breaks at dark sites.
    # The built-in route is off; /docs below serves a vendored offline copy.
    app = FastAPI(
        title="vCommunity-Observability", version=CORE_VERSION,
        docs_url=None, redoc_url=None, lifespan=lifespan,
    )
    app.state.core = core
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals["asset_v"] = asset_version(HERE / "static")
    # Sidebar link to the vCommunity Ask chat container (empty = no link).
    templates.env.globals["ask_url"] = os.environ.get("VCO_ASK_URL", "")
    # Module templates are addressed as "<module>/<file>" and can extend base.html.
    from jinja2 import ChoiceLoader, FileSystemLoader, PrefixLoader
    templates.env.loader = ChoiceLoader([
        FileSystemLoader(HERE / "templates"),
        PrefixLoader({m.name: FileSystemLoader(m.path / "templates") for m in core.modules.values()}),
    ])

    @app.get("/docs", include_in_schema=False)
    def swagger_ui():
        return get_swagger_ui_html(
            openapi_url="/openapi.json", title="vCommunity-Observability API",
            swagger_js_url="/static/vendor/swagger-ui/swagger-ui-bundle.js",
            swagger_css_url="/static/vendor/swagger-ui/swagger-ui.css",
            swagger_favicon_url="/static/vendor/swagger-ui/favicon-32x32.png",
            swagger_ui_parameters={"persistAuthorization": True},
        )

    # -- API auth -----------------------------------------------------------
    # Declared as a security scheme so the Swagger UI "Authorize" button works.
    api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

    def require_token(request: Request, api_key: str | None = Security(api_key_header)) -> None:
        if not core.cfg.api_tokens:
            return
        supplied = api_key or ""
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            supplied = auth[7:].strip()
        if not any(secrets.compare_digest(supplied, t) for t in core.cfg.api_tokens):
            raise HTTPException(status_code=401, detail="Missing or invalid API token")

    # -- core API -------------------------------------------------------------
    @app.get("/api/core/health", tags=["core"])
    def health() -> dict[str, Any]:
        return {"status": "ok", "version": CORE_VERSION, "core_api": CORE_API, "time": utcnow()}

    @app.get("/api/core/modules", tags=["core"], dependencies=[Depends(require_token)])
    def list_modules() -> dict[str, Any]:
        runs = core.storage.last_runs()
        items = []
        for m in core.modules.values():
            items.append({
                "name": m.name, "version": m.version, "enabled": m.enabled,
                "available": m.available, "problems": m.problems, "languages": m.languages,
                "endpoints": [f"/api/{m.name}{r['path']}" for r in m.api],
                "collectors": [
                    {"name": c.name, "language": c.language, "interval_minutes": c.interval_minutes,
                     "last_run": runs.get((m.name, c.name))}
                    for c in m.collectors
                ],
            })
        return {"count": len(items), "items": items}

    @app.put("/api/core/modules/{module}/enabled", tags=["core"], dependencies=[Depends(require_token)])
    def set_module_enabled(module: str, enabled: bool) -> dict[str, Any]:
        """Turn a module on or off. Remembered across restarts."""
        mod = core.modules.get(module)
        if not mod:
            raise HTTPException(404, "Unknown module")
        core.set_enabled(mod, enabled)
        return {"name": mod.name, "enabled": mod.enabled, "available": mod.available}

    @app.put("/api/core/modules/order", tags=["core"], dependencies=[Depends(require_token)])
    def set_module_order(names: list[str]) -> dict[str, Any]:
        """Set the menu order. Body: JSON list of module names, first = top.
        Modules not listed keep their place after the listed ones."""
        unknown = [n for n in names if n not in core.modules]
        if unknown:
            raise HTTPException(400, f"Unknown module(s): {', '.join(unknown)}")
        core.set_order(names)
        return {"order": list(core.modules)}

    # -- module API routes, declared in each module.yaml ------------------------
    def make_endpoint(mod: Module, route: dict[str, Any]):
        sql = route.get("query") or f"SELECT * FROM {route['table']}"

        def endpoint() -> dict[str, Any]:
            if not mod.available:
                raise HTTPException(404, f"Module {mod.name} is disabled")
            items = core.storage.query(mod.name, sql)
            stamps = [i["collected_at"] for i in items if i.get("collected_at")]
            return {
                "module": mod.name, "endpoint": route["path"],
                "collected_at": max(stamps) if stamps else None,
                "count": len(items), "items": items,
            }
        return endpoint

    for mod in core.modules.values():
        if mod.problems:
            continue
        for route in mod.api:
            app.add_api_route(
                f"/api/{mod.name}{route['path']}", make_endpoint(mod, route), methods=["GET"],
                tags=[mod.name], summary=route.get("description", route["path"]),
                dependencies=[Depends(require_token)],
                name=f"{mod.name}:{route['path']}",
            )

    # -- exports: files a module produces on demand (XLSX, CSV, reports) ----------
    def export_response(module: str, name: str, request: Request) -> Response:
        mod = core.modules.get(module)
        exp = next((e for e in (mod.exports if mod else []) if e.name == name), None)
        if not mod or not mod.available or not exp:
            raise HTTPException(404, "Export not found")
        params = {k: v for k, v in request.query_params.items() if k in exp.params}
        try:
            body = run_export(core.cfg, core.storage, mod, exp, params)
        except Exception as exc:
            log.error("%s export %s failed: %s", mod.name, exp.name, exc)
            raise HTTPException(500, f"Export failed: {exc}") from None
        fields = {"timestamp": utcnow().replace("-", "").replace(":", "").replace(" ", "-")}
        fields.update({k: re.sub(r"[^A-Za-z0-9._-]+", "_", v)[:60] for k, v in params.items()})
        try:
            filename = exp.filename.format(**fields)
        except (KeyError, IndexError, ValueError):
            filename = f"{mod.name}-{exp.name}"
        return Response(body, media_type=exp.media_type,
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    @app.get("/export/{module}/{name}", include_in_schema=False)
    def export_ui(module: str, name: str, request: Request):
        return export_response(module, name, request)

    for mod in core.modules.values():
        for exp in mod.exports:
            def make(m=mod, e=exp):
                def endpoint(request: Request) -> Response:
                    return export_response(m.name, e.name, request)
                return endpoint
            app.add_api_route(
                f"/api/{mod.name}/export/{exp.name}", make(), methods=["GET"], tags=[mod.name],
                summary=exp.title, dependencies=[Depends(require_token)],
                response_class=Response, name=f"{mod.name}:export:{exp.name}",
            )

    # -- UI ---------------------------------------------------------------------
    def nav() -> list[dict[str, Any]]:
        return [
            {"module": m.name, "slug": d["slug"], "title": d.get("title", d["slug"])}
            for m in core.modules.values() if m.available for d in m.dashboards
        ]

    def render_widget(mod: Module, w: dict[str, Any]) -> dict[str, Any]:
        out = {**w, "rows": [], "error": None}
        try:
            out["rows"] = core.storage.query(mod.name, w["query"])
        except Exception as exc:  # show the problem on the tile, keep the page up
            out["error"] = str(exc)
            return out
        warn, crit = w.get("warn"), w.get("crit")

        def level(v: Any) -> str:
            if not isinstance(v, (int, float)) or warn is None:
                return ""
            if w.get("lower_is_worse"):
                return "crit" if crit is not None and v <= crit else "warn" if v <= warn else "ok"
            return "crit" if crit is not None and v >= crit else "warn" if v >= warn else "ok"

        if w["type"] == "kpi":
            v = next(iter(out["rows"][0].values())) if out["rows"] else None
            out["value"], out["level"] = v, level(v)
            if w.get("max") and isinstance(v, (int, float)):  # render as a ring gauge
                out["gauge"] = max(0.0, min(100.0, v / w["max"] * 100))
        elif w["type"] == "bar":
            values = [r.get("value") or 0 for r in out["rows"]]
            top = w.get("max") or (max(values) if values else 1) or 1
            out["bars"] = [
                {"label": r.get("label"), "value": r.get("value"),
                 "pct": max(0, min(100, (r.get("value") or 0) / top * 100)),
                 "level": level(r.get("value"))}
                for r in out["rows"]
            ]
        elif w["type"] == "table":
            out["columns"] = list(out["rows"][0].keys()) if out["rows"] else []
            hl = w.get("highlight") or {}
            out["hl_col"] = hl.get("column")
            if hl:
                warn, crit = hl.get("warn"), hl.get("crit")
                w = {**w, "lower_is_worse": hl.get("lower_is_worse")}
                out["levels"] = [level(r.get(hl["column"])) for r in out["rows"]]
        return out

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def home(request: Request):
        return templates.TemplateResponse(request, "home.html", {
            "nav": nav(), "modules": list(core.modules.values()),
            "runs": core.storage.last_runs(), "mock": core.cfg.mock, "active": "/",
        })

    @app.get("/dash/{module}/{slug}", response_class=HTMLResponse, include_in_schema=False)
    def dashboard(request: Request, module: str, slug: str):
        mod = core.modules.get(module)
        dash = next((d for d in (mod.dashboards if mod else []) if d["slug"] == slug), None)
        if not mod or not mod.available or not dash:
            raise HTTPException(404, "Dashboard not found")
        runs = core.storage.last_runs()
        if dash.get("template"):
            errors: list[str] = []
            data = LazyQueries(core.storage, mod.name, dash.get("queries") or {}, errors)
            mod_runs = [runs[(mod.name, c.name)] for c in mod.collectors if (mod.name, c.name) in runs]
            return templates.TemplateResponse(request, f"{mod.name}/{dash['template']}", {
                "nav": nav(), "mod": mod, "dash": dash, "data": data, "query_errors": errors,
                "q": dict(request.query_params), "mock": core.cfg.mock,
                "active": f"/dash/{mod.name}/{slug}",
                "last_collected": max((r["finished_at"] for r in mod_runs if r["status"] == "ok"), default=None),
                "run_errors": [r for r in mod_runs if r["status"] != "ok"],
            })
        widgets = [render_widget(mod, w) for w in dash.get("widgets", [])]
        mod_runs = [runs[(mod.name, c.name)] for c in mod.collectors if (mod.name, c.name) in runs]
        return templates.TemplateResponse(request, "dashboard.html", {
            "nav": nav(), "mod": mod, "dash": dash, "widgets": widgets, "mock": core.cfg.mock,
            "active": f"/dash/{mod.name}/{slug}",
            "last_collected": max((r["finished_at"] for r in mod_runs if r["status"] == "ok"), default=None),
            "run_errors": [r for r in mod_runs if r["status"] != "ok"],
        })

    # -- Settings: connections and collection intervals -------------------------
    async def read_form(request: Request) -> dict[str, str]:
        # Plain url-encoded forms, parsed here so python-multipart isn't needed.
        from urllib.parse import parse_qs
        body = (await request.body()).decode("utf-8", "replace")
        return {k: v[-1] for k, v in parse_qs(body, keep_blank_values=True).items()}

    def admin_ok(token: str) -> bool:
        # The UI has no login yet, so changes need an API token when tokens exist.
        return not core.cfg.api_tokens or any(secrets.compare_digest(token or "", t) for t in core.cfg.api_tokens)

    def denied(where: str = "") -> RedirectResponse:
        return settings_redirect("Not saved: enter a valid API token (the one in .env) to change settings.",
                                 False, f"#{where}" if where else "")

    def settings_redirect(msg: str, ok: bool = True, anchor: str = "", verified: bool = False) -> RedirectResponse:
        from urllib.parse import urlencode
        # The message is shown again inside the card it belongs to, next to the form.
        q = urlencode({"msg": msg, "ok": int(ok), "where": anchor.lstrip("#"), **({"verified": 1} if verified else {})})
        return RedirectResponse(f"/settings?{q}{anchor}", status_code=303)

    def export_form_defaults() -> dict[str, Any]:
        """The scheduled-export form shows the saved schedule, or sensible defaults."""
        current = next(iter(core.storage.export_schedules()), None)
        if current:
            import json
            params = json.loads(current["params"] or "{}")
            return {**current, "export": f"{current['module']}:{current['export']}",
                    **{f"param_{k}": v for k, v in params.items()}, "keep": str(current["keep"] or 0)}
        return {"export": "dbh-insights:xlsx", "folder": "dbh-insights", "frequency": "weekly", "weekday": "mon",
                "monthday": "1", "at_time": "06:00", "timezone": xs.default_timezone(), "keep": "12"}

    def schedule_view(x: dict[str, Any]) -> dict[str, Any]:
        import json
        mod = core.modules.get(x["module"])
        exp = next((e for e in (mod.exports if mod else []) if e.name == x["export"]), None)
        params = json.loads(x["params"] or "{}")
        return {**x, "when": xs.describe(x), "next": xs.next_run(x) if x["enabled"] else None,
                "folder_path": str(xs.resolve_folder(x["folder"])), "export_title": exp.title if exp else x["export"],
                "param_text": ", ".join(f"{k} {v}" for k, v in params.items())}

    def render_settings(request: Request, msg: str | None, ok: bool, where: str,
                        export_form: dict[str, Any] | None = None, verified: bool = False,
                        edit_vcenter: str | None = None):
        saved = core.storage.connection_settings()
        conns = []
        for name, conn in core.cfg.connections.items():
            if name == "vcenter":
                continue
            fields = fields_for(core.file_connections.get(name, conn))
            conns.append({
                "name": name, "values": conn, "fields": fields, "labels": LABELS,
                "from_ui": sorted(saved.get(name, {})), "password_set": bool(conn.get("password")),
                "verify": as_bool(conn.get("verify_ssl", False)),
                "used_by": [m.name for m in core.modules.values() if any(name in c.connections for c in m.collectors)],
            })
        vcenters = []
        for inst in core.vcenter_list():
            vcenters.append({
                **inst,
                "password_set": bool(inst.get("password")),
                "verify": as_bool(inst.get("verify_ssl", False)),
                "enabled": bool(inst.get("enabled", True)),
            })
        vc_used = [m.name for m in core.modules.values()
                   if any("vcenter" in c.connections for c in m.collectors)]
        editing = None
        if edit_vcenter == "__new__":
            editing = {"id": "", "label": "", "host": "", "user": "", "verify": False,
                       "enabled": True, "password_set": False, "is_new": True}
        elif edit_vcenter:
            editing = next((dict(v, is_new=False) for v in vcenters if v["id"] == edit_vcenter), None)
        return templates.TemplateResponse(request, "settings.html", {
            "nav": nav(), "connections": conns, "vcenters": vcenters, "vcenter_used_by": vc_used,
            "vcenters_from_ui": bool(core.storage.vcenter_instances()) or bool(saved.get("vcenter")),
            "edit_vcenter": editing, "modules": list(core.modules.values()), "mock": core.cfg.mock,
            "auth": bool(core.cfg.api_tokens), "msg": msg, "ok": ok, "where": where, "active": "/settings",
            "xf": export_form or export_form_defaults(), "verified": verified,
            "intervals_from_ui": core.storage.collector_intervals(),
            "schedules": [schedule_view(x) for x in core.storage.export_schedules()],
            "editing_export": export_form is not None or request.query_params.get("edit") == "1",
            "export_choices": [(m, e) for m in core.modules.values() for e in m.exports],
            "weekdays": xs.WEEKDAYS, "frequencies": xs.FREQUENCIES, "timezones": xs.timezone_names(),
            "default_tz": xs.default_timezone(), "exports_root": str(xs.exports_root()),
        })

    @app.get("/settings", response_class=HTMLResponse, include_in_schema=False)
    def settings_page(request: Request):
        return render_settings(
            request, request.query_params.get("msg"), request.query_params.get("ok") != "0",
            request.query_params.get("where", ""), verified=request.query_params.get("verified") == "1",
            edit_vcenter=request.query_params.get("edit_vcenter"),
        )

    @app.post("/settings/connections/{name}", include_in_schema=False)
    async def save_connection(name: str, request: Request):
        if name == "vcenter":
            # Legacy single-card save: update/create the primary (first) instance.
            form = await read_form(request)
            if not admin_ok(form.get("token", "")):
                return denied("vcenters")
            current = core.vcenter_list()
            primary = dict(current[0]) if current else {"id": "default", "label": "vCenter", "enabled": True}
            primary["host"] = str(form.get("host", "")).strip()
            primary["user"] = str(form.get("user", "")).strip()
            primary["verify_ssl"] = "true" if form.get("verify_ssl") else "false"
            if form.get("password"):
                primary["password"] = str(form["password"])
            if not primary.get("label"):
                primary["label"] = primary["host"] or primary["id"]
            # Prefer the multi-instance table so a later Add works cleanly.
            others = [dict(i) for i in current[1:]]
            core.apply_vcenters([primary, *others])
            if form.get("action") == "test":
                ok, message = test_connection("vcenter", primary, core.cfg.mock)
                return settings_redirect(f"vcenter: saved. Test: {message}", ok, "#vcenters")
            return settings_redirect("vcenter: saved. Collectors use it from their next run.", True, "#vcenters")
        if name not in core.cfg.connections:
            raise HTTPException(404, "Unknown connection")
        form = await read_form(request)
        if not admin_ok(form.get("token", "")):
            return denied(name)
        values: dict[str, str] = {}
        for field in fields_for(core.file_connections.get(name, core.cfg.connections[name])):
            if field == "verify_ssl":
                values[field] = "true" if form.get("verify_ssl") else "false"
            elif field == "password":
                if form.get("password"):          # blank keeps the current password
                    values[field] = str(form["password"])
            elif field in form:
                values[field] = str(form[field]).strip()
        core.set_connection(name, values)
        if form.get("action") == "test":
            ok, message = test_connection(name, core.cfg.connections[name], core.cfg.mock)
            return settings_redirect(f"{name}: saved. Test: {message}", ok, f"#{name}")
        return settings_redirect(f"{name}: saved. Collectors use it from their next run.", True, f"#{name}")

    @app.post("/settings/connections/{name}/reset", include_in_schema=False)
    async def reset_connection(name: str, request: Request):
        if name == "vcenter":
            form = await read_form(request)
            if not admin_ok(form.get("token", "")):
                return denied("vcenters")
            core.reset_vcenters()
            return settings_redirect("vCenter: back to the values in config.yaml and .env.", True, "#vcenters")
        if name not in core.cfg.connections:
            raise HTTPException(404, "Unknown connection")
        form = await read_form(request)
        if not admin_ok(form.get("token", "")):
            return denied(name)
        core.reset_connection(name)
        return settings_redirect(f"{name}: back to the values in config.yaml and .env.", True, f"#{name}")

    @app.post("/settings/vcenters", include_in_schema=False)
    async def save_vcenter(request: Request):
        form = await read_form(request)
        if not admin_ok(form.get("token", "")):
            return denied("vcenters")
        is_new = form.get("is_new") == "1"
        vid = str(form.get("id", "")).strip()
        if not valid_id(vid):
            return settings_redirect(
                "vCenter id must start with a letter and use only letters, digits, _ or - (max 32).",
                False, "#vcenters",
            )
        existing_ids = {i["id"] for i in core.vcenter_list()}
        if is_new and vid in existing_ids:
            return settings_redirect(f"vCenter id '{vid}' already exists.", False, "#vcenters")
        if not is_new and vid not in existing_ids:
            return settings_redirect(f"Unknown vCenter id '{vid}'.", False, "#vcenters")
        inst = {
            "id": vid,
            "label": str(form.get("label", "")).strip() or str(form.get("host", "")).strip() or vid,
            "host": str(form.get("host", "")).strip(),
            "user": str(form.get("user", "")).strip(),
            "verify_ssl": "true" if form.get("verify_ssl") else "false",
            "enabled": form.get("enabled") != "0",
        }
        if form.get("password"):
            inst["password"] = str(form["password"])
        elif is_new:
            inst["password"] = ""
        # First write into the UI table: seed from current list so file instances are preserved.
        if not core.storage.vcenter_instances():
            seed = [dict(i) for i in core.vcenter_list() if i["id"] != vid]
            if seed:
                core.storage.set_vcenter_instances(seed)
        core.upsert_vcenter(inst)
        if form.get("action") == "test":
            saved = next(i for i in core.vcenter_list() if i["id"] == vid)
            ok, message = test_connection("vcenter", saved, core.cfg.mock)
            return settings_redirect(f"{vid}: saved. Test: {message}", ok, "#vcenters")
        return settings_redirect(f"{vid}: saved. Collectors use it from their next run.", True, "#vcenters")

    @app.post("/settings/vcenters/{vid}/delete", include_in_schema=False)
    async def delete_vcenter(vid: str, request: Request):
        form = await read_form(request)
        if not admin_ok(form.get("token", "")):
            return denied("vcenters")
        if len(core.vcenter_list()) <= 1:
            return settings_redirect("Keep at least one vCenter connection.", False, "#vcenters")
        if vid not in {i["id"] for i in core.vcenter_list()}:
            return settings_redirect(f"Unknown vCenter id '{vid}'.", False, "#vcenters")
        if not core.storage.vcenter_instances():
            seed = [dict(i) for i in core.vcenter_list() if i["id"] != vid]
            core.apply_vcenters(seed)
        else:
            core.delete_vcenter(vid)
        return settings_redirect(f"{vid}: removed.", True, "#vcenters")

    @app.post("/settings/vcenters/reset", include_in_schema=False)
    async def reset_vcenters(request: Request):
        form = await read_form(request)
        if not admin_ok(form.get("token", "")):
            return denied("vcenters")
        core.reset_vcenters()
        return settings_redirect("vCenter: back to the values in config.yaml and .env.", True, "#vcenters")

    @app.post("/settings/intervals", include_in_schema=False)
    async def save_intervals(request: Request):
        form = await read_form(request)
        if not admin_ok(form.get("token", "")):
            return denied("intervals")
        changed = 0
        for mod in core.modules.values():
            for c in mod.collectors:
                raw = form.get(f"{mod.name}:{c.name}")
                if raw is None or str(raw).strip() == "":
                    continue
                try:
                    minutes = int(str(raw))
                except ValueError:
                    return settings_redirect(f"{mod.name}/{c.name}: '{raw}' is not a whole number of minutes", False, "#intervals")
                if not 1 <= minutes <= 10080:
                    return settings_redirect(f"{mod.name}/{c.name}: use 1 to 10080 minutes (one week)", False, "#intervals")
                if minutes != c.interval_minutes:
                    core.set_interval(mod, c, minutes)
                    changed += 1
        return settings_redirect(f"Collection intervals saved ({changed} changed).", True, "#intervals")

    @app.post("/settings/exports", include_in_schema=False)
    async def save_export_schedule(request: Request):
        """Create the schedule, or update it if one exists. On a problem the page is
        shown again with what was typed, so nothing is lost."""
        import json
        form = await read_form(request)

        def again(message: str):
            return render_settings(request, message, False, "exports", {**form})
        if not admin_ok(form.get("token", "")):
            return again("Not saved: enter a valid API token (the one in .env).")
        module, _, name = form.get("export", "").partition(":")
        mod = core.modules.get(module)
        exp = next((e for e in (mod.exports if mod else []) if e.name == name), None)
        if not exp:
            return again("Not saved: pick an export.")
        params = {p: form.get(f"param_{p}", "").strip() for p in exp.params}
        missing = [p for p, v in params.items() if not v]
        if missing:
            return again(f"Not saved: {exp.title} needs {', '.join(missing)}.")
        values = {"module": module, "export": name, "params": json.dumps(params),
                  "folder": form.get("folder", "").strip(), "frequency": form.get("frequency", ""),
                  "weekday": form.get("weekday", ""), "monthday": form.get("monthday", ""),
                  "at_time": form.get("at_time", "").strip(), "timezone": form.get("timezone", "UTC"),
                  "keep": form.get("keep", "0").strip() or "0"}
        error = xs.validate(values)
        if error:
            return again(f"Not saved: {error}")
        values["keep"] = int(values["keep"])
        current = next(iter(core.storage.export_schedules()), None)
        if current:
            core.storage.update_export_schedule(current["id"], values)
            sid = current["id"]
        else:
            sid = core.storage.add_export_schedule(values)
        sched = next((x for x in core.storage.export_schedules() if x["id"] == sid), None)
        if sched and sched["enabled"] and core.scheduler.running:
            core.schedule_export(sched)
        problem = verify_export_schedule(sid, values)
        if problem:
            return again(f"Not saved: {problem}")
        return settings_redirect(f"{exp.title}: {xs.describe(sched)}.", True, "#exports", verified=True)

    def verify_export_schedule(sid: int, wanted: dict[str, Any]) -> str | None:
        """Read the schedule back from the database and check the scheduler has it.
        Returns what's wrong, or None when everything matches what was submitted."""
        stored = next((x for x in core.storage.export_schedules() if x["id"] == sid), None)
        if not stored:
            return "the schedule wasn't found in the database after saving."
        if len(core.storage.export_schedules()) != 1:
            return "more than one schedule is stored; expected exactly one."
        for key, value in wanted.items():
            if str(stored.get(key) if stored.get(key) is not None else "") != str(value):
                return f"the database has a different {key.replace('_', ' ')} ({stored.get(key)!r}) than was entered ({value!r})."
        if stored["enabled"] and core.scheduler.running and not core.scheduler.get_job(f"export:{sid}"):
            return "it's in the database but the scheduler didn't pick it up. Check the logs."
        return None

    @app.post("/settings/exports/{sid}/{action}", include_in_schema=False)
    async def export_schedule_action(sid: int, action: str, request: Request):
        form = await read_form(request)
        if not admin_ok(form.get("token", "")):
            return denied("exports")
        sched = next((x for x in core.storage.export_schedules() if x["id"] == sid), None)
        if not sched:
            raise HTTPException(404, "Unknown schedule")
        if action == "run":
            import asyncio
            result = await asyncio.to_thread(core.run_scheduled_export, sid)
            return settings_redirect(f"Export run: {result['message']}", result["status"] == "ok", "#exports")
        if action == "delete":
            core.unschedule_export(sid)
            core.storage.delete_export_schedule(sid)
            return settings_redirect("Schedule removed. Files already written were kept.", True, "#exports")
        if action == "toggle":
            enabled = not sched["enabled"]
            core.storage.set_export_schedule_enabled(sid, enabled)
            if enabled and core.scheduler.running:
                core.schedule_export({**sched, "enabled": 1})
            elif not enabled:
                core.unschedule_export(sid)
            return settings_redirect(f"Schedule {'resumed' if enabled else 'paused'}.", True, "#exports")
        raise HTTPException(404, "Unknown action")

    # -- Folder picker for the scheduled export (folders under the exports root) --
    @app.get("/settings/folders", include_in_schema=False)
    def list_folders(path: str = ""):
        from fastapi.responses import JSONResponse
        try:
            return xs.browse(path)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    @app.post("/settings/folders", include_in_schema=False)
    async def new_folder(request: Request):
        from fastapi.responses import JSONResponse
        form = await read_form(request)
        if not admin_ok(form.get("token", "")):
            return JSONResponse({"error": "Enter a valid API token (the one in .env) to create folders."}, status_code=403)
        try:
            return xs.browse(xs.make_folder(form.get("path", ""), form.get("name", "")))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    # -- About: project summary and the README ------------------------------
    @app.get("/about", response_class=HTMLResponse, include_in_schema=False)
    def about(request: Request):
        import platform
        import shutil
        readme = find_readme()
        mods = list(core.modules.values())
        return templates.TemplateResponse(request, "about.html", {
            "nav": nav(), "mock": core.cfg.mock, "active": "/about",
            "readme": readme.read_text(encoding="utf-8") if readme else "",
            "version": CORE_VERSION, "core_api": CORE_API, "repo": REPO_URL,
            "modules_total": len(mods), "modules_ok": sum(1 for m in mods if m.available),
            "dashboards": len(nav()), "pwsh": bool(shutil.which("pwsh")),
            "python": platform.python_version(),
        })

    @app.get("/about/screenshots/{name}", include_in_schema=False)
    def about_screenshot(name: str):
        readme = find_readme()
        folder = readme.parent / "docs" / "screenshots" if readme else None
        if not folder or not re.fullmatch(r"[\w.-]+\.(png|jpe?g|gif|svg|webp)", name, re.I) or not (folder / name).is_file():
            raise HTTPException(404, "Not found")
        return FileResponse(folder / name)

    @app.get("/modules", response_class=HTMLResponse, include_in_schema=False)
    def modules_page(request: Request):
        return templates.TemplateResponse(request, "modules.html", {
            "nav": nav(), "modules": list(core.modules.values()),
            "runs": core.storage.last_runs(), "mock": core.cfg.mock,
            "auth": bool(core.cfg.api_tokens), "active": "/modules",
        })

    @app.post("/modules/{module}/run", include_in_schema=False)
    def run_now(module: str):
        mod = core.modules.get(module)
        if not mod or not mod.available:
            raise HTTPException(404, "Module not available")
        core.run_module(mod)
        return RedirectResponse("/modules", status_code=303)

    @app.post("/modules/{module}/move", include_in_schema=False)
    def move(module: str, dir: str = "up"):
        if module not in core.modules:
            raise HTTPException(404, "Unknown module")
        core.move(module, -1 if dir == "up" else 1)
        return RedirectResponse(f"/modules#{module}", status_code=303)

    @app.post("/modules/{module}/toggle", include_in_schema=False)
    def toggle(module: str):
        mod = core.modules.get(module)
        if not mod:
            raise HTTPException(404, "Unknown module")
        core.set_enabled(mod, not mod.enabled)
        return RedirectResponse(f"/modules#{mod.name}", status_code=303)

    return app
