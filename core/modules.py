"""Discover and validate add-on modules.

A module is a folder containing module.yaml. The manifest is language-neutral:
collectors are .py or .ps1 scripts, and API routes and dashboards are declared
as data so most modules need no web code at all.
"""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import CORE_API

log = logging.getLogger("vco.modules")

IDENT = re.compile(r"^[a-z][a-z0-9_]*$")
SLUG = re.compile(r"^[a-z][a-z0-9-]*$")
COLUMN_TYPES = {"text": "TEXT", "integer": "INTEGER", "real": "REAL"}
INTERPRETERS = {".py": "python", ".ps1": "powershell"}


@dataclass
class Collector:
    name: str
    script: Path
    language: str
    interval_minutes: int
    timeout_seconds: int
    connections: list[str]
    tables: list[str]


@dataclass
class Service:
    """A long-running script the core keeps alive while its module is on (a syslog
    listener, a message-queue consumer...). Unlike a collector it is not scheduled: the
    core starts it, restarts it when it exits, and stops it when the module is switched
    off or vCommunity Observability is paused. .py or .ps1, like collectors."""
    name: str
    script: Path
    language: str
    restart_seconds: int = 5


@dataclass
class Module:
    name: str
    version: str
    description: str
    path: Path
    manifest: dict[str, Any]
    collectors: list[Collector]
    tables: dict[str, dict[str, Any]]
    api: list[dict[str, Any]]
    dashboards: list[dict[str, Any]]
    exports: list["Export"] = field(default_factory=list)
    services: list[Service] = field(default_factory=list)
    # Optional Python file with register(app, ctx): extra pages and JSON routes for
    # modules that need more than read-only queries (search, forms). See core/extensions.py.
    web: Path | None = None
    enabled: bool = True
    settings: dict[str, Any] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    @property
    def available(self) -> bool:
        return self.enabled and not self.problems

    @property
    def languages(self) -> list[str]:
        return sorted({c.language for c in self.collectors} | {s.language for s in self.services})


@dataclass
class Export:
    """A file a module can produce on demand (XLSX, CSV, a report...).

    The script reads the module's SQLite file (VCO_DB, opened read-only) and
    writes the file to VCO_OUTPUT_FILE. URL query parameters arrive as JSON in
    VCO_PARAMS and can be used in the filename as {name}.
    """
    name: str
    title: str
    script: Path
    language: str
    filename: str
    media_type: str
    timeout_seconds: int
    params: list[str]


class ManifestError(ValueError):
    pass


def _check_sql(sql: str, where: str) -> None:
    head = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ""
    if head not in {"SELECT", "WITH"}:
        raise ManifestError(f"{where}: only SELECT/WITH queries are allowed")


def parse_manifest(folder: Path) -> Module:
    folder = folder.resolve()
    data = yaml.safe_load((folder / "module.yaml").read_text()) or {}
    name = data.get("name", "")
    if not SLUG.match(name):
        raise ManifestError(f"name '{name}' must be lowercase letters, digits and dashes")
    if data.get("core_api") != CORE_API:
        raise ManifestError(
            f"written for core_api {data.get('core_api')}, this core provides {CORE_API}"
        )

    tables: dict[str, dict[str, Any]] = {}
    for tname, tdef in (data.get("tables") or {}).items():
        if not IDENT.match(tname):
            raise ManifestError(f"table '{tname}' is not a valid identifier")
        cols = tdef.get("columns") or {}
        for cname, ctype in cols.items():
            if not IDENT.match(cname) or ctype not in COLUMN_TYPES:
                raise ManifestError(f"table {tname}: bad column {cname}: {ctype}")
        mode = tdef.get("mode", "snapshot")
        if mode not in {"snapshot", "append"}:
            raise ManifestError(f"table {tname}: mode must be snapshot or append")
        tables[tname] = {**tdef, "mode": mode, "columns": cols}

    collectors: list[Collector] = []
    for c in data.get("collectors") or []:
        script = folder / c["script"]
        lang = INTERPRETERS.get(script.suffix.lower())
        if lang is None:
            raise ManifestError(f"collector {c['name']}: script must be .py or .ps1")
        if not script.exists():
            raise ManifestError(f"collector {c['name']}: {c['script']} not found")
        for t in c.get("tables", []):
            if t not in tables:
                raise ManifestError(f"collector {c['name']}: unknown table {t}")
        collectors.append(
            Collector(
                name=c["name"],
                script=script,
                language=lang,
                interval_minutes=int(c.get("interval_minutes", 15)),
                timeout_seconds=int(c.get("timeout_seconds", 300)),
                connections=list(c.get("connections", [])),
                tables=list(c.get("tables", [])),
            )
        )

    api = data.get("api") or []
    for route in api:
        if not str(route.get("path", "")).startswith("/"):
            raise ManifestError(f"api path {route.get('path')} must start with /")
        if "query" in route:
            _check_sql(route["query"], f"api {route['path']}")
        elif route.get("table") not in tables:
            raise ManifestError(f"api {route['path']}: needs a known table or a query")

    dashboards = data.get("dashboards") or []
    for dash in dashboards:
        if not SLUG.match(dash.get("slug", "")):
            raise ManifestError(f"dashboard slug '{dash.get('slug')}' is invalid")
        if dash.get("template"):
            # Custom page: a Jinja template shipped in the module's templates/
            # folder, fed by named read-only queries. For layouts the widget
            # grid can't express (trees, object pages, tabs).
            if not (folder / "templates" / dash["template"]).is_file():
                raise ManifestError(f"dashboard {dash['slug']}: templates/{dash['template']} not found")
            for qname, sql in (dash.get("queries") or {}).items():
                if not IDENT.match(qname):
                    raise ManifestError(f"dashboard {dash['slug']}: bad query name {qname}")
                _check_sql(sql, f"dashboard {dash['slug']} query {qname}")
            continue
        for w in dash.get("widgets", []):
            if w.get("type") not in {"kpi", "bar", "table"}:
                raise ManifestError(f"dashboard {dash['slug']}: unknown widget {w.get('type')}")
            _check_sql(w.get("query", ""), f"dashboard {dash['slug']}")

    exports: list[Export] = []
    for e in data.get("exports") or []:
        if not IDENT.match(str(e.get("name", ""))):
            raise ManifestError(f"export '{e.get('name')}' must be a lowercase identifier")
        script = folder / str(e.get("script", ""))
        lang = INTERPRETERS.get(script.suffix.lower())
        if lang is None or not script.is_file():
            raise ManifestError(f"export {e['name']}: script must be an existing .py or .ps1 file")
        exports.append(Export(
            name=e["name"], title=e.get("title", e["name"]), script=script, language=lang,
            filename=str(e.get("filename", f"{name}-{e['name']}")),
            media_type=str(e.get("media_type", "application/octet-stream")),
            timeout_seconds=int(e.get("timeout_seconds", 120)),
            params=[str(p) for p in e.get("params", [])],
        ))

    services: list[Service] = []
    for sv in data.get("services") or []:
        if not IDENT.match(str(sv.get("name", ""))):
            raise ManifestError(f"service '{sv.get('name')}' must be a lowercase identifier")
        script = folder / str(sv.get("script", ""))
        lang = INTERPRETERS.get(script.suffix.lower())
        if lang is None or not script.is_file():
            raise ManifestError(f"service {sv['name']}: script must be an existing .py or .ps1 file")
        services.append(Service(name=sv["name"], script=script, language=lang,
                                restart_seconds=max(1, int(sv.get("restart_seconds", 5)))))

    web = None
    if data.get("web"):
        web = folder / str(data["web"])
        if web.suffix != ".py" or not web.is_file():
            raise ManifestError(f"web: {data['web']} must be an existing .py file")

    return Module(
        name=name,
        version=str(data.get("version", "0.0.0")),
        description=data.get("description", ""),
        path=folder,
        manifest=data,
        collectors=collectors,
        tables=tables,
        api=api,
        dashboards=dashboards,
        exports=exports,
        services=services,
        web=web,
        enabled=bool(data.get("enabled_by_default", True)),
    )


def discover(dirs: list[Path], overrides: dict[str, dict[str, Any]]) -> dict[str, Module]:
    found: dict[str, Module] = {}
    for base in dirs:
        if not base.is_dir():
            continue
        for manifest in sorted(base.glob("*/module.yaml")):
            try:
                mod = parse_manifest(manifest.parent)
            except Exception as exc:  # a broken module must not stop the others
                log.error("skipping module in %s: %s", manifest.parent, exc)
                continue
            if mod.name in found:
                # Later folders (e.g. the mounted /modules volume) override
                # built-in copies, which is how a patched module is dropped in.
                log.info("module %s overridden by %s", mod.name, manifest.parent)
            override = overrides.get(mod.name) or {}
            if "enabled" in override:
                mod.enabled = bool(override["enabled"])
            mod.settings = override.get("settings") or {}
            if "powershell" in mod.languages + [e.language for e in mod.exports] and not shutil.which("pwsh"):
                mod.problems.append("PowerShell (pwsh) is not installed; use the core-pwsh image")
            found[mod.name] = mod
    return found
