"""Scheduled prompts: the /schedules page, its /ui/schedules endpoints and the /api/schedules REST API.

The scheduling and sending live in schedules.py; this file only holds the web routes."""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from fastapi import Body, Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import mcp, websearch
from . import saved as saved_mod
from . import schedules as sched


def register(app: FastAPI, ctx: SimpleNamespace) -> None:
    """Add the routes to the app. ctx holds what main.create_app shares with them."""
    page, paused, settings = ctx.page, ctx.paused, ctx.settings
    require_admin, require_token = ctx.require_admin, ctx.require_token
    schedules = ctx.schedules

    # -- scheduled prompts ------------------------------------------------------
    def schedule_or_404(sid: int) -> dict[str, Any]:
        s = schedules.get(sid)
        if not s:
            raise HTTPException(404, "No such scheduled prompt")
        return s

    def save_schedule(body: dict, sid: int | None = None):
        try:
            return schedules.save(body, sid)
        except ValueError as e:
            return JSONResponse({"errors": json.loads(str(e))}, status_code=400)
        except KeyError:
            raise HTTPException(404, "No such scheduled prompt")

    def run_schedule(sid: int) -> dict[str, Any]:
        schedule_or_404(sid)
        paused()
        if not schedules.start(sid):
            raise HTTPException(409, "It is already running.")
        return {"ok": True, "started": True}

    @app.get("/schedules", response_class=HTMLResponse, include_in_schema=False)
    def schedules_page(request: Request, edit: int | None = None, new: bool = False, saved: int | None = None):
        s = settings()
        current = schedules.get(edit) if edit else None
        from_saved = None
        if saved and not current:
            # "Schedule this" on a saved prompt: a new schedule filled with its prompt, sources
            # and model ({variables} take their defaults; the pages go in as "Read these pages").
            p = ctx.saved.get(saved)
            if p:
                from_saved = p["name"]
                current = {"name": p["name"], "prompt": saved_mod.question(p, {}), "scope": p["scope"], "model": p["model"]}
        return page(request, "schedules.html", "/schedules", items=schedules.list(), current=current,
                    from_saved=from_saved, webhooks=schedules.notifier.webhooks(),
                    show_form=bool(current) or new, frequencies=sched.FREQUENCIES, weekdays=sched.WEEKDAYS,
                    default_tz=sched.DEFAULT_TZ, mcp_servers=mcp.quick_servers(s["agent_url"], s["mcp_http_servers"]),
                    web_search=websearch.status(s), brave_search=websearch.brave_status(s), local_fetch=websearch.local_status(s),
                    mail_token=bool(sched.Notifier.token()))

    @app.get("/ui/schedules", include_in_schema=False)
    def ui_schedules():
        return {"items": schedules.list()}

    @app.get("/ui/schedules/{sid}/runs", include_in_schema=False)
    def ui_schedule_runs(sid: int):
        schedule_or_404(sid)
        return {"items": schedules.runs(sid)}

    @app.post("/ui/schedules/save", include_in_schema=False, dependencies=[Depends(require_admin)])
    def ui_schedule_save(body: dict = Body(...)):
        sid = body.get("id")
        return save_schedule(body, int(sid) if sid else None)

    @app.post("/ui/schedules/{sid}/run", include_in_schema=False, dependencies=[Depends(require_admin)])
    def ui_schedule_run(sid: int):
        return run_schedule(sid)

    @app.post("/ui/schedules/{sid}/enabled", include_in_schema=False, dependencies=[Depends(require_admin)])
    def ui_schedule_enabled(sid: int, body: dict = Body(...)):
        schedule_or_404(sid)
        return schedules.set_enabled(sid, bool(body.get("on")))

    @app.post("/ui/schedules/{sid}/delete", include_in_schema=False, dependencies=[Depends(require_admin)])
    def ui_schedule_delete(sid: int):
        schedule_or_404(sid)
        return {"ok": schedules.delete(sid)}

    @app.post("/ui/schedules/test-email", include_in_schema=False, dependencies=[Depends(require_admin)])
    def ui_schedule_test_email(body: dict = Body(...)):
        to = sched.recipients(body.get("recipients"))
        bad = [a for a in to if not sched.EMAIL_RE.match(a)]
        if not to or bad:
            raise HTTPException(400, "Enter valid email addresses first." + (f" Not valid: {', '.join(bad)}" if bad else ""))
        text = "This is a test from vCommunity Ask. Scheduled prompt emails go through this mail server."
        try:
            result = schedules.notifier.send(to, "[Ask] Test email", text, f"<p>{text}</p>")
        except RuntimeError as e:
            raise HTTPException(502, str(e))
        return {"ok": True, "result": result}

    @app.post("/ui/schedules/test-webhook", include_in_schema=False, dependencies=[Depends(require_admin)])
    def ui_schedule_test_webhook(body: dict = Body(default={})):
        try:
            result = schedules.notifier.webhook("[Ask] Test", "This is a test from **vCommunity Ask**. "
                                              "Scheduled prompts with Webhook on post their answers here.",
                                              name=str(body.get("webhook_name") or ""), color="ok")
        except RuntimeError as e:
            raise HTTPException(502, str(e))
        return {"ok": True, "result": result}


    TAG = ["scheduled prompts"]

    @app.get("/api/homeview", tags=TAG, dependencies=[Depends(require_token)])
    def api_homeview():
        """The latest answer of each scheduled prompt with "Show on the HomeView" on (read by
        the Apple HomeView wall display in vCommunity Observability). Citations are removed."""
        return {"items": schedules.homeview(), "public_url": sched.public_base(settings())}

    @app.get("/api/schedules", tags=TAG, dependencies=[Depends(require_token)])
    def api_schedules():
        """Every scheduled prompt with its schedule, next run and last run."""
        return {"items": schedules.list()}

    @app.post("/api/schedules", tags=TAG, dependencies=[Depends(require_token)])
    def api_schedule_create(body: dict = Body(..., examples=[{
            "name": "Weekly blog summary", "prompt": "Summarize the posts published in the last 7 days, with their URLs.",
            "scope": "vcrocs", "frequency": "weekly", "weekday": "mon", "at_time": "07:00",
            "timezone": "America/New_York", "recipients": "you@example.com"}])):
        """Create a scheduled prompt. scope: "all", source names (comma-separated or a list),
        "web:ollama", "web:brave", "web:fetch", "mcp:<server>" or "none". frequency: daily, weekdays, weekly
        (weekday mon..sun), monthly (monthday 1-28 or last) or hours (every_hours). Optional: model,
        sync_first, attach_report (default true), only_changed, enabled (default true),
        send_email (default true; recipients needed then), send_notes (default false: a new note
        in Apple Notes on the Mac through the host agent) and send_webhook (default false: posts to
        the webhook on Log alerts → Notifications in vCommunity Observability)."""
        return save_schedule(body)

    @app.get("/api/schedules/{sid}", tags=TAG, dependencies=[Depends(require_token)])
    def api_schedule(sid: int):
        return {**schedule_or_404(sid), "runs": schedules.runs(sid)}

    @app.put("/api/schedules/{sid}", tags=TAG, dependencies=[Depends(require_token)])
    def api_schedule_update(sid: int, body: dict = Body(...)):
        """Replace a scheduled prompt (same fields as create; fields left out take their defaults)."""
        return save_schedule(body, sid)

    @app.delete("/api/schedules/{sid}", tags=TAG, dependencies=[Depends(require_token)])
    def api_schedule_delete(sid: int):
        schedule_or_404(sid)
        return {"ok": schedules.delete(sid)}

    @app.post("/api/schedules/{sid}/run", tags=TAG, dependencies=[Depends(require_token)])
    def api_schedule_run(sid: int):
        """Run it now in the background and email the result (see GET /api/schedules/{id} for the run)."""
        return run_schedule(sid)
