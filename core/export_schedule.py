"""Scheduled exports: run a module export (e.g. DBH Insights XLSX) daily, weekly
or monthly and write the file to a folder, keeping the newest N files."""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, available_timezones

from apscheduler.triggers.cron import CronTrigger

log = logging.getLogger("vco.exports")

WEEKDAYS = [("mon", "Monday"), ("tue", "Tuesday"), ("wed", "Wednesday"), ("thu", "Thursday"),
            ("fri", "Friday"), ("sat", "Saturday"), ("sun", "Sunday")]
FREQUENCIES = {"daily": "Daily", "weekly": "Weekly", "monthly": "Monthly"}
TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def exports_root() -> Path:
    """Where relative folders live. Docker Compose mounts ./exports here."""
    return Path(os.environ.get("VCO_EXPORT_DIR", "exports")).resolve()


def resolve_folder(folder: str) -> Path:
    folder = (folder or "").strip() or "."
    path = Path(folder)
    return (path if path.is_absolute() else exports_root() / path).resolve()


def browse(rel: str) -> dict[str, Any]:
    """Sub-folders of one folder under the exports root, for the folder picker.
    Paths are relative to the root; anything that escapes it is refused."""
    root = exports_root()
    root.mkdir(parents=True, exist_ok=True)
    target = (root / (rel or "").strip().strip("/")).resolve()
    if target != root and root not in target.parents:
        raise ValueError("That folder is outside the exports folder.")
    if not target.is_dir():
        raise ValueError("That folder doesn't exist.")
    dirs = sorted((p.name for p in target.iterdir() if p.is_dir() and not p.name.startswith(".")), key=str.lower)
    here = target.relative_to(root).as_posix()
    here = "" if here == "." else here
    parent = None if not here else Path(here).parent.as_posix()
    return {"root": str(root), "path": here, "parent": "" if parent == "." else parent,
            "dirs": dirs, "writable": os.access(target, os.W_OK)}


NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$")


def make_folder(rel: str, name: str) -> str:
    """Create `name` inside `rel` (under the exports root); returns the new relative path."""
    name = (name or "").strip()
    if not NAME.match(name) or ".." in name:
        raise ValueError("Use letters, numbers, spaces, dots, dashes or underscores for the folder name.")
    parent = browse(rel)["path"]
    new_rel = f"{parent}/{name}" if parent else name
    try:
        (exports_root() / new_rel).mkdir(exist_ok=True)
    except OSError as exc:
        raise ValueError(f"Can't create the folder: {exc.strerror or exc}") from exc
    return new_rel


def default_timezone() -> str:
    tz = os.environ.get("TZ", "")
    return tz if tz in timezone_names() else "UTC"


_TZ_CACHE: list[str] = []


def timezone_names() -> list[str]:
    if not _TZ_CACHE:
        _TZ_CACHE.extend(sorted(n for n in available_timezones() if "/" in n or n == "UTC"))
    return _TZ_CACHE


def validate(values: dict[str, str]) -> str | None:
    """Return an error message, or None when the schedule is valid."""
    if values.get("frequency") not in FREQUENCIES:
        return "Pick daily, weekly or monthly."
    if not TIME.match(values.get("at_time", "")):
        return "Time must be HH:MM (24-hour), for example 06:30."
    if values["frequency"] == "weekly" and values.get("weekday") not in dict(WEEKDAYS):
        return "Pick a day of the week."
    if values["frequency"] == "monthly":
        day = values.get("monthday", "")
        if day != "last" and not (day.isdigit() and 1 <= int(day) <= 28):
            return "Day of month must be 1 to 28, or the last day."
    if values.get("timezone") not in timezone_names():
        return f"Unknown time zone '{values.get('timezone')}'."
    try:
        keep = int(values.get("keep") or 0)
    except ValueError:
        return "Keep must be a whole number (0 keeps every file)."
    if keep < 0:
        return "Keep must be 0 or more."
    folder = resolve_folder(values.get("folder", ""))
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / ".vco-write-test"
        probe.write_text("ok")
        probe.unlink()
    except OSError as exc:
        return f"Can't write to {folder}: {exc.strerror or exc}. In Docker, use a folder under /app/exports (./exports on the host)."
    return None


def trigger(s: dict[str, Any]) -> CronTrigger:
    hour, minute = (int(x) for x in s["at_time"].split(":"))
    kw: dict[str, Any] = {"hour": hour, "minute": minute, "timezone": ZoneInfo(s["timezone"])}
    if s["frequency"] == "weekly":
        kw["day_of_week"] = s["weekday"]
    elif s["frequency"] == "monthly":
        kw["day"] = s["monthday"]
    return CronTrigger(**kw)


def describe(s: dict[str, Any]) -> str:
    at = f"at {s['at_time']} {s['timezone']}"
    if s["frequency"] == "weekly":
        return f"Every {dict(WEEKDAYS).get(s['weekday'], s['weekday'])} {at}"
    if s["frequency"] == "monthly":
        day = "last day" if s["monthday"] == "last" else f"day {s['monthday']}"
        return f"Monthly on the {day} {at}"
    return f"Daily {at}"


def next_run(s: dict[str, Any]) -> str | None:
    try:
        nxt = trigger(s).get_next_fire_time(None, datetime.now(ZoneInfo(s["timezone"])))
    except Exception:
        return None
    return nxt.strftime("%Y-%m-%d %H:%M %Z") if nxt else None


def filename_for(template: str, params: dict[str, str], tz: str) -> str:
    stamp = datetime.now(ZoneInfo(tz)).strftime("%Y%m%d-%H%M%S")
    fields = {"timestamp": stamp, **{k: re.sub(r"[^A-Za-z0-9._-]+", "_", v)[:60] for k, v in params.items()}}
    try:
        return template.format(**fields)
    except (KeyError, IndexError, ValueError):
        return f"export-{stamp}"


def prune(folder: Path, template: str, params: dict[str, str], keep: int) -> list[str]:
    """Delete the oldest files made by this schedule beyond `keep` (0 = keep all)."""
    if keep <= 0:
        return []
    fields = {"timestamp": "*", **{k: re.sub(r"[^A-Za-z0-9._-]+", "_", v)[:60] for k, v in params.items()}}
    try:
        pattern = template.format(**fields)
    except (KeyError, IndexError, ValueError):
        return []
    files = sorted((p for p in folder.glob(pattern) if p.is_file()), key=lambda p: p.stat().st_mtime, reverse=True)
    removed = []
    for old in files[keep:]:
        try:
            old.unlink()
            removed.append(old.name)
        except OSError as exc:
            log.warning("could not delete old export %s: %s", old, exc)
    return removed
