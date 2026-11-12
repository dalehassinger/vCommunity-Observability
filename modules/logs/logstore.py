"""Shared by the Logs module's syslog service and its web extension: the database
(data/syslog.db), the settings, the search language and the syslog parser.

The listener is the only writer of log rows. The web side reads them, and writes only
settings and alert rules (small rows), so the two never fight over the database.

Search language (the Logs page search box and alert rules):

    error disk                 both words in the message, app or host (full-text)
    "lost access"              exact phrase
    vmk*                       word prefix
    -debug                     leave out messages with the word
    host:esx9-01*              host name (* = any text), case-insensitive
    ip:192.168.1.*             sender IP address
    app:vpxa  procid:1234  msgid:esx
    sev:err                    severity err or worse (emerg, alert, crit, err)
    -host:esx9-04*             a minus works on fields too
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SEVERITIES = ["emerg", "alert", "crit", "err", "warning", "notice", "info", "debug"]
SEV_ALIASES = {"emergency": 0, "panic": 0, "critical": 2, "error": 3, "warn": 4, "informational": 6}
FACILITIES = ["kern", "user", "mail", "daemon", "auth", "syslog", "lpr", "news", "uucp", "cron", "authpriv",
              "ftp", "ntp", "security", "console", "solaris-cron", "local0", "local1", "local2", "local3",
              "local4", "local5", "local6", "local7"]

DEFAULTS: dict[str, Any] = {
    "retention_days": 5,          # Dale's choice, changeable on the Log settings page
    "max_size_gb": 5,
    "alerts_enabled": True,
    "smtp_host": "", "smtp_port": 25, "smtp_security": "none",  # none | starttls | ssl
    "smtp_user": "", "smtp_password": "", "smtp_from": "", "smtp_to": "",
    "webhook_url": "",
    "silent_minutes": 15,         # a sender is "quiet" after this many minutes
}
SECRET_KEYS = {"smtp_password"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS logs (
  id INTEGER PRIMARY KEY,
  ts REAL NOT NULL,            -- when the server received it (epoch seconds, UTC)
  host TEXT, ip TEXT, facility INTEGER, severity INTEGER,
  app TEXT, procid TEXT, msgid TEXT,
  msg_time TEXT,               -- the time stamp inside the message, as sent
  message TEXT, sd TEXT,       -- sd = RFC 5424 structured data as JSON
  proto TEXT                   -- udp | tcp
);
CREATE INDEX IF NOT EXISTS logs_ts ON logs(ts);
CREATE INDEX IF NOT EXISTS logs_host_ts ON logs(host, ts);
CREATE INDEX IF NOT EXISTS logs_sev_ts ON logs(severity, ts);
CREATE VIRTUAL TABLE IF NOT EXISTS logs_fts USING fts5(
  message, app, host, content='logs', content_rowid='id', tokenize='unicode61 remove_diacritics 2');
CREATE TRIGGER IF NOT EXISTS logs_ai AFTER INSERT ON logs BEGIN
  INSERT INTO logs_fts(rowid, message, app, host) VALUES (new.id, new.message, new.app, new.host);
END;
CREATE TRIGGER IF NOT EXISTS logs_ad AFTER DELETE ON logs BEGIN
  INSERT INTO logs_fts(logs_fts, rowid, message, app, host) VALUES ('delete', old.id, old.message, old.app, old.host);
END;
CREATE TABLE IF NOT EXISTS rates (minute INTEGER PRIMARY KEY, received INTEGER NOT NULL, dropped INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS alert_rules (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
  query TEXT NOT NULL DEFAULT '', min_severity INTEGER,       -- NULL = any severity
  threshold INTEGER NOT NULL DEFAULT 1, window_minutes INTEGER NOT NULL DEFAULT 5,
  cooldown_minutes INTEGER NOT NULL DEFAULT 30,
  notify_email INTEGER NOT NULL DEFAULT 0, notify_webhook INTEGER NOT NULL DEFAULT 0,
  created REAL, updated REAL, last_fired REAL, last_count INTEGER,
  last_id INTEGER NOT NULL DEFAULT 0     -- newest message already alerted on: never alert twice on one message
);
CREATE TABLE IF NOT EXISTS alert_events (
  id INTEGER PRIMARY KEY, rule_id INTEGER, rule_name TEXT, fired_at REAL NOT NULL,
  count INTEGER, window_minutes INTEGER, query TEXT, sample TEXT, notified TEXT,
  acked INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS alert_events_fired ON alert_events(fired_at);
"""


def db_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "syslog.db"


def connect(data_dir: str | Path, readonly: bool = False) -> sqlite3.Connection:
    path = db_path(data_dir)
    if readonly and path.exists():
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10, check_same_thread=False)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        new = not path.exists()
        con = sqlite3.connect(path, timeout=10, check_same_thread=False)
        if new:
            # Must be set before the first table: lets the size cap give space back to the disk.
            con.execute("PRAGMA auto_vacuum=INCREMENTAL")
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.executescript(SCHEMA)
        _migrate(con)
    con.row_factory = sqlite3.Row
    return con


def _migrate(con: sqlite3.Connection) -> None:
    """Columns added after the first release (CREATE TABLE IF NOT EXISTS keeps old tables as they were)."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(alert_rules)")}
    if "last_id" not in cols:
        con.execute("ALTER TABLE alert_rules ADD COLUMN last_id INTEGER NOT NULL DEFAULT 0")
        # Existing rules: start after what is already stored, so an upgrade sends no burst of old alerts.
        con.execute("UPDATE alert_rules SET last_id = COALESCE((SELECT MAX(id) FROM logs), 0)")
        con.commit()


# -- settings ------------------------------------------------------------------------
def load_settings(con: sqlite3.Connection) -> dict[str, Any]:
    out = dict(DEFAULTS)
    try:
        for row in con.execute("SELECT key, value FROM settings"):
            if row["key"] in DEFAULTS:
                try:
                    out[row["key"]] = json.loads(row["value"])
                except (TypeError, ValueError):
                    pass
    except sqlite3.OperationalError:
        pass
    return out


def save_settings(con: sqlite3.Connection, values: dict[str, Any]) -> None:
    with con:
        for k, v in values.items():
            if k in DEFAULTS:
                con.execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (k, json.dumps(v)))


def set_state(con: sqlite3.Connection, value: dict[str, Any]) -> None:
    """Listener status (ports, counters), written by the service for the web pages."""
    with con:
        con.execute("INSERT INTO settings(key, value) VALUES ('_state', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (json.dumps(value),))


def get_state(con: sqlite3.Connection) -> dict[str, Any]:
    try:
        row = con.execute("SELECT value FROM settings WHERE key='_state'").fetchone()
        return json.loads(row["value"]) if row else {}
    except (sqlite3.OperationalError, ValueError):
        return {}


# -- search ----------------------------------------------------------------------------
TOKEN = re.compile(r'(-?)(?:(host|ip|app|procid|msgid|sev|severity|facility):)?("(?:[^"\\]|\\.)*"?|\S+)', re.I)


def severity_number(value: str | int | None) -> int | None:
    if value is None or value == "":
        return None
    v = str(value).strip().lower()
    if v.isdigit() and 0 <= int(v) <= 7:
        return int(v)
    if v in SEVERITIES:
        return SEVERITIES.index(v)
    if v in SEV_ALIASES:
        return SEV_ALIASES[v]
    raise ValueError(f"unknown severity '{value}' (use {', '.join(SEVERITIES)})")


def _like(value: str) -> str:
    v = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return v.replace("*", "%")


def _fts_term(word: str, phrase: bool) -> str:
    w = word.replace('"', '""')
    if not phrase and w.endswith("*") and len(w) > 1:
        return f'"{w[:-1]}"*'
    return f'"{w}"'


def build_where(query: str = "", since: float | None = None, until: float | None = None,
                min_severity: int | None = None, host: str | None = None,
                app: str | None = None) -> tuple[str, list[Any]]:
    """SQL WHERE clause (without the word WHERE) and its parameters for a search."""
    clauses: list[str] = []
    params: list[Any] = []
    must: list[str] = []
    must_not: list[str] = []
    for neg, field, value in TOKEN.findall(query or ""):
        phrase = value.startswith('"')
        if phrase:
            value = value.strip('"').replace('\\"', '"')
        if not value:
            continue
        field = (field or "").lower()
        if not field:
            words = [value] if phrase else [w for w in re.split(r"[^\w*]+", value) if w.strip("*")]
            if not words:
                continue
            term = " ".join(_fts_term(w, phrase) for w in words) if not phrase else _fts_term(value, True)
            (must_not if neg else must).append(term if len(words) == 1 or phrase else f"({term})")
            continue
        if field in ("sev", "severity"):
            n = severity_number(value)
            clauses.append("severity > ?" if neg else "severity <= ?")
            params.append(n)
            continue
        if field == "facility":
            fv = value.lower()
            num = int(fv) if fv.isdigit() else (FACILITIES.index(fv) if fv in FACILITIES else None)
            if num is None:
                raise ValueError(f"unknown facility '{value}'")
            clauses.append("facility <> ?" if neg else "facility = ?")
            params.append(num)
            continue
        col = field
        if "*" in value:
            clauses.append(f"{'NOT ' if neg else ''}(COALESCE({col}, '') LIKE ? ESCAPE '\\')")
            params.append(_like(value))
        else:
            clauses.append(f"{'NOT ' if neg else ''}(COALESCE({col}, '') = ? COLLATE NOCASE)")
            params.append(value)
    if must:
        clauses.append("id IN (SELECT rowid FROM logs_fts WHERE logs_fts MATCH ?)")
        params.append(" AND ".join(must))
    for term in must_not:
        clauses.append("id NOT IN (SELECT rowid FROM logs_fts WHERE logs_fts MATCH ?)")
        params.append(term)
    if since is not None:
        clauses.append("ts >= ?")
        params.append(since)
    if until is not None:
        clauses.append("ts < ?")
        params.append(until)
    if min_severity is not None:
        clauses.append("severity <= ?")
        params.append(min_severity)
    if host:
        clauses.append("host = ? COLLATE NOCASE")
        params.append(host)
    if app:
        clauses.append("app = ? COLLATE NOCASE")
        params.append(app)
    return (" AND ".join(clauses) or "1=1"), params


def row_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    sev = d.get("severity")
    d["severity_name"] = SEVERITIES[sev] if isinstance(sev, int) and 0 <= sev <= 7 else None
    fac = d.get("facility")
    d["facility_name"] = FACILITIES[fac] if isinstance(fac, int) and 0 <= fac < len(FACILITIES) else None
    d["time"] = datetime.fromtimestamp(d["ts"], timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    if d.get("sd"):
        try:
            d["sd"] = json.loads(d["sd"])
        except ValueError:
            pass
    return d


# -- size ------------------------------------------------------------------------------
def size_info(con: sqlite3.Connection, data_dir: str | Path) -> dict[str, Any]:
    path = db_path(data_dir)
    files = sum(p.stat().st_size for p in (path, path.with_name(path.name + "-wal")) if p.exists())
    page_size = con.execute("PRAGMA page_size").fetchone()[0]
    pages = con.execute("PRAGMA page_count").fetchone()[0]
    free = con.execute("PRAGMA freelist_count").fetchone()[0]
    return {"file_bytes": files, "used_bytes": (pages - free) * page_size}


def disk_info(data_dir: str | Path) -> dict[str, int]:
    import shutil
    try:
        u = shutil.disk_usage(Path(data_dir))
        return {"disk_total": u.total, "disk_used": u.used, "disk_free": u.free}
    except OSError:
        return {}


def rates(con: sqlite3.Connection, now: float | None = None) -> dict[str, Any]:
    """Messages per second (last 10 s), per minute (last 60 s) and per hour (last 60 min)."""
    now = now or time.time()
    per_10s = con.execute("SELECT COUNT(*) FROM logs WHERE ts >= ?", (now - 10,)).fetchone()[0]
    per_min = con.execute("SELECT COUNT(*) FROM logs WHERE ts >= ?", (now - 60,)).fetchone()[0]
    cur = int(now // 60)
    row = con.execute("SELECT COALESCE(SUM(received),0), COALESCE(SUM(dropped),0) FROM rates WHERE minute > ?",
                      (cur - 60,)).fetchone()
    return {"per_second": round(per_10s / 10, 2), "per_minute": per_min, "per_hour": row[0], "dropped_hour": row[1]}


# -- syslog parsing --------------------------------------------------------------------
PRI = re.compile(r"^<(\d{1,3})>")
TS3164 = re.compile(r"^(?P<ts>[A-Z][a-z]{2} [ \d]\d \d\d:\d\d:\d\d(?:\.\d+)?(?: \d{4})?|\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d\S*)\s+")
HOST_TAG = re.compile(r"^(?P<host>[^\s:\[]+)\s+(?P<tag>[^\s:\[]+)(?:\[(?P<pid>[^\]]*)\])?:\s?(?P<msg>.*)$", re.S)
TAG_ONLY = re.compile(r"^(?P<tag>[^\s:\[]+)(?:\[(?P<pid>[^\]]*)\])?:\s?(?P<msg>.*)$", re.S)


def _nil(v: str) -> str | None:
    return None if v == "-" else v


def _parse_sd(s: str) -> tuple[dict[str, dict[str, str]] | None, str]:
    """RFC 5424 STRUCTURED-DATA: '-' or [id k="v" ...][...]. Returns (sd, rest)."""
    if s.startswith("-"):
        return None, s[1:].lstrip(" ")
    sd: dict[str, dict[str, str]] = {}
    i = 0
    while i < len(s) and s[i] == "[":
        j = i + 1
        while j < len(s) and s[j] not in " ]":
            j += 1
        sd_id = s[i + 1:j]
        params: dict[str, str] = {}
        while j < len(s) and s[j] == " ":
            j += 1
            k = j
            while k < len(s) and s[k] != "=":
                k += 1
            name = s[j:k]
            if k + 1 >= len(s) or s[k + 1] != '"':
                break
            k += 2
            val = []
            while k < len(s) and s[k] != '"':
                if s[k] == "\\" and k + 1 < len(s):
                    k += 1
                val.append(s[k])
                k += 1
            params[name] = "".join(val)
            j = k + 1
        if j >= len(s) or s[j] != "]":
            break
        sd[sd_id] = params
        i = j + 1
    return sd or None, s[i:].lstrip(" ")


def parse(raw: str, sender_ip: str) -> dict[str, Any]:
    """One syslog message (RFC 5424 or RFC 3164, or anything) as a row dict."""
    text = raw.rstrip("\r\n\x00")
    row: dict[str, Any] = {"ip": sender_ip, "host": None, "facility": None, "severity": None, "app": None,
                           "procid": None, "msgid": None, "msg_time": None, "sd": None}
    m = PRI.match(text)
    if m:
        pri = int(m.group(1))
        if pri <= 191:
            row["facility"], row["severity"] = pri >> 3, pri & 7
        text = text[m.end():]
    if re.match(r"^\d{1,2} ", text):
        # RFC 5424: VERSION SP TIMESTAMP SP HOSTNAME SP APP-NAME SP PROCID SP MSGID SP SD [SP MSG]
        parts = text.split(" ", 6)
        if len(parts) >= 7:
            _, ts, host, app, procid, msgid, rest = parts
            row.update(msg_time=_nil(ts), host=_nil(host), app=_nil(app), procid=_nil(procid), msgid=_nil(msgid))
            sd, msg = _parse_sd(rest)
            row["sd"] = json.dumps(sd) if sd else None
            row["message"] = msg.lstrip("\ufeff")
            row["host"] = row["host"] or sender_ip
            return row
    m = TS3164.match(text)
    if m:
        row["msg_time"] = m.group("ts")
        rest = text[m.end():]
        h = HOST_TAG.match(rest)
        if h and ("." in h.group("host") or "-" in h.group("host") or h.group("host").isalnum()):
            row.update(host=h.group("host"), app=h.group("tag"), procid=h.group("pid"), message=h.group("msg"))
        else:
            t = TAG_ONLY.match(rest)
            if t:
                row.update(app=t.group("tag"), procid=t.group("pid"), message=t.group("msg"))
            else:
                first, _, msg = rest.partition(" ")
                row.update(host=first, message=msg or rest)
    else:
        t = TAG_ONLY.match(text)
        if t and len(t.group("tag")) <= 48:
            row.update(app=t.group("tag"), procid=t.group("pid"), message=t.group("msg"))
        else:
            row["message"] = text
    row["host"] = row["host"] or sender_ip
    return row


def env_ports() -> tuple[int, int]:
    return (int(os.environ.get("VCO_SYSLOG_UDP_PORT", "5514")), int(os.environ.get("VCO_SYSLOG_TCP_PORT", "5514")))
