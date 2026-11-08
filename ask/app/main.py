"""vCommunity Ask: chat with your own knowledge, answered by your local Ollama.

Run locally:   uvicorn app.main:create_app --factory --port 8081   (from the ask/ folder)
In Docker:     see ask/Dockerfile and the vco-ask service in docker-compose.yml"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Body, Depends, FastAPI, HTTPException, Request, Security
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.security import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import __version__
from .config import SETTINGS, coerce, load_config
from .db import DB
from .indexer import NAME_RE, Indexer, discover_plugins
from .ollama import Ollama, OllamaError
from . import control, mcp, rag, websearch
from .reports import Reports
from .perf import SAMPLE_SECONDS, Sampler

HERE = Path(__file__).resolve().parent
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
    """The project README for the About page: /app/README.md in the image, the repo root otherwise."""
    for p in (os.environ.get("ASK_README"), HERE.parent / "README.md", HERE.parent.parent / "README.md"):
        if p and Path(p).is_file():
            return Path(p)
    return None
log = logging.getLogger("ask")
IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
# File types each upload-capable source plug-in accepts on the Knowledge sources page.
UPLOAD_TYPES_BY_PLUGIN = {"spreadsheet": {".xlsx", ".csv"}, "pdf": {".pdf"}}
UPLOAD_TYPES = set().union(*UPLOAD_TYPES_BY_PLUGIN.values())
UPLOAD_DEFAULT_MODE = {"spreadsheet": "replace", "pdf": "keep"}
UPLOAD_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()+-]{0,150}$")


def shared_dir() -> Path:
    """The first container's theme (app.css, Swagger UI). In the image it is copied to
    /app/shared; when run from the repo it is read from ../core/static."""
    for p in (os.environ.get("ASK_SHARED_DIR"), "/app/shared", HERE.parent.parent / "core" / "static"):
        if p and (Path(p) / "app.css").exists():
            return Path(p)
    raise RuntimeError("Theme not found: set ASK_SHARED_DIR to the folder holding app.css")


def interval_hours(value: Any) -> float:
    """Re-sync interval from a form or ask.yaml: empty = 24, 0 (or less) = never, else at least 0.25."""
    if value is None or str(value).strip() == "":
        return 24.0
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 24.0
    return 0.0 if v <= 0 else max(0.25, v)


def ago(ts: float | None) -> str:
    if not ts:
        return "never"
    s = int(time.time() - ts)
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60} min ago"
    if s < 86400:
        return f"{s // 3600} h ago"
    return f"{s // 86400} d ago"


def create_app() -> FastAPI:
    logging.basicConfig(level=os.environ.get("ASK_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    db = DB(cfg.data_dir / "ask.db")

    def settings() -> dict[str, Any]:
        merged = dict(cfg.defaults)
        for k, v in db.get_settings().items():
            if k in SETTINGS:
                try:
                    merged[k] = coerce(SETTINGS[k]["type"], v)
                except (TypeError, ValueError):
                    pass
        return merged

    def plugins():
        return discover_plugins(cfg.sources_dirs)

    indexer = Indexer(db, cfg.data_dir, plugins, settings)
    upload_max = int(float(os.environ.get("ASK_UPLOAD_MAX_MB") or 50) * 1024 * 1024)

    def uploaded_files(name: str) -> list[dict[str, Any]]:
        d = indexer.upload_dir(name)
        if not d.is_dir():
            return []
        return [{"name": p.name, "size_mb": round(p.stat().st_size / 1048576, 1), "uploaded": ago(p.stat().st_mtime)}
                for p in sorted(d.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
                if p.is_file() and p.suffix.lower() in UPLOAD_TYPES]

    # Seed sources from ask.yaml once; after that the Sources page owns them.
    for src in cfg.sources:
        if src.get("name") and not db.get_source(src["name"]):
            db.upsert_source({**src, "origin": "config"})

    scheduler = BackgroundScheduler(timezone="UTC")
    sampler = Sampler(db, settings)
    # The sampler runs every 10 s; don't log "Running job ..." for every run.
    logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)

    def due_syncs() -> None:
        if not control.is_on("ask"):
            return          # Settings → Apps on / off: Ask is paused
        now = time.time()
        for s in db.list_sources():
            if not s["enabled"] or indexer.is_running(s["name"]):
                continue
            last = s.get("last_end") or 0
            # 0 hours = never re-sync on a schedule (only the first sync and "Sync now").
            if s["status"] == "never" or (s["interval_hours"] > 0 and now - last > s["interval_hours"] * 3600):
                indexer.start(s["name"])

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Every 10 minutes: sync each enabled source whose interval has passed. The first
        # check runs 5 seconds after start, so a new install indexes without a click.
        scheduler.add_job(due_syncs, "interval", minutes=10, id="due-syncs",
                          next_run_time=datetime.now(timezone.utc) + timedelta(seconds=5))
        # Performance page: one sample of the container, Ollama and the host agent every 10 s.
        scheduler.add_job(sampler.sample, "interval", seconds=SAMPLE_SECONDS, id="perf-sample",
                          max_instances=1, coalesce=True, next_run_time=datetime.now(timezone.utc) + timedelta(seconds=2))
        scheduler.start()
        yield
        scheduler.shutdown(wait=False)

    app = FastAPI(title="vCommunity Ask API", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None,
                  description="Questions answered from your own knowledge sources by a local Ollama model.")
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.mount("/shared", StaticFiles(directory=shared_dir()), name="shared")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals["asset_v"] = asset_version(HERE / "static", shared_dir())
    reports = Reports(cfg.data_dir / "reports", templates.env, shared_dir(), HERE / "static")
    rag.REPORTS = reports
    templates.env.filters["ago"] = ago

    # -- auth -----------------------------------------------------------------
    api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

    def token_ok(supplied: str) -> bool:
        return not cfg.api_tokens or any(secrets.compare_digest(supplied or "", t) for t in cfg.api_tokens)

    def supplied_token(request: Request, api_key: str | None) -> str:
        auth = request.headers.get("authorization", "")
        return auth[7:].strip() if auth.lower().startswith("bearer ") else (api_key or "")

    def require_token(request: Request, api_key: str | None = Security(api_key_header)) -> None:
        if not token_ok(supplied_token(request, api_key)):
            raise HTTPException(status_code=401, detail="Missing or invalid API token")

    def same_origin(request: Request) -> None:
        """The UI endpoints have no login (like the first container), so at least refuse
        requests that another web site's page sends from the user's browser."""
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != request.headers.get("host"):
            raise HTTPException(status_code=403, detail="Cross-site request refused")

    def require_admin(request: Request, api_key: str | None = Security(api_key_header)) -> None:
        same_origin(request)
        if not token_ok(supplied_token(request, api_key)):
            raise HTTPException(status_code=401, detail="Enter the API token (ASK_API_TOKEN in .env) to change this.")

    # -- page helpers ---------------------------------------------------------
    def page(request: Request, name: str, active: str, **ctx: Any) -> HTMLResponse:
        s = settings()
        return templates.TemplateResponse(request, name, {
            "active": active, "chats": db.list_chats(20), "sources": db.list_sources(),
            "observability_url": s["observability_url"] if control.is_on("observability") else "",
            "ask_paused": not control.is_on("ask"), "version": __version__,
            "auth": bool(cfg.api_tokens), "settings": s, **ctx})

    def scope_list(scope: Any) -> list[str] | None:
        """Source names a question may use. "All sources" leaves spreadsheet sources out: they
        are used (search and data questions) only when picked by name."""
        srcs = db.list_sources()
        if isinstance(scope, str):          # "isvara" or "isvara,vcrocs" (saved chats, /api/search)
            scope = [n.strip() for n in scope.split(",") if n.strip()]
        if not scope or "all" in scope:
            # "-" matches no source, so with only spreadsheet sources "All" searches nothing.
            return [s["name"] for s in srcs if s["plugin"] != "spreadsheet"] or ["-"]
        names = [str(n) for n in scope]
        known = {s["name"] for s in srcs}
        return [n for n in names if n in known] or scope_list("all")

    def split_scope(scope: Any) -> tuple[list[str] | None, list[str]]:
        """The Sources menu value as (knowledge sources, MCP servers). "mcp:<server>" entries
        are MCP tools; with only those picked, no knowledge source is searched (an empty list)."""
        items = [n.strip() for n in scope.split(",")] if isinstance(scope, str) else [str(n) for n in (scope or [])]
        tools = [n[4:] for n in items if n.startswith("mcp:") and n[4:]]
        # "web:ollama" = Ollama web search (web_search / web_fetch tools), passed on with the MCP servers.
        if websearch.SCOPE in items:
            tools.append(websearch.SCOPE)
        rest = [n for n in items if n and not n.startswith(("mcp:", "web:"))]
        if tools and not rest:
            return [], tools
        return scope_list(rest or "all"), tools

    # -- pages ----------------------------------------------------------------
    @app.get("/", include_in_schema=False)
    def home():
        return RedirectResponse("/chat")

    @app.get("/chat", response_class=HTMLResponse, include_in_schema=False)
    def chat_new(request: Request):
        examples = [e for s in db.list_sources() if s["enabled"] for e in s["examples"]][:6]
        return page(request, "chat.html", "/chat", chat=None, messages=[], examples=examples, stats=db.stats(),
                    mcp_servers=mcp.quick_servers(settings()["agent_url"]), web_search=websearch.status(settings()))

    @app.get("/chat/{chat_id}", response_class=HTMLResponse, include_in_schema=False)
    def chat_page(request: Request, chat_id: str):
        chat = db.get_chat(chat_id)
        if not chat:
            return RedirectResponse("/chat")
        return page(request, "chat.html", f"/chat/{chat_id}", chat=chat, messages=db.messages(chat_id),
                    examples=[], stats=db.stats(), mcp_servers=mcp.quick_servers(settings()["agent_url"]),
                    web_search=websearch.status(settings()))

    @app.get("/sources", response_class=HTMLResponse, include_in_schema=False)
    def sources_page(request: Request):
        srcs = db.list_sources()
        for s in srcs:
            view = _source_view(s)
            s["status"], s["last_error"] = view["status"], view["last_error"]
            if s["plugin"] in UPLOAD_TYPES_BY_PLUGIN:
                s["uploads"] = uploaded_files(s["name"])
                s["upload_types"] = ",".join(sorted(UPLOAD_TYPES_BY_PLUGIN[s["plugin"]]))
                s["upload_mode"] = s["options"].get("uploads") or UPLOAD_DEFAULT_MODE[s["plugin"]]
            if s["plugin"] == "spreadsheet":
                st = db.get_state(s["name"])["state"]
                s["tables"] = [{"table": t["table"], "sheet": t["sheet"], "rows": t["rows"]} for t in st.get("tables") or []]
        pl = plugins()
        return page(request, "sources.html", "/sources", plugins=pl, stats=db.stats(), sources=srcs,
                    plugins_json={n: p.options for n, p in pl.items()})

    @app.get("/sources/{name}", response_class=HTMLResponse, include_in_schema=False)
    def source_docs(request: Request, name: str):
        src = db.get_source(name)
        if not src:
            raise HTTPException(404, "Unknown source")
        return page(request, "source_docs.html", "/sources", src=src, docs=db.list_documents(name))

    @app.get("/doc/{row}", response_class=HTMLResponse, include_in_schema=False)
    def doc_page(request: Request, row: int):
        d = db.get_document(row)
        if not d:
            raise HTTPException(404, "Document not found")
        # Point relative image links at /files/<source>/<path>.
        base = os.path.dirname(d["path"])

        def fix(m: re.Match[str]) -> str:
            src = m.group(2)
            if re.match(r"^[a-z]+://", src) or src.startswith("/"):
                return m.group(0)
            p = os.path.normpath(os.path.join(base, src)).replace(os.sep, "/")
            return f"![{m.group(1)}]({rag.file_url(d['source'], p)})"
        body = re.sub(r"!\[([^\]]*)\]\(([^)\s]+)\)", fix, d["text"])
        linked = {}
        for t, ids in (d["links"] or {}).items():
            found = {x["doc_id"]: x for x in db.find_documents(d["source"], ids)}
            linked[t] = [{"doc_id": i, "row": found[i]["id"] if i in found else None,
                          "title": found[i]["title"] if i in found else i} for i in ids]
        return page(request, "doc.html", "/sources", doc=d, body=body, linked=linked)

    # -- HTML reports (made by report and dashboard questions) --------------------
    @app.get("/reports", response_class=HTMLResponse, include_in_schema=False)
    def reports_page(request: Request):
        return page(request, "reports.html", "/reports", reports=reports.list())

    @app.get("/reports/{rid}.html", include_in_schema=False)
    def report_file(rid: str, download: bool = False):
        p = reports.path(rid)
        if not p:
            raise HTTPException(404, "Report not found")
        meta = reports.meta(rid) or {}
        slug = re.sub(r"[^a-z0-9]+", "-", str(meta.get("title", rid)).lower()).strip("-")[:60] or rid
        headers = {
            # Everything the report needs is inside the file: no network access at all.
            "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                                       "img-src data: blob:; font-src data:; base-uri 'none'; form-action 'none'",
            "X-Content-Type-Options": "nosniff",
        }
        if download:
            headers["Content-Disposition"] = f'attachment; filename="ask-report-{slug}.html"'
        return FileResponse(p, media_type="text/html; charset=utf-8", headers=headers)

    @app.get("/api/reports", tags=["reports"], dependencies=[Depends(require_token)])
    def api_reports():
        return {"reports": reports.list()}

    @app.post("/ui/reports/{rid}/delete", include_in_schema=False, dependencies=[Depends(require_admin)])
    def ui_report_delete(rid: str):
        if not reports.delete(rid):
            raise HTTPException(404, "Report not found")
        return {"ok": True}

    # -- MCP tools ---------------------------------------------------------------
    def mcp_payload(fresh: bool) -> dict[str, Any]:
        s = settings()
        info = mcp.servers(s["agent_url"], fresh=fresh)
        policy = mcp.load_policy(s["mcp_tools"])
        for srv in info["servers"]:
            for t in srv.get("tools") or []:
                t["policy"] = mcp.policy_of(policy, srv["name"], t)
                t["read_only"] = mcp.read_only(t)
        return {**info, "agent_url": s["agent_url"], "agent_token_set": bool(os.environ.get("ASK_AGENT_TOKEN")),
                "max_rounds": s["mcp_max_rounds"], "result_chars": s["mcp_result_chars"]}

    @app.get("/tools", response_class=HTMLResponse, include_in_schema=False)
    def tools_page(request: Request):
        return page(request, "tools.html", "/tools")

    @app.get("/ui/mcp", include_in_schema=False)
    def ui_mcp(fresh: bool = False):
        return mcp_payload(fresh)

    @app.get("/api/mcp", tags=["mcp"], dependencies=[Depends(require_token)])
    def api_mcp():
        """MCP servers offered by the host agent, their tools and each tool's policy
        (allow, ask, off). Pick a server for a question with `"sources": ["mcp:<server>"]`
        on POST /api/ask; tools set to "ask" are refused there (they need the chat page)."""
        return mcp_payload(True)

    @app.post("/ui/mcp/policy", include_in_schema=False, dependencies=[Depends(require_admin)])
    def ui_mcp_policy(body: dict = Body(...)):
        pol = {str(k): v for k, v in (body.get("policy") or {}).items() if v in mcp.POLICIES and "/" in str(k)}
        old = mcp.load_policy(settings()["mcp_tools"])
        old.update(pol)
        db.set_setting("mcp_tools", json.dumps(old, sort_keys=True))
        for key, lo, hi in (("mcp_max_rounds", 1, 20), ("mcp_result_chars", 1000, 200000)):
            if body.get(key) not in (None, ""):
                try:
                    v = int(body[key])
                except (TypeError, ValueError):
                    raise HTTPException(400, f"{key}: not a number")
                if not lo <= v <= hi:
                    raise HTTPException(400, f"{key}: use {lo} to {hi}")
                if v == cfg.defaults.get(key):
                    db.delete_setting(key)
                else:
                    db.set_setting(key, v)
        return {"ok": True}

    @app.post("/ui/mcp/{name}/reload", include_in_schema=False, dependencies=[Depends(require_admin)])
    def ui_mcp_reload(name: str):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name):
            raise HTTPException(400, "Bad server name")
        try:
            return {"ok": True, "server": mcp.reload(settings()["agent_url"], name)}
        except Exception as e:  # noqa: BLE001 - shown on the page
            raise HTTPException(502, f"Reload failed: {e}")

    @app.post("/ui/mcp/confirm", include_in_schema=False)
    def ui_mcp_confirm(request: Request, body: dict = Body(...)):
        """Run or Cancel for a tool set to "ask". The key is sent only to the chat that asked."""
        same_origin(request)
        if not mcp.APPROVALS.answer(str(body.get("key") or ""), bool(body.get("run"))):
            raise HTTPException(404, "This question is no longer waiting (answered, stopped or timed out).")
        return {"ok": True}

    # -- performance -----------------------------------------------------------
    def perf_payload(hours: float) -> dict[str, Any]:
        s = settings()
        return {"now": sampler.last or sampler.sample(), "history": sampler.history(hours),
                "answers": sampler.answers(max(hours, 24.0)), "num_ctx": s["num_ctx"], "chat_model": s["chat_model"],
                "agent_url": s.get("agent_url") or "", "agent_token_set": bool(os.environ.get("ASK_AGENT_TOKEN")),
                "sample_seconds": SAMPLE_SECONDS}

    @app.get("/performance", response_class=HTMLResponse, include_in_schema=False)
    def performance_page(request: Request):
        return page(request, "performance.html", "/performance")

    @app.get("/ui/perf", include_in_schema=False)
    def ui_perf(request: Request, hours: float = 1.0):
        same_origin(request)
        return perf_payload(hours)

    @app.get("/perf/container", include_in_schema=False)
    def perf_container():
        # Raw cgroup counters of this container for the dashboards' Performance page.
        # Numbers only (CPU time, memory, byte counts), so no token is needed.
        from .perf import container_raw
        raw = container_raw()
        try:
            raw["uptime"] = round(time.time() - Path("/proc/1").stat().st_mtime)
        except OSError:
            raw["uptime"] = None
        return {**raw, "app": "ask", "version": __version__}

    @app.get("/api/perf", tags=["performance"], dependencies=[Depends(require_token)])
    def api_perf(hours: float = 1.0):
        """Latest sample (container, Ollama, host agent), history (up to 24 h) and recent answer speeds."""
        return perf_payload(hours)

    @app.get("/about", response_class=HTMLResponse, include_in_schema=False)
    def about(request: Request):
        import platform
        readme = find_readme()
        return page(request, "about.html", "/about", repo=REPO_URL, counts=db.stats(),
                    readme=readme.read_text(encoding="utf-8") if readme else "",
                    python=platform.python_version())

    @app.get("/about/screenshots/{name}", include_in_schema=False)
    def about_screenshot(name: str):
        readme = find_readme()
        folder = readme.parent / "docs" / "screenshots" if readme else None
        if not folder or not re.fullmatch(r"[\w.-]+\.(png|jpe?g|gif|svg|webp)", name, re.I) or not (folder / name).is_file():
            raise HTTPException(404, "Not found")
        return FileResponse(folder / name)

    @app.get("/settings", response_class=HTMLResponse, include_in_schema=False)
    def settings_page(request: Request):
        overridden = set(db.get_settings())
        return page(request, "settings.html", "/settings", spec=SETTINGS, overridden=overridden,
                    defaults=cfg.defaults, config_path=str(cfg.path), web_key_set=bool(websearch.api_key()),
                    apps=control.status())

    @app.get("/docs", include_in_schema=False)
    def swagger():
        return get_swagger_ui_html(openapi_url="/openapi.json", title="vCommunity Ask API",
                                   swagger_js_url="/shared/vendor/swagger-ui/swagger-ui-bundle.js",
                                   swagger_css_url="/shared/vendor/swagger-ui/swagger-ui.css",
                                   swagger_favicon_url="/shared/vendor/swagger-ui/favicon-32x32.png",
                                   swagger_ui_parameters={"persistAuthorization": True})

    @app.get("/files/{source}/{path:path}", include_in_schema=False)
    def source_file(source: str, path: str):
        """Images from an indexed source (the diagrams the answers cite). Only image
        files inside that source's folder are served."""
        root = db.get_state(source).get("root")
        if not root:
            raise HTTPException(404)
        root_p = Path(root).resolve()
        f = (root_p / path).resolve()
        if root_p not in f.parents or not f.is_file():
            raise HTTPException(404)
        if f.suffix.lower() == ".pdf":
            # The PDFs of a pdf source, so a cited page opens in the browser (#page=N).
            src = db.get_source(source)
            if not src or src["plugin"] != "pdf":
                raise HTTPException(404)
            return FileResponse(f, media_type="application/pdf", content_disposition_type="inline",
                                headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "max-age=300"})
        if f.suffix.lower() not in IMAGE_TYPES:
            raise HTTPException(404)
        return FileResponse(f, headers={"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
                                        "Cache-Control": "max-age=3600"})

    # -- UI endpoints (used by the pages) --------------------------------------
    @app.post("/ui/chat", include_in_schema=False)
    def ui_chat(request: Request, body: dict = Body(...)):
        same_origin(request)
        paused()
        question = (body.get("question") or "").strip()
        if not question:
            raise HTTPException(400, "Empty question")
        chat_id = body.get("chat_id") or ""
        scope = body.get("scope") or "all"
        if not chat_id or not db.get_chat(chat_id):
            chat_id = db.new_chat(question, ",".join(map(str, scope)) if isinstance(scope, list) else str(scope))
        docs, tools = split_scope(scope)
        return StreamingResponse(rag.answer_stream(db, settings(), chat_id, question, docs, tools),
                                 media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/ui/chats/delete", include_in_schema=False)
    def ui_delete_chats(request: Request, body: dict = Body(...)):
        """Delete several chats at once (the sidebar's Select mode)."""
        same_origin(request)
        ids = [i for i in (body.get("ids") or []) if isinstance(i, str)][:500]
        for cid in ids:
            db.delete_chat(cid)
        return {"ok": True, "deleted": len(ids)}

    @app.post("/ui/chats/{chat_id}/delete", include_in_schema=False)
    def ui_delete_chat(request: Request, chat_id: str):
        same_origin(request)
        db.delete_chat(chat_id)
        return {"ok": True}

    @app.get("/ui/ollama", include_in_schema=False)
    def ui_ollama(url: str = ""):
        o = Ollama(url or settings()["ollama_url"])
        try:
            return {"ok": True, "url": o.url, "version": o.version(), "models": o.models()}
        except OllamaError as e:
            return {"ok": False, "url": o.url, "error": str(e), "models": []}

    @app.get("/ui/sources", include_in_schema=False)
    def ui_sources():
        return {"sources": [_source_view(s) for s in db.list_sources()], "stats": db.stats()}

    def _source_view(s: dict[str, Any]) -> dict[str, Any]:
        status, error = s["status"], s["last_error"]
        if indexer.is_running(s["name"]):
            status = "running"
        elif status == "running":  # the app stopped during a sync
            status, error = "error", error or "The last sync was interrupted (app restarted). Press Sync now."
        return {"name": s["name"], "status": status, "progress": s["progress"], "docs": s["docs"],
                "chunks": s["chunks"], "last_ok": ago(s["last_ok"]), "last_error": error,
                "version": s["version"], "enabled": s["enabled"]}

    @app.post("/ui/settings", include_in_schema=False, dependencies=[Depends(require_admin)])
    def ui_settings(body: dict = Body(...)):
        before = settings()
        errors = {}
        for key, value in (body.get("values") or {}).items():
            if key not in SETTINGS:
                continue
            try:
                v = coerce(SETTINGS[key]["type"], value)
            except (TypeError, ValueError):
                errors[key] = "not a valid value"
                continue
            if key in ("top_k", "history_turns") and not (0 <= v <= 50):
                errors[key] = "use 0 to 50"
                continue
            if v == cfg.defaults.get(key):
                db.delete_setting(key)  # back to the default: keep the table clean
            else:
                db.set_setting(key, v)
        if errors:
            return JSONResponse({"ok": False, "errors": errors}, status_code=400)
        after = settings()
        reindex = before["embed_model"] != after["embed_model"]
        if reindex:
            # Vectors from two models cannot be compared: start again from empty.
            db.reset_index()
            for s in db.list_sources():
                if s["enabled"]:
                    indexer.start(s["name"], full=True)
        return {"ok": True, "reindex": reindex}

    @app.post("/ui/sources/save", include_in_schema=False, dependencies=[Depends(require_admin)])
    def ui_source_save(body: dict = Body(...)):
        name = (body.get("name") or "").strip().lower()
        if not NAME_RE.match(name):
            raise HTTPException(400, "Name: lower-case letters, digits, - or _ (max 63)")
        plugin = plugins().get(body.get("plugin") or "")
        if not plugin:
            raise HTTPException(400, "Unknown source plug-in")
        options = {}
        for opt in plugin.options:
            v = (body.get("options") or {}).get(opt["key"], opt.get("default"))
            if opt.get("type") == "list" and isinstance(v, str):
                v = [x.strip() for x in v.splitlines() if x.strip()]
            if v not in (None, "", []):
                options[opt["key"]] = v
        examples = body.get("examples") or []
        if isinstance(examples, str):
            examples = [x.strip() for x in examples.splitlines() if x.strip()]
        is_new = db.get_source(name) is None
        db.upsert_source({"name": name, "plugin": plugin.name, "title": body.get("title") or name,
                          "enabled": bool(body.get("enabled", True)),
                          "interval_hours": interval_hours(body.get("interval_hours")),
                          "options": options, "examples": examples[:6], "origin": "ui"})
        if body.get("sync_now", is_new):
            indexer.start(name, full=True)
        return {"ok": True, "name": name}

    def paused() -> None:
        if not control.is_on("ask"):
            raise HTTPException(503, "vCommunity Ask is paused. Turn it on in Settings (Apps on / off).")

    @app.post("/ui/apps", include_in_schema=False, dependencies=[Depends(require_admin)])
    def ui_apps(body: dict = Body(...)):
        try:
            control.set_on(str(body.get("app") or ""), bool(body.get("on")), "ask")
        except control.ControlError as e:
            raise HTTPException(409, str(e))
        return control.status()

    @app.get("/api/apps", tags=["core"], dependencies=[Depends(require_token)])
    def api_apps():
        """Which parts are on: vCommunity Observability and vCommunity Ask (./control/apps.json,
        shared with the dashboards container)."""
        return control.status()

    @app.put("/api/apps/{name}", tags=["core"], dependencies=[Depends(require_token)])
    def api_app_set(name: str, on: bool):
        """Turn `observability` or `ask` on or off. Off pauses it; the container keeps running."""
        try:
            control.set_on(name, on, "ask")
        except control.ControlError as e:
            raise HTTPException(400 if name not in control.APPS else 409, str(e))
        return control.status()

    @app.post("/ui/sources/{name}/sync", include_in_schema=False, dependencies=[Depends(require_admin)])
    def ui_source_sync(name: str, body: dict = Body(default={})):
        if not db.get_source(name):
            raise HTTPException(404, "Unknown source")
        paused()
        started = indexer.start(name, full=bool(body.get("full")))
        return {"ok": True, "started": started}

    @app.post("/ui/sources/{name}/enabled", include_in_schema=False, dependencies=[Depends(require_admin)])
    def ui_source_enabled(name: str, body: dict = Body(...)):
        db.set_source_enabled(name, bool(body.get("enabled")))
        return {"ok": True}

    @app.put("/ui/sources/{name}/files/{filename}", include_in_schema=False, dependencies=[Depends(require_admin)])
    async def ui_upload(name: str, filename: str, request: Request):
        """Upload one file to a spreadsheet (.xlsx, .csv) or pdf (.pdf) source (the raw
        file is the request body), then sync the source."""
        src = db.get_source(name)
        if not src or src["plugin"] not in UPLOAD_TYPES_BY_PLUGIN:
            raise HTTPException(404, "Uploads are for spreadsheet and PDF sources")
        types = UPLOAD_TYPES_BY_PLUGIN[src["plugin"]]
        filename = os.path.basename(filename).strip()
        if Path(filename).suffix.lower() not in types or not UPLOAD_NAME_RE.match(filename):
            raise HTTPException(400, f"Upload a {' or '.join(sorted(types))} file (letters, digits, spaces and . _ - ( ) in the name)")
        if int(request.headers.get("content-length") or 0) > upload_max:
            raise HTTPException(413, f"The file is larger than {upload_max // 1048576} MB (ASK_UPLOAD_MAX_MB)")
        folder = indexer.upload_dir(name)
        folder.mkdir(parents=True, exist_ok=True)
        part = folder / f".{filename}.part"
        size = 0
        try:
            with part.open("wb") as f:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > upload_max:
                        raise HTTPException(413, f"The file is larger than {upload_max // 1048576} MB (ASK_UPLOAD_MAX_MB)")
                    f.write(chunk)
            if size == 0:
                raise HTTPException(400, "The file is empty")
            with part.open("rb") as f:
                head = f.read(4)
            if filename.lower().endswith(".xlsx") and head != b"PK\x03\x04":
                raise HTTPException(400, "That is not an .xlsx workbook (an .xls or .xlsm file? Save it as .xlsx)")
            if filename.lower().endswith(".pdf") and head != b"%PDF":
                raise HTTPException(400, "That is not a PDF file")
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        if (src["options"].get("uploads") or UPLOAD_DEFAULT_MODE[src["plugin"]]) == "replace":
            for old in folder.iterdir():
                if old.is_file() and old.suffix.lower() in types:
                    old.unlink()
        os.replace(part, folder / filename)
        indexer.start(name)
        return {"ok": True, "name": filename, "size": size}

    @app.post("/ui/sources/{name}/files/{filename}/delete", include_in_schema=False, dependencies=[Depends(require_admin)])
    def ui_upload_delete(name: str, filename: str):
        f = indexer.upload_dir(name) / os.path.basename(filename)
        if not f.is_file():
            raise HTTPException(404, "No such uploaded file")
        f.unlink()
        indexer.start(name)
        return {"ok": True}

    @app.post("/ui/sources/{name}/delete", include_in_schema=False, dependencies=[Depends(require_admin)])
    def ui_source_delete(name: str):
        if indexer.is_running(name):
            raise HTTPException(409, "A sync is running; try again when it finishes")
        db.delete_source(name)
        indexer.remove_workdir(name)
        return {"ok": True}

    # -- REST API -------------------------------------------------------------
    @app.get("/api/health", tags=["core"])
    def health():
        return {"status": "ok", "version": __version__, **db.stats()}

    @app.post("/api/ask", tags=["chat"], dependencies=[Depends(require_token)])
    def api_ask(body: dict = Body(..., examples=[{"question": "What does VM CPU Co-stop mean?", "sources": "all"}])):
        """Ask a question and wait for the whole answer. `sources`: "all", a source name or a
        list of names. Pass `chat_id` from a previous answer to ask a follow-up."""
        paused()
        question = (body.get("question") or "").strip()
        if not question:
            raise HTTPException(400, "Empty question")
        chat_id = body.get("chat_id") or ""
        if not chat_id or not db.get_chat(chat_id):
            s = body.get("sources") or "all"
            chat_id = db.new_chat(question, ",".join(map(str, s)) if isinstance(s, list) else str(s))
        docs, tools = split_scope(body.get("sources"))
        return rag.answer(db, settings(), chat_id, question, docs, tools)

    @app.get("/api/search", tags=["chat"], dependencies=[Depends(require_token)])
    def api_search(q: str, sources: str = "all"):
        """Retrieval only, no model answer: shows which chunks a question would get.
        `sources`: "all" (every source except spreadsheets), a name, or names separated by commas."""
        try:
            items = rag.retrieve(db, settings(), q, scope_list(sources))
        except OllamaError as e:
            raise HTTPException(502, str(e))
        return {"query": q, "fts": rag.fts_query(q), "results": rag.source_cards(items)}

    @app.get("/api/chats", tags=["chat"], dependencies=[Depends(require_token)])
    def api_chats(limit: int = 50):
        return db.list_chats(limit)

    @app.get("/api/chats/{chat_id}", tags=["chat"], dependencies=[Depends(require_token)])
    def api_chat(chat_id: str):
        chat = db.get_chat(chat_id)
        if not chat:
            raise HTTPException(404, "Unknown chat")
        return {**chat, "messages": db.messages(chat_id)}

    @app.get("/api/sources", tags=["sources"], dependencies=[Depends(require_token)])
    def api_sources():
        return [{**_source_view(s), "title": s["title"], "plugin": s["plugin"], "options": s["options"],
                 "interval_hours": s["interval_hours"]} for s in db.list_sources()]

    @app.post("/api/sources/{name}/sync", tags=["sources"], dependencies=[Depends(require_token)])
    def api_source_sync(name: str, full: bool = False):
        if not db.get_source(name):
            raise HTTPException(404, "Unknown source")
        paused()
        return {"started": indexer.start(name, full=full)}

    @app.get("/api/sources/{name}/documents", tags=["sources"], dependencies=[Depends(require_token)])
    def api_source_docs(name: str):
        return db.list_documents(name)

    app.state.db, app.state.indexer, app.state.settings = db, indexer, settings
    return app
