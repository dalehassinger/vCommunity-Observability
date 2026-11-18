"""Saved prompts: the /saved page, its /ui/saved endpoints and the /api/saved REST API.

The store and checks live in saved.py; this file only holds the web routes."""
from __future__ import annotations

import json
import time
from types import SimpleNamespace
from typing import Any

from fastapi import Body, Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from . import mcp, rag, websearch
from . import saved as saved_mod


def register(app: FastAPI, ctx: SimpleNamespace) -> None:
    """Add the routes to the app. ctx holds what main.create_app shares with them."""
    db, page, paused, settings = ctx.db, ctx.page, ctx.paused, ctx.settings
    split_scope, usable_scopes = ctx.split_scope, ctx.usable_scopes
    require_admin, require_token = ctx.require_admin, ctx.require_token
    saved = ctx.saved

    # -- Saved prompts ----------------------------------------------------------
    def saved_or_404(pid: int) -> dict[str, Any]:
        s = saved.get(pid)
        if not s:
            raise HTTPException(404, "No such saved prompt")
        return s

    def with_status(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Each prompt with "unavailable": the sources it picks that can't be used now (deleted
        or turned off, web search without its key, MCP server down). Run leaves them out."""
        usable = usable_scopes()
        for it in items:
            it["unavailable"] = saved_mod.fit_scope(it["scope"], usable)[1]
        return items

    def filled(s: dict[str, Any], values: Any) -> str:
        """The question with the {variables} filled in; 400 names the ones still missing."""
        if not isinstance(values, dict):
            values = {}
        try:
            return saved_mod.question(s, values, strict=True)
        except KeyError as e:
            raise HTTPException(400, f"Give a value for {{{e.args[0]}}}.")

    def export_response() -> Response:
        stamp = time.strftime("%Y%m%d", time.localtime())
        return Response(json.dumps(saved.export(), indent=2, ensure_ascii=False), media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="saved-prompts-{stamp}.json"'})

    def import_body(body: Any) -> dict[str, Any]:
        try:
            return {"ok": True, **saved.import_items(body)}
        except ValueError as e:
            raise HTTPException(400, str(e))

    def save_saved(body: dict, pid: int | None = None):
        try:
            return saved.save(body, pid)
        except ValueError as e:
            return JSONResponse({"errors": e.args[0]}, status_code=400)
        except KeyError:
            raise HTTPException(404, "No such saved prompt")

    @app.get("/saved", response_class=HTMLResponse, include_in_schema=False)
    def saved_page(request: Request, edit: int | None = None, new: bool = False):
        s = settings()
        current = saved.get(edit) if edit else None
        return page(request, "saved.html", "/saved", items=with_status(saved.list()), current=current, all_tags=saved.all_tags(),
                    show_form=bool(current) or new, mcp_servers=mcp.quick_servers(s["agent_url"]),
                    web_search=websearch.status(s), brave_search=websearch.brave_status(s), local_fetch=websearch.local_status(s))

    @app.get("/ui/saved", include_in_schema=False)
    def ui_saved():
        return {"items": with_status(saved.list())}

    @app.get("/ui/saved/export", include_in_schema=False)
    def ui_saved_export():
        return export_response()

    @app.post("/ui/saved/import", include_in_schema=False, dependencies=[Depends(require_admin)])
    def ui_saved_import(body: Any = Body(...)):
        return import_body(body)

    @app.get("/ui/saved/{pid}", include_in_schema=False)
    def ui_saved_one(pid: int):
        return with_status([saved_or_404(pid)])[0]

    @app.post("/ui/saved/{pid}/question", include_in_schema=False)
    def ui_saved_question(pid: int, body: dict = Body(default={})):
        """The question to send, with the {variables} filled in (Run asks for their values)."""
        return {"question": filled(saved_or_404(pid), body.get("values"))}

    @app.post("/ui/saved/{pid}/pin", include_in_schema=False)
    def ui_saved_pin(request: Request, pid: int, body: dict = Body(default={})):
        """Pin or unpin (like deleting a chat: the pages' own action, no token needed)."""
        ctx.same_origin(request)
        saved_or_404(pid)
        saved.pin(pid, bool(body.get("on", True)))
        return {"ok": True}

    @app.post("/ui/saved/save", include_in_schema=False, dependencies=[Depends(require_admin)])
    def ui_saved_save(body: dict = Body(...)):
        pid = body.get("id")
        return save_saved(body, int(pid) if pid else None)

    @app.post("/ui/saved/delete", include_in_schema=False, dependencies=[Depends(require_admin)])
    def ui_saved_delete(body: dict = Body(...)):
        try:
            ids = [int(i) for i in body.get("ids") or []]
        except (TypeError, ValueError):
            raise HTTPException(400, "ids must be numbers")
        return {"ok": True, "deleted": saved.delete_many(ids)}

    STAG = ["saved prompts"]

    @app.get("/api/saved", tags=STAG, dependencies=[Depends(require_token)])
    def api_saved():
        """Every saved prompt: name, prompt, scope, model, pages to fetch, how often used.
        `question` is what Run sends (the prompt plus the pages to read)."""
        return {"items": saved.list()}

    @app.post("/api/saved", tags=STAG, dependencies=[Depends(require_token)])
    def api_saved_create(body: dict = Body(..., examples=[{
            "name": "VCF release notes", "prompt": "List the newest fixes in these release notes.",
            "scope": "web:fetch", "urls": ["https://example.com/release-notes"], "tags": "vcf, news"}])):
        """Create a saved prompt. scope: like the chat ("all", source names, "web:fetch",
        "web:ollama", "web:brave", "mcp:<server>", "none"; comma-separated or a list).
        Optional: name (default: the start of the prompt), model, urls (pages to fetch, at most
        10; needs web:fetch, web:local or web:ollama), tags (comma-separated or a list, for example
        "sports, news"; at most 10), pinned. The prompt and pages may hold {variables}, for
        example {city} or {city=Dillsburg} with a default, asked for when it runs."""
        return save_saved(body)

    @app.get("/api/saved/export", tags=STAG, dependencies=[Depends(require_token)])
    def api_saved_export():
        """Every saved prompt as a JSON file (name, prompt, scope, model, urls, tags, pinned) to
        keep, share or import into another Ask."""
        return export_response()

    @app.post("/api/saved/import", tags=STAG, dependencies=[Depends(require_token)])
    def api_saved_import(body: Any = Body(..., examples=[{"items": [{"name": "Weather", "prompt": "Weather in {city=Dillsburg}?",
                                                                     "scope": "web:ollama", "tags": "weather"}]}])):
        """Add saved prompts from an export ({"items": [...]} or a list). A prompt with the same
        name as one here is replaced (its use count is kept). Returns added, replaced and skipped."""
        return import_body(body)

    @app.put("/api/saved/{pid}/pin", tags=STAG, dependencies=[Depends(require_token)])
    def api_saved_pin(pid: int, on: bool = True):
        """Pin a saved prompt (listed first) or unpin it."""
        saved_or_404(pid)
        return {"ok": saved.pin(pid, on)}

    @app.get("/api/saved/{pid}", tags=STAG, dependencies=[Depends(require_token)])
    def api_saved_one(pid: int):
        return saved_or_404(pid)

    @app.put("/api/saved/{pid}", tags=STAG, dependencies=[Depends(require_token)])
    def api_saved_update(pid: int, body: dict = Body(...)):
        """Replace a saved prompt (same fields as create)."""
        return save_saved(body, pid)

    @app.delete("/api/saved/{pid}", tags=STAG, dependencies=[Depends(require_token)])
    def api_saved_delete(pid: int):
        saved_or_404(pid)
        return {"ok": saved.delete(pid)}

    @app.post("/api/saved/{pid}/run", tags=STAG, dependencies=[Depends(require_token)])
    def api_saved_run(pid: int, body: dict = Body(default={}, examples=[{"values": {"city": "Dillsburg"}}])):
        """Ask the saved prompt in a new chat and wait for the whole answer (like POST /api/ask).
        values: the {variables} of the prompt (a variable with a default may be left out)."""
        paused()
        s = saved_or_404(pid)
        q = filled(s, (body or {}).get("values"))
        # Sources that can't be used now are left out; with none left, just the model answers.
        scope, missing = saved_mod.fit_scope(s["scope"], usable_scopes())
        chat_id = db.new_chat(s["name"], scope)
        docs, tools = split_scope(scope)
        cfg_now = settings()
        if s["model"]:
            cfg_now = {**cfg_now, "chat_model": s["model"]}
        result = rag.answer(db, cfg_now, chat_id, q, docs, tools)
        if not result.get("error"):
            saved.used(pid)
        if missing:
            result = {**result, "scope_used": scope, "unavailable": missing}
        return result
