"""vCommunity Ask: chat with your own knowledge, answered by your local Ollama.

Run locally:   uvicorn app.main:create_app --factory --port 8081   (from the ask/ folder)
In Docker:     see ask/Dockerfile and the vco-ask service in docker-compose.yml"""

from __future__ import annotations

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
from . import rag

HERE = Path(__file__).resolve().parent
log = logging.getLogger("ask")
IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
UPLOAD_TYPES = {".xlsx", ".csv"}
UPLOAD_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()+-]{0,150}$")


def shared_dir() -> Path:
    """The first container's theme (app.css, Swagger UI). In the image it is copied to
    /app/shared; when run from the repo it is read from ../core/static."""
    for p in (os.environ.get("ASK_SHARED_DIR"), "/app/shared", HERE.parent.parent / "core" / "static"):
        if p and (Path(p) / "app.css").exists():
            return Path(p)
    raise RuntimeError("Theme not found: set ASK_SHARED_DIR to the folder holding app.css")


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

    def due_syncs() -> None:
        now = time.time()
        for s in db.list_sources():
            if not s["enabled"] or indexer.is_running(s["name"]):
                continue
            last = s.get("last_end") or 0
            if s["status"] == "never" or now - last > s["interval_hours"] * 3600:
                indexer.start(s["name"])

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Every 10 minutes: sync each enabled source whose interval has passed. The first
        # check runs 5 seconds after start, so a new install indexes without a click.
        scheduler.add_job(due_syncs, "interval", minutes=10, id="due-syncs",
                          next_run_time=datetime.now(timezone.utc) + timedelta(seconds=5))
        scheduler.start()
        yield
        scheduler.shutdown(wait=False)

    app = FastAPI(title="vCommunity Ask API", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None,
                  description="Questions answered from your own knowledge sources by a local Ollama model.")
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.mount("/shared", StaticFiles(directory=shared_dir()), name="shared")
    templates = Jinja2Templates(directory=HERE / "templates")
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
            "observability_url": s["observability_url"], "version": __version__,
            "auth": bool(cfg.api_tokens), "settings": s, **ctx})

    def scope_list(scope: Any) -> list[str] | None:
        if not scope or scope == "all":
            return None
        names = [scope] if isinstance(scope, str) else list(scope)
        known = {s["name"] for s in db.list_sources()}
        return [n for n in names if n in known] or None

    # -- pages ----------------------------------------------------------------
    @app.get("/", include_in_schema=False)
    def home():
        return RedirectResponse("/chat")

    @app.get("/chat", response_class=HTMLResponse, include_in_schema=False)
    def chat_new(request: Request):
        examples = [e for s in db.list_sources() if s["enabled"] for e in s["examples"]][:6]
        return page(request, "chat.html", "/chat", chat=None, messages=[], examples=examples, stats=db.stats())

    @app.get("/chat/{chat_id}", response_class=HTMLResponse, include_in_schema=False)
    def chat_page(request: Request, chat_id: str):
        chat = db.get_chat(chat_id)
        if not chat:
            return RedirectResponse("/chat")
        return page(request, "chat.html", f"/chat/{chat_id}", chat=chat, messages=db.messages(chat_id),
                    examples=[], stats=db.stats())

    @app.get("/sources", response_class=HTMLResponse, include_in_schema=False)
    def sources_page(request: Request):
        srcs = db.list_sources()
        for s in srcs:
            view = _source_view(s)
            s["status"], s["last_error"] = view["status"], view["last_error"]
            if s["plugin"] == "spreadsheet":
                s["uploads"] = uploaded_files(s["name"])
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

    @app.get("/settings", response_class=HTMLResponse, include_in_schema=False)
    def settings_page(request: Request):
        overridden = set(db.get_settings())
        return page(request, "settings.html", "/settings", spec=SETTINGS, overridden=overridden,
                    defaults=cfg.defaults, config_path=str(cfg.path))

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
        if root_p not in f.parents or f.suffix.lower() not in IMAGE_TYPES or not f.is_file():
            raise HTTPException(404)
        return FileResponse(f, headers={"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
                                        "Cache-Control": "max-age=3600"})

    # -- UI endpoints (used by the pages) --------------------------------------
    @app.post("/ui/chat", include_in_schema=False)
    def ui_chat(request: Request, body: dict = Body(...)):
        same_origin(request)
        question = (body.get("question") or "").strip()
        if not question:
            raise HTTPException(400, "Empty question")
        chat_id = body.get("chat_id") or ""
        scope = body.get("scope") or "all"
        if not chat_id or not db.get_chat(chat_id):
            chat_id = db.new_chat(question, scope)
        return StreamingResponse(rag.answer_stream(db, settings(), chat_id, question, scope_list(scope)),
                                 media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

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
                          "interval_hours": max(0.25, float(body.get("interval_hours") or 24)),
                          "options": options, "examples": examples[:6], "origin": "ui"})
        if body.get("sync_now", is_new):
            indexer.start(name, full=True)
        return {"ok": True, "name": name}

    @app.post("/ui/sources/{name}/sync", include_in_schema=False, dependencies=[Depends(require_admin)])
    def ui_source_sync(name: str, body: dict = Body(default={})):
        if not db.get_source(name):
            raise HTTPException(404, "Unknown source")
        started = indexer.start(name, full=bool(body.get("full")))
        return {"ok": True, "started": started}

    @app.post("/ui/sources/{name}/enabled", include_in_schema=False, dependencies=[Depends(require_admin)])
    def ui_source_enabled(name: str, body: dict = Body(...)):
        db.set_source_enabled(name, bool(body.get("enabled")))
        return {"ok": True}

    @app.put("/ui/sources/{name}/files/{filename}", include_in_schema=False, dependencies=[Depends(require_admin)])
    async def ui_upload(name: str, filename: str, request: Request):
        """Upload one .xlsx or .csv file to a spreadsheet source (the raw file is the
        request body), then sync the source."""
        src = db.get_source(name)
        if not src or src["plugin"] != "spreadsheet":
            raise HTTPException(404, "Uploads are for spreadsheet sources")
        filename = os.path.basename(filename).strip()
        if Path(filename).suffix.lower() not in UPLOAD_TYPES or not UPLOAD_NAME_RE.match(filename):
            raise HTTPException(400, "Upload an .xlsx or .csv file (letters, digits, spaces and . _ - ( ) in the name)")
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
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        if (src["options"].get("uploads") or "replace") == "replace":
            for old in folder.iterdir():
                if old.is_file() and old.suffix.lower() in UPLOAD_TYPES:
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
        question = (body.get("question") or "").strip()
        if not question:
            raise HTTPException(400, "Empty question")
        chat_id = body.get("chat_id") or ""
        if not chat_id or not db.get_chat(chat_id):
            chat_id = db.new_chat(question, str(body.get("sources") or "all"))
        return rag.answer(db, settings(), chat_id, question, scope_list(body.get("sources")))

    @app.get("/api/search", tags=["chat"], dependencies=[Depends(require_token)])
    def api_search(q: str, sources: str = "all"):
        """Retrieval only, no model answer: shows which chunks a question would get."""
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
        return {"started": indexer.start(name, full=full)}

    @app.get("/api/sources/{name}/documents", tags=["sources"], dependencies=[Depends(require_token)])
    def api_source_docs(name: str):
        return db.list_documents(name)

    app.state.db, app.state.indexer, app.state.settings = db, indexer, settings
    return app
