"""Scheduled prompts: a saved question that Ask answers on a schedule and emails.

Each schedule has sources (the same Sources pick as the chat), a prompt, an optional model,
a schedule (daily, weekdays, weekly, monthly, or every N hours, in a time zone) and email
recipients. At run time the prompt is answered exactly like a chat (rag.answer, saved as a
chat named "Scheduled: <name> · <date>"), and the answer is emailed as HTML with its sources,
an "Open in Ask" link and the HTML report attached when the answer made one. It can also
(or instead) be posted to the chat webhook set there (Discord, Slack, ...).

Email goes through the mail server set up in the dashboards container (Log alerts →
Notifications) by calling its token-protected POST /api/logs/notify/email. Ask never sees
the SMTP password. Settings: ASK_VCO_INTERNAL_URL (default http://vco:8080) and the token in
ASK_VCO_TOKEN, else VCO_API_TOKEN_MPBUILDER (both containers read the same .env).
The webhook goes the same way (POST /api/logs/notify/webhook), so Ask never stores its URL.
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from markdown_it import MarkdownIt

from urllib.parse import urlsplit

from . import applenotes, control, links, perf, rag
from .db import DB

log = logging.getLogger("ask.schedules")

TABLES = """
CREATE TABLE IF NOT EXISTS schedules (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, prompt TEXT NOT NULL, scope TEXT NOT NULL DEFAULT 'all',
  model TEXT NOT NULL DEFAULT '', frequency TEXT NOT NULL DEFAULT 'daily', at_time TEXT NOT NULL DEFAULT '07:00',
  weekday TEXT NOT NULL DEFAULT 'mon', monthday TEXT NOT NULL DEFAULT '1', every_hours INTEGER NOT NULL DEFAULT 6,
  timezone TEXT NOT NULL DEFAULT 'America/New_York', recipients TEXT NOT NULL DEFAULT '',
  sync_first INTEGER NOT NULL DEFAULT 0, attach_report INTEGER NOT NULL DEFAULT 1,
  only_changed INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 1,
  include_prompt INTEGER NOT NULL DEFAULT 1, include_sources INTEGER NOT NULL DEFAULT 1,
  avoid_repeats INTEGER NOT NULL DEFAULT 0,
  send_email INTEGER NOT NULL DEFAULT 1, send_webhook INTEGER NOT NULL DEFAULT 0,
  webhook_name TEXT NOT NULL DEFAULT '', homeview INTEGER NOT NULL DEFAULT 0,
  send_notes INTEGER NOT NULL DEFAULT 0,
  created REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS schedule_runs (
  id INTEGER PRIMARY KEY, schedule INTEGER NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
  started REAL NOT NULL, finished REAL, status TEXT NOT NULL DEFAULT 'running',
  trigger TEXT NOT NULL DEFAULT 'schedule', chat_id TEXT NOT NULL DEFAULT '', email TEXT NOT NULL DEFAULT '',
  error TEXT NOT NULL DEFAULT '', answer_hash TEXT NOT NULL DEFAULT '', seconds REAL);
CREATE INDEX IF NOT EXISTS schedule_runs_sched ON schedule_runs(schedule, started);
"""

FREQUENCIES = {"daily": "Daily", "weekdays": "Weekdays (Mon–Fri)", "weekly": "Weekly", "monthly": "Monthly",
               "hours": "Every N hours"}
WEEKDAYS = [("mon", "Monday"), ("tue", "Tuesday"), ("wed", "Wednesday"), ("thu", "Thursday"),
            ("fri", "Friday"), ("sat", "Saturday"), ("sun", "Sunday")]
DEFAULT_TZ = os.environ.get("ASK_SCHEDULE_TZ") or "America/New_York"
EMAIL_RE = re.compile(r"^[^@\s,;<>\"]+@[^@\s,;<>\"]+\.[^@\s,;<>\"]+$")
KEEP_RUNS = 100                  # run history rows kept per schedule
SHOW_RUNS = 25                   # rows shown by History (and the API's "runs")
AVOID_RUNS = 30                  # "Don't repeat recent answers": this many earlier answers are sent along
AVOID_EACH_CHARS = 600           # each earlier answer is cut to this length
SYNC_WAIT_S = 30 * 60            # "Sync the sources first": wait at most this long
MAX_ATTACH_BYTES = 12 * 1024 * 1024


# -- validation ------------------------------------------------------------------
def recipients(value: Any) -> list[str]:
    items = value if isinstance(value, list) else str(value or "").replace(";", ",").replace("\n", ",").split(",")
    return [str(a).strip() for a in items if str(a).strip()]


def clean_when(body: dict[str, Any], s: dict[str, Any], errors: dict[str, str]) -> None:
    """The time fields (frequency, at_time, weekday, monthday, every_hours, timezone) into `s`."""
    s["frequency"] = str(body.get("frequency") or "daily")
    if s["frequency"] not in FREQUENCIES:
        errors["frequency"] = "Pick daily, weekdays, weekly, monthly or every N hours."
    s["at_time"] = str(body.get("at_time") or "07:00").strip()
    if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", s["at_time"]):
        errors["at_time"] = "Time as HH:MM (24 hours)."
    s["weekday"] = str(body.get("weekday") or "mon")
    if s["weekday"] not in dict(WEEKDAYS):
        errors["weekday"] = "Pick a day of the week."
    s["monthday"] = str(body.get("monthday") or "1").strip()
    if s["monthday"] != "last" and not (s["monthday"].isdigit() and 1 <= int(s["monthday"]) <= 28):
        errors["monthday"] = "Day 1 to 28, or last."
    try:
        s["every_hours"] = int(body.get("every_hours") or 6)
        if not 1 <= s["every_hours"] <= 168:
            raise ValueError
    except (TypeError, ValueError):
        errors["every_hours"] = "1 to 168 hours."
        s["every_hours"] = 6
    s["timezone"] = str(body.get("timezone") or DEFAULT_TZ).strip()
    try:
        ZoneInfo(s["timezone"])
    except (ZoneInfoNotFoundError, ValueError):
        errors["timezone"] = "Unknown time zone (for example America/New_York or UTC)."


# Knowledge source re-syncs: "interval" keeps the old Re-sync every N hours (from the last
# sync, decimals allowed, 0 = never); the others run at a set time like scheduled prompts.
SYNC_FREQUENCIES = {"interval": "Every N hours", "daily": "Daily", "weekdays": "Weekdays (Mon–Fri)",
                    "weekly": "Weekly", "monthly": "Monthly"}


def clean_sync_schedule(body: Any) -> tuple[dict[str, Any], dict[str, str]]:
    """A source's re-sync schedule from the form or API: {} for "interval", else the time fields."""
    body = body if isinstance(body, dict) else {}
    freq = str(body.get("frequency") or "interval")
    if freq == "interval":
        return {}, {}
    if freq not in SYNC_FREQUENCIES:
        return {}, {"frequency": "Pick every N hours, daily, weekdays, weekly or monthly."}
    s: dict[str, Any] = {}
    errors: dict[str, str] = {}
    clean_when(body, s, errors)
    for k in ("every_hours",):
        s.pop(k, None)
    return s, errors


def sync_when(src: dict[str, Any]) -> str:
    """"Daily at 06:00 America/New_York · next 2026-11-15 06:00 EST" or "every 24 h" for the Sources page."""
    sch = src.get("sync_schedule") or {}
    if sch.get("frequency"):
        nxt = next_run({**sch, "every_hours": 6, "enabled": 1}) if src.get("enabled", True) else ""
        return describe({**sch, "every_hours": 6}) + (f" · next {nxt}" if nxt else "")
    hours = float(src.get("interval_hours") or 0)
    if hours <= 0:
        return "never (Sync now only)"
    return f"every {('%g' % round(hours, 2))} h"


def clean(body: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """The saved fields from a form or API body, and errors per field."""
    errors: dict[str, str] = {}
    s: dict[str, Any] = {}
    s["name"] = str(body.get("name") or "").strip()[:120]
    if not s["name"]:
        errors["name"] = "Give it a name (it is also the email subject)."
    s["prompt"] = str(body.get("prompt") or "").strip()
    if not s["prompt"]:
        errors["prompt"] = "Write the prompt."
    scope = body.get("scope") or "all"
    s["scope"] = ",".join(map(str, scope)) if isinstance(scope, list) else str(scope)
    s["scope"] = s["scope"].strip() or "all"
    s["model"] = str(body.get("model") or "").strip()
    clean_when(body, s, errors)
    for k, default in (("send_email", True), ("send_webhook", False), ("send_notes", False)):
        v = body.get(k, default)
        s[k] = 1 if (v is True or str(v).lower() in ("1", "true", "on", "yes")) else 0
    if not s["send_email"] and not s["send_webhook"] and not s["send_notes"]:
        errors["send_to"] = "Pick Email, Webhook, Apple Notes or more than one."
    s["webhook_name"] = " ".join(str(body.get("webhook_name") or "").split())[:40]
    if s["webhook_name"].lower() == "default":
        s["webhook_name"] = ""
    to = recipients(body.get("recipients"))
    bad = [a for a in to if not EMAIL_RE.match(a)]
    if not to:
        if s["send_email"]:
            errors["recipients"] = "Add at least one email address."
    elif bad:
        errors["recipients"] = "Not an email address: " + ", ".join(bad)
    elif len(to) > 20:
        errors["recipients"] = "At most 20 addresses."
    s["recipients"] = ", ".join(to)
    for k, default in (("sync_first", False), ("attach_report", True), ("only_changed", False), ("enabled", True),
                       ("include_prompt", True), ("include_sources", True), ("avoid_repeats", False), ("homeview", False)):
        v = body.get(k, default)
        s[k] = 1 if (v is True or str(v).lower() in ("1", "true", "on", "yes")) else 0
    return s, errors


def trigger(s: dict[str, Any]):
    hour, minute = (int(x) for x in s["at_time"].split(":"))
    tz = ZoneInfo(s["timezone"])
    if s["frequency"] == "hours":
        # Every N hours, counted from the set time today (so 6 h at 07:00 = 07, 13, 19, 01).
        start = datetime.now(tz).replace(hour=hour, minute=minute, second=0, microsecond=0) - timedelta(days=1)
        return IntervalTrigger(hours=int(s["every_hours"]), start_date=start, timezone=tz)
    kw: dict[str, Any] = {"hour": hour, "minute": minute, "timezone": tz}
    if s["frequency"] == "weekdays":
        kw["day_of_week"] = "mon-fri"
    elif s["frequency"] == "weekly":
        kw["day_of_week"] = s["weekday"]
    elif s["frequency"] == "monthly":
        kw["day"] = s["monthday"]
    return CronTrigger(**kw)


def describe(s: dict[str, Any]) -> str:
    at = f"at {s['at_time']} {s['timezone']}"
    f = s["frequency"]
    if f == "hours":
        return f"Every {s['every_hours']} hours from {s['at_time']} {s['timezone']}"
    if f == "weekdays":
        return f"Weekdays {at}"
    if f == "weekly":
        return f"Every {dict(WEEKDAYS).get(s['weekday'], s['weekday'])} {at}"
    if f == "monthly":
        day = "last day" if s["monthday"] == "last" else f"day {s['monthday']}"
        return f"Monthly on the {day} {at}"
    return f"Daily {at}"


def next_run(s: dict[str, Any]) -> str:
    if not s.get("enabled"):
        return ""
    try:
        tz = ZoneInfo(s["timezone"])
        nxt = trigger(s).get_next_fire_time(None, datetime.now(tz))
    except Exception:  # noqa: BLE001 - a broken row shows no next run
        return ""
    return nxt.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z") if nxt else ""


# -- email -------------------------------------------------------------------------
_MD = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False}).enable(["table", "strikethrough"])
_STYLES = {
    "h1": "font-size:20px;margin:18px 0 8px", "h2": "font-size:17px;margin:16px 0 6px", "h3": "font-size:15px;margin:14px 0 6px",
    "p": "margin:0 0 10px", "ul": "margin:0 0 10px;padding-left:22px", "ol": "margin:0 0 10px;padding-left:22px",
    "table": "border-collapse:collapse;margin:0 0 12px;font-size:13px",
    "th": "border:1px solid #c9d3da;background:#eef2f5;padding:5px 8px;text-align:left",
    "td": "border:1px solid #c9d3da;padding:5px 8px;vertical-align:top",
    "pre": "background:#f4f6f8;border:1px solid #d9e0e5;border-radius:4px;padding:10px 12px;overflow-x:auto;font-size:12px",
    "code": "font-family:Menlo,Consolas,monospace;font-size:12px", "blockquote": "margin:0 0 10px;padding:0 12px;border-left:3px solid #c9d3da;color:#4a5a66",
    "a": "color:#0072a3",
}


def md_html(text: str) -> str:
    """Markdown → HTML for mail clients: raw HTML in the answer is escaped (never passed on),
    and styles are inline because many clients drop <style> blocks."""
    out = _MD.render(text or "")
    for tag, style in _STYLES.items():
        out = re.sub(rf"<{tag}(?=[ >])", f'<{tag} style="{style}"', out)
    return out


def public_base(cfg: dict[str, Any]) -> str:
    """The Ask address for links in emails. When Ask address names this computer only
    (localhost, 127.0.0.1, host.docker.internal ...), the host agent on the Mac tells us the
    Mac's current network address and that is used instead, keeping the port. Without the
    agent (or an older one) the setting is used as it is."""
    url = str(cfg.get("public_url") or "").rstrip("/")
    if not url or (urlsplit(url).hostname or "").lower() not in links.LOCAL_NAMES:
        return url
    view = perf.host_view(cfg.get("agent_url") or "", os.environ.get("ASK_AGENT_TOKEN", ""))
    ip = (view or {}).get("lan_ip") or ""
    return links.follow_host(url, ip).rstrip("/") if ip else url


def _abs(url: str, public: str) -> str:
    url = url or ""
    if url.startswith(("http://", "https://")):
        return url
    return f"{public.rstrip('/')}{url}" if url.startswith("/") and public else ""


def source_lines(cards: list[dict[str, Any]], public: str) -> list[dict[str, str]]:
    out, seen = [], set()
    for c in cards or []:
        title = str(c.get("title") or c.get("name") or c.get("kind") or "source")
        if c.get("kind") == "tool":
            # A tool call: its name and server, and how it went when it didn't work.
            title = f"Tool {c.get('tool') or '?'}" + (f" ({c['server']})" if c.get("server") else "")
            if c.get("status") not in (None, "ok"):
                title += f": {c['status']}" + (f", {c['error']}" if c.get("error") else "")
        if c.get("heading"):
            title += f" > {c['heading']}"
        url = _abs(str(c.get("url") or ""), public)
        if not url and c.get("doc_row"):
            url = _abs(f"/doc/{c['doc_row']}", public)
        key = (title, url)
        if key in seen:
            continue
        seen.add(key)
        out.append({"n": str(c.get("n") or len(out) + 1), "title": title, "url": url})
    return out


def build_email(s: dict[str, Any], result: dict[str, Any], *, public: str, when: datetime,
                model: str, seconds: float) -> tuple[str, str, str]:
    """(subject, text, html) for one run. result: rag.answer's dict (answer, sources, error)."""
    stamp = when.strftime("%Y-%m-%d %H:%M %Z")
    failed = bool(result.get("error"))
    subject = f"[Ask] {s['name']}: {'failed ' if failed else ''}{when.strftime('%Y-%m-%d')}"
    chat_url = f"{public.rstrip('/')}/chat/{result['chat_id']}" if public and result.get("chat_id") else ""
    manage = f"{public.rstrip('/')}/schedules" if public else ""
    srcs = [] if failed or not s.get("include_sources", 1) else source_lines(result.get("sources") or [], public)
    show_prompt = failed or bool(s.get("include_prompt", 1))     # a failure always says what was asked
    answer = result.get("answer") or ""
    if not s.get("include_sources", 1):
        answer = CITATION_RE.sub("", answer)                     # no "[1]" pointing at a list that isn't there
    meta = f"{stamp} · model {model} · {seconds:.0f} s"
    stats = result.get("stats") or {}
    note = ""
    if not failed and stats.get("no_final_answer"):
        note = ("The model used all its tool turns without writing an answer. Below is what it said while it "
                "worked. Raise Tool turns per answer on the MCP tools page, or make the prompt more specific.")

    text = [s["name"], meta, ""] + ([f"Prompt: {s['prompt']}", ""] if show_prompt else [])
    if failed:
        text += ["The scheduled prompt failed:", str(result["error"]), ""]
    else:
        if note:
            text += [f"Note: {note}", ""]
        text += [answer or "(empty answer)", ""]
    if srcs:
        text += ["Sources:"] + [f"[{x['n']}] {x['title']}" + (f" - {x['url']}" if x["url"] else "") for x in srcs] + [""]
    if chat_url:
        text.append(f"Open in Ask: {chat_url}")
    if manage:
        text.append(f"Change or stop this schedule: {manage}")

    e = html.escape
    body = [f'<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;font-size:14px;line-height:1.5;color:#1b2a32;max-width:760px">',
            f'<div style="border-left:4px solid {"#c92100" if failed else "#0072a3"};padding:6px 12px;margin:0 0 14px;background:#f4f6f8">',
            f'<div style="font-size:17px;font-weight:600">{e(s["name"])}</div>',
            f'<div style="font-size:12px;color:#4a5a66">{e(meta)}</div>']
    if show_prompt:
        body.append(f'<div style="font-size:12px;color:#4a5a66;margin-top:4px"><b>Prompt:</b> {e(s["prompt"])}</div>')
    body.append('</div>')
    if failed:
        body.append(f'<p style="margin:0 0 10px"><b>The scheduled prompt failed.</b></p><pre style="{_STYLES["pre"]}">{e(str(result["error"]))}</pre>')
    else:
        if note:
            body.append(f'<p style="margin:0 0 10px;padding:6px 10px;background:#fef3b5;border-left:4px solid #e9b700;font-size:13px">{e(note)}</p>')
        body.append(md_html(answer or "_(empty answer)_"))
    if srcs:
        body.append('<div style="margin-top:16px;border-top:1px solid #d9e0e5;padding-top:8px;font-size:12px">'
                    '<div style="font-weight:600;margin-bottom:4px">Sources</div><ol style="margin:0;padding-left:20px">')
        for x in srcs:
            t = e(x["title"])
            body.append(f'<li><a style="{_STYLES["a"]}" href="{e(x["url"])}">{t}</a><br><span style="color:#4a5a66">{e(x["url"])}</span></li>'
                        if x["url"] else f"<li>{t}</li>")
        body.append("</ol></div>")
    links = []
    if chat_url:
        links.append(f'<a style="display:inline-block;background:#0072a3;color:#fff;text-decoration:none;padding:6px 14px;border-radius:3px;font-weight:600" href="{e(chat_url)}">Open in Ask</a>')
    if manage:
        links.append(f'<a style="{_STYLES["a"]}" href="{e(manage)}">Change or stop this schedule</a>')
    if links:
        body.append('<div style="margin-top:16px">' + " &nbsp; ".join(links) + "</div>")
    body.append('<div style="margin-top:14px;font-size:11px;color:#6b7b86">Sent by vCommunity Ask (scheduled prompt).</div></div>')
    return subject, "\n".join(text), "".join(body)


def build_chat(s: dict[str, Any], result: dict[str, Any], *, public: str, when: datetime,
               model: str, seconds: float) -> tuple[str, str]:
    """(title, markdown text) for the chat webhook: the same parts as the email, as one
    Markdown message (Discord and Slack show **bold** and links)."""
    failed = bool(result.get("error"))
    title = f"[Ask] {s['name']}: {'failed ' if failed else ''}{when.strftime('%Y-%m-%d')}"
    chat_url = f"{public.rstrip('/')}/chat/{result['chat_id']}" if public and result.get("chat_id") else ""
    answer = result.get("answer") or ""
    if not s.get("include_sources", 1):
        answer = CITATION_RE.sub("", answer)
    lines = [f"**{s['name']}** · {when.strftime('%Y-%m-%d %H:%M %Z')} · {model} · {seconds:.0f} s"]
    if failed or s.get("include_prompt", 1):
        # Every line quoted: Discord and Slack quote only the line that starts with ">".
        prompt = s["prompt"].strip()
        short = prompt[:300] + ("…" if len(prompt) > 300 else "")
        lines += [f"> {line}" if line.strip() else ">" for line in short.splitlines()]
    lines.append("")
    if failed:
        lines += ["**The scheduled prompt failed.**", str(result["error"])[:1000]]
    else:
        if (result.get("stats") or {}).get("no_final_answer"):
            lines += ["_The model used all its tool turns without writing an answer. This is what it said while it worked._", ""]
        lines.append(answer.strip() or "_(empty answer)_")
    srcs = [] if failed or not s.get("include_sources", 1) else source_lines(result.get("sources") or [], public)
    if srcs:
        lines += ["", "**Sources**"] + [f"[{x['n']}] {x['title']}" + (f" <{x['url']}>" if x["url"] else "") for x in srcs[:10]]
        if len(srcs) > 10:
            lines.append(f"… and {len(srcs) - 10} more")
    if chat_url:
        lines += ["", f"Open in Ask: <{chat_url}>"]
    return title, "\n".join(lines)


# "[1]", "[2, 3]" citations (not "[1](link)" markdown links), with the space before them.
CITATION_RE = re.compile(r"[ \t]*\[\d+(?:\s*,\s*\d+)*\](?!\()")


def answer_hash(text: str) -> str:
    """Change check for "Email only when something changed": the answer without citation
    numbers, case and spacing."""
    norm = re.sub(r"\s*\[\d+(?:\s*,\s*\d+)*\]", "", text or "").lower()
    return hashlib.sha256(" ".join(norm.split()).encode()).hexdigest()[:16]


class Notifier:
    """Sends a scheduled prompt's result through the dashboards container: email with the
    Log alerts → Notifications mail server, or a chat message with its webhook."""

    def __init__(self) -> None:
        self.url = (os.environ.get("ASK_VCO_INTERNAL_URL") or "http://vco:8080").rstrip("/")

    @staticmethod
    def token() -> str:
        return os.environ.get("ASK_VCO_TOKEN") or os.environ.get("VCO_API_TOKEN_MPBUILDER") or ""

    def send(self, to: list[str], subject: str, text: str, html_body: str,
             attachments: list[dict[str, Any]] | None = None) -> str:
        files = [{"filename": a["filename"], "content_type": a["content_type"],
                  "data_base64": base64.b64encode(a["data"]).decode()} for a in attachments or []]
        tok = self.token()
        try:
            r = requests.post(f"{self.url}/api/logs/notify/email", timeout=60,
                              headers={"Authorization": f"Bearer {tok}"} if tok else {},
                              json={"to": to, "subject": subject, "text": text, "html": html_body,
                                    "attachments": files, "sender_name": "vCommunity Ask"})
        except requests.RequestException as e:
            raise RuntimeError(f"vCommunity Observability is not reachable at {self.url} ({type(e).__name__}). "
                               "Email goes through its mail server, so the vco container must be running.") from None
        if r.status_code == 401:
            raise RuntimeError("vCommunity Observability refused the API token. Put the dashboards token in .env "
                               "as VCO_API_TOKEN_MPBUILDER (or ASK_VCO_TOKEN) and restart vco-ask.")
        if r.status_code == 404:
            raise RuntimeError("The Logs module is switched off (or too old) in vCommunity Observability; "
                               "it provides the mail server. Turn it on on the Modules page.")
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail")
            except ValueError:
                detail = r.text[:300]
            raise RuntimeError(f"Email not sent: {detail}")
        return r.json().get("result") or "email sent"

    def webhooks(self) -> list[str] | None:
        """The named webhooks on Log alerts → Notifications (names only), or None when the
        dashboards container can't be asked."""
        tok = self.token()
        try:
            r = requests.get(f"{self.url}/api/logs/notify/webhooks", timeout=3,
                             headers={"Authorization": f"Bearer {tok}"} if tok else {})
            if r.status_code != 200:
                return None
            return [str(i["name"]) for i in r.json().get("items") or [] if i.get("name") and i["name"] != "default"]
        except (requests.RequestException, ValueError, KeyError, TypeError):
            return None

    def webhook(self, title: str, text: str, *, name: str = "", color: str = "info", link: str = "") -> str:
        """Post to a webhook set on Log alerts → Notifications (name "" = the default one)."""
        tok = self.token()
        try:
            r = requests.post(f"{self.url}/api/logs/notify/webhook", timeout=90,
                              headers={"Authorization": f"Bearer {tok}"} if tok else {},
                              json={"title": title, "text": text, "username": "vCommunity Ask", "webhook": name,
                                    "color": color, "link": link})
        except requests.RequestException as e:
            raise RuntimeError(f"vCommunity Observability is not reachable at {self.url} ({type(e).__name__}). "
                               "The webhook is sent through it, so the vco container must be running.") from None
        if r.status_code == 401:
            raise RuntimeError("vCommunity Observability refused the API token. Put the dashboards token in .env "
                               "as VCO_API_TOKEN_MPBUILDER (or ASK_VCO_TOKEN) and restart vco-ask.")
        if r.status_code == 404:
            raise RuntimeError("The Logs module is switched off (or too old) in vCommunity Observability; "
                               "it holds the webhook. Turn it on on the Modules page and rebuild vco if it is old.")
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail")
            except ValueError:
                detail = r.text[:300]
            raise RuntimeError(f"Webhook not sent: {detail}")
        return r.json().get("result") or "webhook sent"


class Schedules:
    def __init__(self, db: DB, settings: Callable[[], dict[str, Any]], split_scope: Callable[[Any], tuple],
                 indexer: Any, reports: Any, scheduler: Any, notifier: Notifier | None = None):
        self.db, self.settings, self.split_scope = db, settings, split_scope
        self.indexer, self.reports, self.scheduler = indexer, reports, scheduler
        self.notifier = notifier or Notifier()
        self._locks: dict[int, threading.Lock] = {}
        self._guard = threading.Lock()
        with db.conn() as c:
            c.executescript(TABLES)
            # 2026-11-14: the prompt and the sources list in the email are options (on by default).
            have = {r[1] for r in c.execute("PRAGMA table_info(schedules)")}
            for col in ("include_prompt", "include_sources"):
                if col not in have:
                    c.execute(f"ALTER TABLE schedules ADD COLUMN {col} INTEGER NOT NULL DEFAULT 1")
            # 2026-11-15: "Don't repeat recent answers" (off by default).
            if "avoid_repeats" not in have:
                c.execute("ALTER TABLE schedules ADD COLUMN avoid_repeats INTEGER NOT NULL DEFAULT 0")
            # 2026-11-15: send by email and/or the Logs webhook (old schedules: email only).
            if "send_email" not in have:
                c.execute("ALTER TABLE schedules ADD COLUMN send_email INTEGER NOT NULL DEFAULT 1")
            if "send_webhook" not in have:
                c.execute("ALTER TABLE schedules ADD COLUMN send_webhook INTEGER NOT NULL DEFAULT 0")
            # 2026-11-16: which of the Logs webhooks gets the post ("" = the default one).
            if "webhook_name" not in have:
                c.execute("ALTER TABLE schedules ADD COLUMN webhook_name TEXT NOT NULL DEFAULT ''")
            # 2026-11-16: show the latest answer on the Apple HomeView wall display.
            if "homeview" not in have:
                c.execute("ALTER TABLE schedules ADD COLUMN homeview INTEGER NOT NULL DEFAULT 0")
            # 2026-11-23: deliver to Apple Notes on the Mac (through the host agent).
            if "send_notes" not in have:
                c.execute("ALTER TABLE schedules ADD COLUMN send_notes INTEGER NOT NULL DEFAULT 0")
            # 2026-11-16: the status "email_failed" is now "not_sent" (email or webhook).
            c.execute("UPDATE schedule_runs SET status='not_sent' WHERE status='email_failed'")

    # -- storage -------------------------------------------------------------------
    def list(self) -> list[dict[str, Any]]:
        with self.db.conn() as c:
            rows = [dict(r) for r in c.execute("SELECT * FROM schedules ORDER BY name COLLATE NOCASE")]
            for r in rows:
                last = c.execute("SELECT * FROM schedule_runs WHERE schedule=? ORDER BY id DESC LIMIT 1", (r["id"],)).fetchone()
                r["last"] = dict(last) if last else None
        for r in rows:
            r["when"] = describe(r)
            r["next"] = next_run(r)
            r["running"] = self.is_running(r["id"])
        return rows

    def get(self, sid: int) -> dict[str, Any] | None:
        with self.db.conn() as c:
            r = c.execute("SELECT * FROM schedules WHERE id=?", (sid,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["when"], d["next"], d["running"] = describe(d), next_run(d), self.is_running(sid)
        return d

    def using_source(self, name: str) -> list[dict[str, Any]]:
        """Scheduled prompts that pick this knowledge source by name (All sources does not count).
        A source they use can't be deleted until they are changed to another source."""
        with self.db.conn() as c:
            rows = c.execute("SELECT id, name, scope FROM schedules ORDER BY name COLLATE NOCASE").fetchall()
        return [{"id": r["id"], "name": r["name"]} for r in rows
                if name in [n.strip() for n in (r["scope"] or "").split(",")]]

    def save(self, body: dict[str, Any], sid: int | None = None) -> dict[str, Any]:
        s, errors = clean(body)
        if errors:
            raise ValueError(json.dumps(errors))
        now = time.time()
        cols = list(s)
        with self.db.conn() as c:
            if sid:
                if not c.execute("SELECT 1 FROM schedules WHERE id=?", (sid,)).fetchone():
                    raise KeyError(sid)
                c.execute(f"UPDATE schedules SET {', '.join(f'{k}=?' for k in cols)}, updated=? WHERE id=?",
                          [s[k] for k in cols] + [now, sid])
            else:
                sid = c.execute(f"INSERT INTO schedules({', '.join(cols)}, created, updated) VALUES({', '.join('?' * len(cols))}, ?, ?)",
                                [s[k] for k in cols] + [now, now]).lastrowid
        self.register(sid)
        return self.get(sid)  # type: ignore[return-value]

    def set_enabled(self, sid: int, on: bool) -> dict[str, Any]:
        with self.db.conn() as c:
            if not c.execute("UPDATE schedules SET enabled=?, updated=? WHERE id=?", (1 if on else 0, time.time(), sid)).rowcount:
                raise KeyError(sid)
        self.register(sid)
        return self.get(sid)  # type: ignore[return-value]

    def delete(self, sid: int) -> bool:
        with self.db.conn() as c:
            n = c.execute("DELETE FROM schedules WHERE id=?", (sid,)).rowcount
        self._unschedule(sid)
        return bool(n)

    def homeview(self, limit_chars: int = 2400) -> list[dict[str, Any]]:
        """The latest good answer of each scheduled prompt with "Show on the HomeView" on, for
        the Apple HomeView wall display in vCommunity Observability (GET /api/homeview)."""
        out = []
        with self.db.conn() as c:
            items = [dict(r) for r in c.execute("SELECT id, name FROM schedules WHERE homeview = 1 ORDER BY name COLLATE NOCASE")]
            for s in items:
                run = c.execute("SELECT finished, chat_id, status FROM schedule_runs WHERE schedule=? AND chat_id != '' "
                                "AND status IN ('ok','unchanged','not_sent') ORDER BY id DESC LIMIT 1", (s["id"],)).fetchone()
                if not run:
                    out.append({"id": s["id"], "name": s["name"], "answer": "", "finished": None, "chat": ""})
                    continue
                msg = c.execute("SELECT content FROM messages WHERE chat=? AND role='assistant' ORDER BY id DESC LIMIT 1",
                                (run["chat_id"],)).fetchone()
                text = CITATION_RE.sub("", (msg["content"] if msg else "") or "").strip()
                out.append({"id": s["id"], "name": s["name"], "finished": run["finished"], "chat": f"/chat/{run['chat_id']}",
                            "answer": text[:limit_chars] + ("…" if len(text) > limit_chars else "")})
        return out

    def runs(self, sid: int, limit: int = SHOW_RUNS) -> list[dict[str, Any]]:
        with self.db.conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM schedule_runs WHERE schedule=? ORDER BY id DESC LIMIT ?",
                                               (sid, limit))]

    # -- scheduler -----------------------------------------------------------------
    def register_all(self) -> None:
        with self.db.conn() as c:
            ids = [r[0] for r in c.execute("SELECT id FROM schedules")]
        for sid in ids:
            self.register(sid)

    def _unschedule(self, sid: int) -> None:
        if self.scheduler is None:
            return
        try:
            self.scheduler.remove_job(f"schedule-{sid}")
        except Exception:  # noqa: BLE001 - JobLookupError: not scheduled
            pass

    def register(self, sid: int) -> None:
        if self.scheduler is None:
            return
        s = self.get(sid)
        if not s or not s["enabled"]:
            self._unschedule(sid)
            return
        try:
            # A run missed while the container was down still runs if it is less than an hour late.
            self.scheduler.add_job(self.run, trigger(s), args=[sid, "schedule"], id=f"schedule-{sid}",
                                   replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=3600)
        except Exception:  # noqa: BLE001
            log.exception("schedule %s could not be registered", sid)

    # -- running -------------------------------------------------------------------
    def is_running(self, sid: int) -> bool:
        lock = self._locks.get(sid)
        return bool(lock and lock.locked())

    def start(self, sid: int) -> bool:
        """Run now, in the background. False when it is already running."""
        if not self.get(sid):
            raise KeyError(sid)
        if self.is_running(sid):
            return False
        threading.Thread(target=self.run, args=(sid, "manual"), daemon=True, name=f"schedule-{sid}").start()
        return True

    def _wait_for_syncs(self, names: list[str]) -> None:
        started = [n for n in names if self.indexer.start(n)]
        deadline = time.time() + SYNC_WAIT_S
        time.sleep(0.5)
        while time.time() < deadline and any(self.indexer.is_running(n) for n in names):
            time.sleep(2)
        if started:
            log.info("scheduled prompt: synced %s first", ", ".join(started))

    def run(self, sid: int, trigger_name: str = "schedule") -> dict[str, Any] | None:
        with self._guard:
            lock = self._locks.setdefault(sid, threading.Lock())
        if not lock.acquire(blocking=False):
            log.info("schedule %s is already running; this run is skipped", sid)
            return None
        try:
            s = self.get(sid)
            if not s:
                return None
            if not control.is_on("ask"):
                if trigger_name == "schedule":
                    return None        # Settings → Apps on / off: Ask is paused
            return self._run(s, trigger_name)
        finally:
            lock.release()

    def _run(self, s: dict[str, Any], trigger_name: str) -> dict[str, Any]:
        t0 = time.time()
        with self.db.conn() as c:
            rid = c.execute("INSERT INTO schedule_runs(schedule, started, trigger) VALUES(?,?,?)",
                            (s["id"], t0, trigger_name)).lastrowid
        cfg = dict(self.settings())
        if s["model"]:
            cfg["chat_model"] = s["model"]
        public = public_base(cfg)
        when = datetime.now(ZoneInfo(s["timezone"]))
        status, chat_id, err, email, digest = "ok", "", "", "", ""
        result: dict[str, Any] = {}
        try:
            if not control.is_on("ask"):
                raise RuntimeError("Ask is switched off (Settings, Apps on / off).")
            docs, tools = self.split_scope(s["scope"])
            if s["sync_first"] and docs:
                self._wait_for_syncs([d for d in docs if d != "-"])
            chat_id = self.db.new_chat(f"Scheduled: {s['name']} · {when.strftime('%Y-%m-%d %H:%M')}", s["scope"])
            question = s["prompt"]
            if s.get("avoid_repeats"):
                question += self.earlier_answers_note(s["id"])
            result = rag.answer(self.db, cfg, chat_id, question, docs, tools)
            if result.get("error"):
                status, err = "error", str(result["error"])
            else:
                digest = answer_hash(result.get("answer") or "")
        except Exception as e:  # noqa: BLE001 - every failure is recorded and emailed
            log.exception("scheduled prompt %s failed", s["id"])
            status, err = "error", str(e)[:500]
            result = {"chat_id": chat_id, "answer": "", "sources": [], "error": err}
        result.setdefault("chat_id", chat_id)
        seconds = time.time() - t0

        skip = False
        if status == "ok" and s["only_changed"]:
            with self.db.conn() as c:
                prev = c.execute("SELECT answer_hash FROM schedule_runs WHERE schedule=? AND status IN ('ok','unchanged') "
                                 "AND answer_hash != '' AND id < ? ORDER BY id DESC LIMIT 1", (s["id"], rid)).fetchone()
            if prev and prev[0] == digest:
                skip, status, email = True, "unchanged", "not sent: the answer has not changed"
        if not skip:
            sent, failed_any = [], False
            if s.get("send_email", 1):
                subject, text, html_body = build_email(s, result, public=public, when=when, model=cfg["chat_model"], seconds=seconds)
                files = self._attachments(result) if s["attach_report"] and status == "ok" else []
                try:
                    sent.append(self.notifier.send(recipients(s["recipients"]), subject, text, html_body, files))
                except Exception as e:  # noqa: BLE001
                    sent.append(f"email not sent: {e}" if s.get("send_webhook") else f"not sent: {e}")
                    failed_any = True
            if s.get("send_webhook"):
                title, chat_text = build_chat(s, result, public=public, when=when, model=cfg["chat_model"], seconds=seconds)
                link = f"{public.rstrip('/')}/chat/{result['chat_id']}" if public and result.get("chat_id") else ""
                try:
                    sent.append(self.notifier.webhook(title, chat_text, name=s.get("webhook_name") or "",
                                                      color="fail" if status == "error" else "ok", link=link))
                except Exception as e:  # noqa: BLE001
                    sent.append(f"webhook not sent: {e}")
                    failed_any = True
            if s.get("send_notes"):
                title, note_text = build_chat(s, result, public=public, when=when, model=cfg["chat_model"], seconds=seconds)
                link = f"{public.rstrip('/')}/chat/{result['chat_id']}" if public and result.get("chat_id") else ""
                note_text = note_text.replace("Open in Ask: <" + link + ">", "").rstrip() if link else note_text
                try:
                    sent.append(applenotes.send(cfg.get("agent_url") or "", title.replace("[Ask] ", ""), note_text,
                                                cfg.get("notes_folder") or "", link, "Scheduled prompt, vCommunity Ask"))
                except Exception as e:  # noqa: BLE001
                    sent.append(f"Apple Notes not saved: {e}")
                    failed_any = True
            email = "; ".join(sent)
            if failed_any and status == "ok":
                status = "not_sent"
        with self.db.conn() as c:
            c.execute("UPDATE schedule_runs SET finished=?, status=?, chat_id=?, email=?, error=?, answer_hash=?, seconds=? WHERE id=?",
                      (time.time(), status, chat_id, email[:500], err[:1000], digest, round(seconds, 1), rid))
            # Keep the newest KEEP_RUNS rows per schedule.
            c.execute("DELETE FROM schedule_runs WHERE schedule=? AND id NOT IN "
                      "(SELECT id FROM schedule_runs WHERE schedule=? ORDER BY id DESC LIMIT ?)", (s["id"], s["id"], KEEP_RUNS))
        log.info("scheduled prompt %s (%s): %s, %s", s["id"], s["name"], status, email)
        return {"id": rid, "status": status, "chat_id": chat_id, "email": email, "error": err, "seconds": round(seconds, 1)}

    def earlier_answers(self, sid: int, limit: int = AVOID_RUNS) -> list[str]:
        """The answers of this schedule's last `limit` good runs, newest first (chats the user
        deleted are skipped). Each is the last assistant message of the run's chat."""
        out: list[str] = []
        with self.db.conn() as c:
            runs = c.execute("SELECT chat_id FROM schedule_runs WHERE schedule=? AND chat_id != '' "
                             "AND status IN ('ok','unchanged','not_sent') ORDER BY id DESC LIMIT ?",
                             (sid, limit)).fetchall()
            for (cid,) in runs:
                m = c.execute("SELECT content FROM messages WHERE chat=? AND role='assistant' ORDER BY id DESC LIMIT 1",
                              (cid,)).fetchone()
                text = re.sub(r"\s+", " ", (m[0] if m else "") or "").strip()
                if text:
                    out.append(text if len(text) <= AVOID_EACH_CHARS else text[:AVOID_EACH_CHARS].rstrip() + " ...")
        return out

    def earlier_answers_note(self, sid: int) -> str:
        """Text added under the prompt when "Don't repeat recent answers" is on."""
        prev = self.earlier_answers(sid)
        if not prev:
            return ""
        lines = "\n".join(f"{i}. {t}" for i, t in enumerate(prev, 1))
        return ("\n\n---\nThis prompt runs on a schedule. These are the answers it gave on earlier runs, newest first. "
                "Do NOT repeat any of them: give a different answer (for example a different quote, author, "
                "topic or item), still following the instructions above.\n" + lines)

    def _attachments(self, result: dict[str, Any]) -> list[dict[str, Any]]:
        out, total = [], 0
        for c in result.get("sources") or []:
            rep = c.get("report") or {}
            p = self.reports.path(rep.get("id") or "") if self.reports else None
            if not p:
                continue
            data = p.read_bytes()
            if total + len(data) > MAX_ATTACH_BYTES:
                break
            total += len(data)
            name = re.sub(r"[^\w -]+", "", rep.get("title") or "report").strip()[:60] or "report"
            out.append({"filename": f"{name}.html", "content_type": "text/html", "data": data})
        return out
