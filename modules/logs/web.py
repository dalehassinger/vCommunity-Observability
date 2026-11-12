"""Web extension of the Logs module: JSON routes for the Logs, Log alerts and Log settings
pages (/ui/logs/...), the REST API (/api/logs/..., token protected, in Swagger) and the
numbers for the Performance dashboard.

The pages themselves are the module's dashboards (module.yaml); their script
(static/logs.js) calls the /ui/logs routes.
"""

from __future__ import annotations

import csv
import io
import os
import json
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

from fastapi import Body, Depends, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse

sys.path.insert(0, str(Path(__file__).parent))
import logstore  # noqa: E402
import notify    # noqa: E402

HERE = Path(__file__).parent
MAX_LIMIT = 1000
COUNT_CAP = 100_000
RANGES = {"15m": 15, "1h": 60, "6h": 360, "24h": 1440, "3d": 4320, "7d": 10080}


def register(app, ctx) -> None:
    def con(readonly: bool = True) -> sqlite3.Connection:
        ctx.check_available()
        if readonly and not logstore.db_path(ctx.data_dir).exists():
            return logstore.connect(ctx.data_dir)        # creates the empty database
        return logstore.connect(ctx.data_dir, readonly=readonly)

    def time_window(rng: str | None, since: float | None, until: float | None) -> tuple[float | None, float | None]:
        if since is not None or until is not None:
            return since, until
        if rng and rng in RANGES:
            return time.time() - RANGES[rng] * 60, None
        return None, None

    def where_for(q: str, rng: str | None, since: float | None, until: float | None, sev: str | None, host: str | None,
                  app: str | None = None):
        try:
            s, u = time_window(rng, since, until)
            return logstore.build_where(q, since=s, until=u, min_severity=logstore.severity_number(sev), host=host or None,
                                        app=app or None)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    # -- search ------------------------------------------------------------------------
    def search(q: str = "", range: str | None = "1h", since: float | None = None, until: float | None = None,
               sev: str | None = None, host: str | None = None, app: str | None = None, limit: int = 200,
               before: int | None = None) -> dict[str, Any]:
        t0 = time.perf_counter()
        where, params = where_for(q, range, since, until, sev, host, app)
        limit = max(1, min(MAX_LIMIT, limit))
        c = con()
        try:
            page_where, page_params = where, list(params)
            if before:
                page_where += " AND id < ?"
                page_params.append(before)
            rows = [logstore.row_dict(r) for r in c.execute(
                f"SELECT * FROM logs WHERE {page_where} ORDER BY id DESC LIMIT ?", page_params + [limit])]
            total = c.execute(f"SELECT COUNT(*) FROM (SELECT 1 FROM logs WHERE {where} LIMIT {COUNT_CAP + 1})", params).fetchone()[0]
        except sqlite3.OperationalError as exc:
            raise HTTPException(400, f"Search failed: {exc}") from None
        finally:
            c.close()
        return {"items": rows, "count": len(rows), "total": min(total, COUNT_CAP), "total_capped": total > COUNT_CAP,
                "more": len(rows) == limit, "took_ms": round((time.perf_counter() - t0) * 1000, 1)}

    def histogram(q: str = "", range: str | None = "1h", since: float | None = None, until: float | None = None,
                  sev: str | None = None, host: str | None = None, app: str | None = None,
                  buckets: int = 60) -> dict[str, Any]:
        where, params = where_for(q, range, since, until, sev, host, app)
        s, u = time_window(range, since, until)
        c = con()
        try:
            end = u or time.time()
            if s is None:
                first = c.execute("SELECT MIN(ts) FROM logs").fetchone()[0]
                s = first or end - 3600
            buckets = max(10, min(240, buckets))
            span = max(end - s, 60)
            step = span / buckets
            out = [{"t": s + i * step, "error": 0, "warning": 0, "other": 0} for i in _range(buckets)]
            for b, grp, n in c.execute(
                f"SELECT CAST((ts - ?) / ? AS INTEGER) AS b, CASE WHEN severity <= 3 THEN 'error' WHEN severity = 4 THEN 'warning' "
                f"ELSE 'other' END AS g, COUNT(*) FROM logs WHERE {where} GROUP BY b, g", [s, step] + params):
                if 0 <= b < buckets:
                    out[b][grp] += n
        finally:
            c.close()
        return {"start": s, "end": end, "step": step, "buckets": out}

    def tail(after: int = 0, q: str = "", sev: str | None = None, host: str | None = None,
             app: str | None = None) -> dict[str, Any]:
        where, params = where_for(q, None, None, None, sev, host, app)
        c = con()
        try:
            if not after:
                last = c.execute("SELECT MAX(id) FROM logs").fetchone()[0] or 0
                return {"items": [], "last": last}
            rows = [logstore.row_dict(r) for r in c.execute(
                f"SELECT * FROM logs WHERE {where} AND id > ? ORDER BY id LIMIT 500", params + [after])]
            last = max([after] + [r["id"] for r in rows])
            if len(rows) < 500:
                last = max(last, c.execute("SELECT MAX(id) FROM logs").fetchone()[0] or 0)
        finally:
            c.close()
        return {"items": rows, "last": last}

    def hosts() -> dict[str, Any]:
        c = con()
        try:
            now = time.time()
            settings = logstore.load_settings(c)
            quiet = float(settings.get("silent_minutes") or 15) * 60
            rows = []
            for r in c.execute("SELECT host, MAX(ts) AS last_seen, MIN(ts) AS first_seen FROM logs GROUP BY host"):
                n24 = c.execute("SELECT COUNT(*) FROM logs WHERE host = ? AND ts >= ?", (r["host"], now - 86400)).fetchone()[0]
                err24 = c.execute("SELECT COUNT(*) FROM logs WHERE host = ? AND ts >= ? AND severity <= 3",
                                  (r["host"], now - 86400)).fetchone()[0]
                ip = c.execute("SELECT ip FROM logs WHERE host = ? ORDER BY id DESC LIMIT 1", (r["host"],)).fetchone()
                apps = [a[0] for a in c.execute(
                    "SELECT app FROM logs WHERE host = ? AND ts >= ? AND app IS NOT NULL GROUP BY app ORDER BY COUNT(*) DESC LIMIT 6",
                    (r["host"], now - 86400))]
                rows.append({"host": r["host"], "ip": ip[0] if ip else None, "apps": apps, "last_seen": r["last_seen"],
                             "first_seen": r["first_seen"], "messages_24h": n24, "errors_24h": err24,
                             "quiet": now - r["last_seen"] > quiet})
        finally:
            c.close()
        rows.sort(key=lambda x: (not x["quiet"], (x["host"] or "").lower()))
        return {"count": len(rows), "quiet_minutes": quiet / 60, "items": rows}

    def apps() -> dict[str, Any]:
        """Every app name seen (hosts' daemons, scripts, vCommunity Ask...): where from, how many, last seen."""
        c = con()
        try:
            now = time.time()
            rows = [{"app": r[0], "last_seen": r[1], "messages_24h": r[2], "errors_24h": r[3] or 0, "sources": r[4]}
                    for r in c.execute(
                        "SELECT app, MAX(ts), SUM(CASE WHEN ts >= ? THEN 1 ELSE 0 END), "
                        "SUM(CASE WHEN ts >= ? AND severity <= 3 THEN 1 ELSE 0 END), COUNT(DISTINCT host) "
                        "FROM logs WHERE app IS NOT NULL GROUP BY app", (now - 86400, now - 86400))]
        finally:
            c.close()
        rows.sort(key=lambda x: (x["app"] or "").lower())
        return {"count": len(rows), "items": rows}

    def summary() -> dict[str, Any]:
        c = con()
        try:
            now = time.time()
            settings = logstore.load_settings(c)
            hour = c.execute("SELECT COUNT(*), SUM(CASE WHEN severity <= 3 THEN 1 ELSE 0 END), "
                             "SUM(CASE WHEN severity = 4 THEN 1 ELSE 0 END) FROM logs WHERE ts >= ?", (now - 3600,)).fetchone()
            senders = c.execute("SELECT COUNT(DISTINCT host) FROM logs WHERE ts >= ?",
                                (now - float(settings.get("silent_minutes") or 15) * 60,)).fetchone()[0]
            all_hosts = c.execute("SELECT COUNT(*) FROM (SELECT DISTINCT host FROM logs)").fetchone()[0]
            oldest = c.execute("SELECT MIN(ts) FROM logs").fetchone()[0]
            newest = c.execute("SELECT MAX(ts) FROM logs").fetchone()[0]
            total = c.execute("SELECT MAX(id) - MIN(id) + 1 FROM logs").fetchone()[0] or 0
            active = c.execute("SELECT COUNT(*) FROM alert_events WHERE acked = 0").fetchone()[0]
            size = logstore.size_info(c, ctx.data_dir)
            state = logstore.get_state(c)
            rates = logstore.rates(c, now)
        finally:
            c.close()
        cap = float(settings.get("max_size_gb") or 5) * 1024 ** 3
        listener = ctx.service_status("syslog") or {}
        return {
            "last_hour": hour[0], "errors_last_hour": hour[1] or 0, "warnings_last_hour": hour[2] or 0,
            "senders_active": senders, "senders_total": all_hosts, "oldest": oldest, "newest": newest,
            "messages_approx": total, "unacked_alerts": active, "retention_days": settings.get("retention_days"),
            "max_size_bytes": cap, **size, "size_pct": round(size["used_bytes"] / cap * 100, 1) if cap else None,
            **logstore.disk_info(ctx.data_dir), **rates,
            "listener": {"running": bool(listener.get("running")), "since": listener.get("since"),
                         "restarts": listener.get("restarts", 0), "last_output": (listener.get("log") or [""])[-1],
                         "udp_port": state.get("udp_port"), "tcp_port": state.get("tcp_port"),
                         "received": state.get("received"), "dropped": state.get("dropped"),
                         "tcp_clients": state.get("tcp_clients"), "errors": state.get("errors") or [],
                         "host_port": _host_port(),
                         "updated": state.get("updated")},
        }

    def export_csv(q: str = "", range: str | None = "1h", since: float | None = None, until: float | None = None,
                   sev: str | None = None, host: str | None = None, app: str | None = None,
                   limit: int = 100_000) -> StreamingResponse:
        where, params = where_for(q, range, since, until, sev, host, app)
        limit = max(1, min(500_000, limit))

        def rows():
            c = con()
            try:
                buf = io.StringIO()
                w = csv.writer(buf)
                w.writerow(["time_utc", "host", "ip", "severity", "facility", "app", "procid", "msgid", "message_time", "message"])
                yield buf.getvalue()
                for r in c.execute(f"SELECT * FROM logs WHERE {where} ORDER BY id DESC LIMIT ?", params + [limit]):
                    d = logstore.row_dict(r)
                    buf.seek(0)
                    buf.truncate()
                    w.writerow([d["time"], d["host"], d["ip"], d["severity_name"], d["facility_name"], d["app"],
                                d["procid"], d["msgid"], d["msg_time"], d["message"]])
                    yield buf.getvalue()
            finally:
                c.close()
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        return StreamingResponse(rows(), media_type="text/csv",
                                 headers={"Content-Disposition": f'attachment; filename="logs-{stamp}.csv"'})

    # -- settings ------------------------------------------------------------------------
    def settings_view() -> dict[str, Any]:
        c = con()
        try:
            s = logstore.load_settings(c)
        finally:
            c.close()
        out = {k: v for k, v in s.items() if k not in logstore.SECRET_KEYS}
        out["smtp_password_set"] = bool(s.get("smtp_password"))
        return out

    def save_settings(body: dict[str, Any]) -> dict[str, Any]:
        values: dict[str, Any] = {}
        errors = []

        def number(key, lo, hi, cast=float):
            if key not in body:
                return
            try:
                v = cast(str(body[key]).strip())
            except ValueError:
                errors.append(f"{key.replace('_', ' ')}: not a number")
                return
            if not lo <= v <= hi:
                errors.append(f"{key.replace('_', ' ')}: use {lo:g} to {hi:g}")
                return
            values[key] = v

        number("retention_days", 0.5, 365)
        number("max_size_gb", 0.1, 10000)
        number("silent_minutes", 1, 1440, int)
        number("smtp_port", 1, 65535, int)
        for key in ("smtp_host", "smtp_user", "smtp_from", "smtp_to", "webhook_url"):
            if key in body:
                values[key] = str(body[key] or "").strip()
        if "smtp_security" in body:
            if body["smtp_security"] not in ("none", "starttls", "ssl"):
                errors.append("smtp security: none, starttls or ssl")
            values["smtp_security"] = body["smtp_security"]
        if body.get("smtp_password"):              # blank keeps the saved password
            values["smtp_password"] = str(body["smtp_password"])
        if body.get("clear_smtp_password"):
            values["smtp_password"] = ""
        if "alerts_enabled" in body:
            values["alerts_enabled"] = bool(body["alerts_enabled"])
        if values.get("webhook_url") and not values["webhook_url"].lower().startswith(("http://", "https://")):
            errors.append("webhook URL must start with http:// or https://")
        if errors:
            raise HTTPException(400, "Not saved: " + "; ".join(errors))
        c = con(readonly=False)
        try:
            logstore.save_settings(c, values)
        finally:
            c.close()
        return {"ok": True, "saved": sorted(values), "settings": settings_view(),
                "note": "Retention and the size cap apply at the next cleanup (within 5 minutes)."}

    # -- alerts ------------------------------------------------------------------------
    def rule_from(body: dict[str, Any]) -> dict[str, Any]:
        name = str(body.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "Give the alert a name")
        query = str(body.get("query") or "").strip()
        try:
            sev = logstore.severity_number(body.get("min_severity"))
            logstore.build_where(query)
            threshold = int(body.get("threshold") or 1)
            window = int(body.get("window_minutes") or 5)
            cooldown = int(body.get("cooldown_minutes") if body.get("cooldown_minutes") not in (None, "") else 30)
        except ValueError as exc:
            raise HTTPException(400, f"Not saved: {exc}") from None
        if not query and sev is None:
            raise HTTPException(400, "Not saved: give a search or a severity, or the alert would fire on every message")
        if not (1 <= threshold <= 1_000_000 and 1 <= window <= 10080 and 0 <= cooldown <= 10080):
            raise HTTPException(400, "Not saved: threshold 1 or more, window 1 to 10080 minutes, cooldown 0 to 10080 minutes")
        return {"name": name[:120], "enabled": 1 if body.get("enabled", True) else 0, "query": query, "min_severity": sev,
                "threshold": threshold, "window_minutes": window, "cooldown_minutes": cooldown,
                "notify_email": 1 if body.get("notify_email") else 0, "notify_webhook": 1 if body.get("notify_webhook") else 0}

    def alerts_view(limit: int = 200) -> dict[str, Any]:
        c = con()
        try:
            rules = [dict(r) for r in c.execute("SELECT * FROM alert_rules ORDER BY name COLLATE NOCASE")]
            events = []
            for r in c.execute("SELECT * FROM alert_events ORDER BY id DESC LIMIT ?", (max(1, min(1000, limit)),)):
                e = dict(r)
                try:
                    e["sample"] = json.loads(e["sample"] or "[]")
                except ValueError:
                    e["sample"] = []
                events.append(e)
            settings = logstore.load_settings(c)
        finally:
            c.close()
        for r in rules:
            r["min_severity_name"] = logstore.SEVERITIES[r["min_severity"]] if r["min_severity"] is not None else None
        return {"rules": rules, "events": events, "alerts_enabled": settings.get("alerts_enabled", True),
                "email_ready": bool(settings.get("smtp_host") and settings.get("smtp_to")),
                "webhook_ready": bool(settings.get("webhook_url")),
                "unacked": sum(1 for e in events if not e["acked"])}

    def save_rule(body: dict[str, Any]) -> dict[str, Any]:
        rule = rule_from(body)
        rid = body.get("id")
        c = con(readonly=False)
        try:
            with c:
                if rid:
                    sets = ", ".join(f"{k} = ?" for k in rule)
                    n = c.execute(f"UPDATE alert_rules SET {sets}, updated = ? WHERE id = ?",
                                  list(rule.values()) + [time.time(), int(rid)]).rowcount
                    if not n:
                        raise HTTPException(404, "Alert not found")
                else:
                    cols = ", ".join(rule)
                    rid = c.execute(f"INSERT INTO alert_rules({cols}, created, updated) VALUES ({', '.join('?' * len(rule))}, ?, ?)",
                                    list(rule.values()) + [time.time(), time.time()]).lastrowid
        finally:
            c.close()
        return {"ok": True, "id": int(rid)}

    def preview_rule(body: dict[str, Any]) -> dict[str, Any]:
        rule = rule_from({**body, "name": body.get("name") or "preview"})
        where, params = logstore.build_where(rule["query"], since=time.time() - rule["window_minutes"] * 60,
                                             min_severity=rule["min_severity"])
        c = con()
        try:
            n = c.execute(f"SELECT COUNT(*) FROM logs WHERE {where}", params).fetchone()[0]
            sample = [logstore.row_dict(r) for r in c.execute(f"SELECT * FROM logs WHERE {where} ORDER BY id DESC LIMIT 5", params)]
        finally:
            c.close()
        return {"count": n, "would_fire": n >= rule["threshold"], "sample": sample}

    # -- UI routes (open reads like the dashboards; changes need the token when one is set) ---
    @app.get("/ui/logs/static/{name}", include_in_schema=False)
    def static(name: str):
        if not re.fullmatch(r"[\w.-]+\.(js|css)", name) or not (HERE / "static" / name).is_file():
            raise HTTPException(404)
        return FileResponse(HERE / "static" / name, headers={"Cache-Control": "no-cache"})

    app.add_api_route("/ui/logs/search", search, methods=["GET"], include_in_schema=False)
    app.add_api_route("/ui/logs/histogram", histogram, methods=["GET"], include_in_schema=False)
    app.add_api_route("/ui/logs/tail", tail, methods=["GET"], include_in_schema=False)
    app.add_api_route("/ui/logs/hosts", hosts, methods=["GET"], include_in_schema=False)
    app.add_api_route("/ui/logs/apps", apps, methods=["GET"], include_in_schema=False)
    app.add_api_route("/ui/logs/summary", summary, methods=["GET"], include_in_schema=False)
    app.add_api_route("/ui/logs/export.csv", export_csv, methods=["GET"], include_in_schema=False)
    app.add_api_route("/ui/logs/settings", settings_view, methods=["GET"], include_in_schema=False)
    app.add_api_route("/ui/logs/alerts", alerts_view, methods=["GET"], include_in_schema=False)

    @app.put("/ui/logs/settings", include_in_schema=False)
    def ui_save_settings(request: Request, body: dict = Body(...)):
        ctx.check_admin(request)
        return save_settings(body)

    @app.post("/ui/logs/alerts", include_in_schema=False)
    def ui_save_rule(request: Request, body: dict = Body(...)):
        ctx.check_admin(request)
        return save_rule(body)

    @app.post("/ui/logs/alerts/preview", include_in_schema=False)
    def ui_preview(body: dict = Body(...)):
        return preview_rule(body)

    @app.post("/ui/logs/alerts/{rid}/delete", include_in_schema=False)
    def ui_delete_rule(rid: int, request: Request):
        ctx.check_admin(request)
        c = con(readonly=False)
        try:
            with c:
                c.execute("DELETE FROM alert_rules WHERE id = ?", (rid,))
        finally:
            c.close()
        return {"ok": True}

    @app.post("/ui/logs/alerts/{rid}/enabled", include_in_schema=False)
    def ui_enable_rule(rid: int, request: Request, body: dict = Body(...)):
        ctx.check_admin(request)
        c = con(readonly=False)
        try:
            with c:
                c.execute("UPDATE alert_rules SET enabled = ? WHERE id = ?", (1 if body.get("enabled") else 0, rid))
        finally:
            c.close()
        return {"ok": True}

    @app.post("/ui/logs/events/ack", include_in_schema=False)
    def ui_ack(request: Request, body: dict = Body(...)):
        """Acknowledge fired alerts: {"ids": [...]} or {"all": true}; {"delete": true} removes them."""
        ctx.check_admin(request)
        ids = [int(i) for i in body.get("ids") or []]
        c = con(readonly=False)
        try:
            with c:
                if body.get("delete"):
                    sql, args = ("DELETE FROM alert_events", []) if body.get("all") else \
                        (f"DELETE FROM alert_events WHERE id IN ({','.join('?' * len(ids))})", ids)
                else:
                    sql, args = ("UPDATE alert_events SET acked = 1", []) if body.get("all") else \
                        (f"UPDATE alert_events SET acked = 1 WHERE id IN ({','.join('?' * len(ids))})", ids)
                n = c.execute(sql, args).rowcount if (ids or body.get("all")) else 0
        finally:
            c.close()
        return {"ok": True, "changed": n}

    @app.post("/ui/logs/notify/test", include_in_schema=False)
    def ui_notify_test(request: Request, body: dict = Body(...)):
        ctx.check_admin(request)
        c = con()
        try:
            settings = logstore.load_settings(c)
        finally:
            c.close()
        event = {"rule_id": 0, "rule_name": "Test notification", "fired_at": time.time(), "count": 1, "window_minutes": 1,
                 "query": "", "sample": [{"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "host": "vco",
                                          "severity_name": "info", "app": "logs", "message": "This is a test from vCommunity Logs."}],
                 "fired_at_text": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())}
        kind = body.get("kind")
        result = notify.notify(settings, event, kind == "email", kind == "webhook")
        return {"ok": "failed" not in result, "result": result}

    # -- REST API for MP Builder and scripts -------------------------------------------
    tok = [Depends(ctx.require_token)]
    app.add_api_route("/api/logs/search", search, methods=["GET"], tags=["logs"], dependencies=tok,
                      summary="Search syslog messages (q uses the Logs page search language; range 15m/1h/6h/24h/3d/7d or since/until epoch)")
    app.add_api_route("/api/logs/summary", summary, methods=["GET"], tags=["logs"], dependencies=tok,
                      summary="Message counts, rates, storage and listener status")
    app.add_api_route("/api/logs/hosts", hosts, methods=["GET"], tags=["logs"], dependencies=tok,
                      summary="Every source that sent syslog (hosts, appliances, apps such as vCommunity Ask): "
                              "last seen, top apps, messages and errors in 24 h, idle flag")
    app.add_api_route("/api/logs/apps", apps, methods=["GET"], tags=["logs"], dependencies=tok,
                      summary="Every app name seen: last seen, messages and errors in 24 h, how many sources")
    app.add_api_route("/api/logs/alerts", alerts_view, methods=["GET"], tags=["logs"], dependencies=tok,
                      summary="Alert rules and the alerts they fired")

    # -- Performance dashboard (Ask) reads these from core GET /perf/container -------------
    def perf() -> dict[str, Any]:
        if not logstore.db_path(ctx.data_dir).exists():
            return {**logstore.disk_info(ctx.data_dir), "per_second": 0, "per_minute": 0, "per_hour": 0, "used_bytes": 0}
        c = logstore.connect(ctx.data_dir, readonly=True)
        try:
            settings = logstore.load_settings(c)
            out = {**logstore.rates(c), **logstore.size_info(c, ctx.data_dir), **logstore.disk_info(ctx.data_dir)}
        finally:
            c.close()
        out["max_size_bytes"] = float(settings.get("max_size_gb") or 5) * 1024 ** 3
        out["retention_days"] = settings.get("retention_days")
        return out

    ctx.add_perf(perf)


_range = range   # the query parameter "range" shadows the built-in inside the route functions


def _host_port() -> int:
    """The port senders use: docker-compose passes SYSLOG_PORT (.env) in as VCO_SYSLOG_HOST_PORT."""
    try:
        return int(os.environ.get("VCO_SYSLOG_HOST_PORT") or 514)
    except ValueError:
        return 514
