"""Module web extensions (module.yaml `web: web.py`).

Most modules need no web code: their API routes and dashboards are declared in
module.yaml. A module that needs more (a search box with live results, forms that save
settings, a live tail) ships a Python file with a function

    def register(app: fastapi.FastAPI, ctx: ExtensionContext) -> None

that adds its own routes. Routes should live under /api/<module>/... (token protected
with ctx.require_token, shown in Swagger) and /ui/<module>/... (used by the module's own
pages: reads are open like the dashboards, changes call ctx.check_admin(request)).
Pages are module templates rendered with ctx.page(...), so they get the sidebar and theme.

Extensions are Python because the web app is FastAPI. A module's collectors and services
can still be PowerShell.
"""

from __future__ import annotations

import importlib.util
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from fastapi import HTTPException, Request

log = logging.getLogger("vco.extensions")


@dataclass
class ExtensionContext:
    module: Any                     # core.modules.Module
    core: Any                       # core.app.Core
    data_dir: Path
    templates: Any                  # Jinja2Templates (module templates as "<module>/<file>")
    nav: Callable[[], list]
    require_token: Callable         # FastAPI dependency for /api routes
    admin_ok: Callable[[str], bool]
    auth: bool                      # True when API tokens are configured

    def available(self) -> bool:
        return self.module.available

    def check_available(self) -> None:
        if not self.module.available:
            raise HTTPException(404, f"Module {self.module.name} is switched off")

    def check_admin(self, request: Request) -> None:
        """Changes from the UI: the API token (X-API-Key or Bearer) when tokens exist."""
        token = request.headers.get("x-api-key", "")
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
        if not self.admin_ok(token):
            raise HTTPException(401, "Enter the API token (the one in .env) to change this.")

    def page(self, request: Request, template: str, active: str, **extra: Any):
        return self.templates.TemplateResponse(request, f"{self.module.name}/{template}", {
            "nav": self.nav(), "mod": self.module, "mock": self.core.cfg.mock, "auth": self.auth,
            "active": active, **extra,
        })

    def add_perf(self, fn: Callable[[], dict]) -> None:
        """Extra numbers for the Performance dashboard (core GET /perf/container)."""
        self.core.perf_extras[self.module.name] = fn

    def service_status(self, name: str) -> dict | None:
        return self.core.services.status(self.module.name, name)


def load_extension(app, core, mod, *, templates, nav, require_token, admin_ok) -> None:
    try:
        spec = importlib.util.spec_from_file_location(f"vco_ext_{mod.name.replace('-', '_')}", mod.web)
        code = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(code)
        ctx = ExtensionContext(module=mod, core=core, data_dir=core.cfg.data_dir, templates=templates, nav=nav,
                               require_token=require_token, admin_ok=admin_ok, auth=bool(core.cfg.api_tokens))
        code.register(app, ctx)
        log.info("module %s: web extension %s loaded", mod.name, mod.web.name)
    except Exception as exc:  # a broken extension must not stop the app
        log.exception("module %s: web extension failed", mod.name)
        mod.problems.append(f"web extension {mod.web.name} failed to load: {exc}")
