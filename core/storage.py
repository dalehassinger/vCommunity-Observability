"""SQLite storage. Each module gets its own database file, so modules are
isolated from each other and can be removed by deleting one file. The core
keeps its own run history in _core.db."""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .modules import COLUMN_TYPES, Module

_lock = threading.Lock()


def utcnow() -> str:
    # SQLite-friendly and sortable; matches datetime('now') format.
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class Storage:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with self._core() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    module TEXT, collector TEXT, started_at TEXT, finished_at TEXT,
                    status TEXT, rows INTEGER, message TEXT)"""
            )
            # On/off switches set from the UI or API; they win over config.yaml.
            db.execute("CREATE TABLE IF NOT EXISTS module_state (module TEXT PRIMARY KEY, enabled INTEGER, changed_at TEXT)")
            # Menu order chosen in the UI or API.
            db.execute("CREATE TABLE IF NOT EXISTS module_order (module TEXT PRIMARY KEY, position INTEGER)")
            # Connection details and collector intervals edited on the Settings page.
            # They win over config.yaml/.env. Stored in this file on the data volume.
            db.execute("CREATE TABLE IF NOT EXISTS connection_settings (connection TEXT, key TEXT, value TEXT, "
                       "changed_at TEXT, PRIMARY KEY (connection, key))")
            # Exports written to a folder on a schedule (Settings page).
            db.execute("CREATE TABLE IF NOT EXISTS export_schedules (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                       "module TEXT, export TEXT, params TEXT, folder TEXT, frequency TEXT, weekday TEXT, "
                       "monthday TEXT, at_time TEXT, timezone TEXT, keep INTEGER, enabled INTEGER DEFAULT 1, "
                       "created_at TEXT, last_run TEXT, last_status TEXT, last_message TEXT, last_file TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS collector_settings (module TEXT, collector TEXT, "
                       "interval_minutes INTEGER, changed_at TEXT, PRIMARY KEY (module, collector))")

    # -- connections -------------------------------------------------------
    def _core(self) -> sqlite3.Connection:
        return sqlite3.connect(self.data_dir / "_core.db")

    def module_db_path(self, module: str) -> Path:
        return self.data_dir / f"{module}.db"

    def readonly(self, module: str) -> sqlite3.Connection:
        """Read-only connection used for every manifest query (API + dashboards)."""
        path = self.module_db_path(module)
        if not path.exists():
            sqlite3.connect(path).close()
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        return db

    # -- schema ------------------------------------------------------------
    def ensure_schema(self, mod: Module) -> None:
        with sqlite3.connect(self.module_db_path(mod.name)) as db:
            for tname, tdef in mod.tables.items():
                # Identifiers are quoted so a column may be named after an SQL
                # keyword (limit, order, key...) without breaking the schema.
                cols = ", ".join(
                    f'"{c}" {COLUMN_TYPES[t]}' for c, t in tdef["columns"].items()
                )
                db.execute(f'CREATE TABLE IF NOT EXISTS "{tname}" ({cols}, collected_at TEXT)')
                # Additive migrations: new columns in a module update are added.
                existing = {r[1] for r in db.execute(f'PRAGMA table_info("{tname}")')}
                for c, t in tdef["columns"].items():
                    if c not in existing:
                        db.execute(f'ALTER TABLE "{tname}" ADD COLUMN "{c}" {COLUMN_TYPES[t]}')

    # -- writes ------------------------------------------------------------
    def write_rows(self, mod: Module, table: str, rows: list[dict[str, Any]]) -> int:
        tdef = mod.tables[table]
        cols = list(tdef["columns"].keys())
        stamp = utcnow()
        values = [tuple(r.get(c) for c in cols) + (stamp,) for r in rows]
        placeholders = ", ".join("?" for _ in range(len(cols) + 1))
        quoted = ", ".join('"' + c + '"' for c in cols)
        with _lock, sqlite3.connect(self.module_db_path(mod.name)) as db:
            if tdef["mode"] == "snapshot":
                db.execute(f'DELETE FROM "{table}"')
            else:
                days = int(tdef.get("retention_days", 90))
                cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                db.execute(f'DELETE FROM "{table}" WHERE collected_at < ?', (cutoff,))
            db.executemany(
                f'INSERT INTO "{table}" ({quoted}, collected_at) VALUES ({placeholders})',
                values,
            )
        return len(values)

    # -- run history -------------------------------------------------------
    def record_run(self, module: str, collector: str, started: str, status: str,
                   rows: int, message: str) -> None:
        with _lock, self._core() as db:
            db.execute(
                "INSERT INTO runs (module, collector, started_at, finished_at, status, rows, message)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (module, collector, started, utcnow(), status, rows, message[:2000]),
            )
            db.execute("DELETE FROM runs WHERE id NOT IN (SELECT id FROM runs ORDER BY id DESC LIMIT 1000)")

    def module_states(self) -> dict[str, bool]:
        with self._core() as db:
            return {m: bool(e) for m, e in db.execute("SELECT module, enabled FROM module_state")}

    def set_module_state(self, module: str, enabled: bool) -> None:
        with _lock, self._core() as db:
            db.execute(
                "INSERT INTO module_state (module, enabled, changed_at) VALUES (?, ?, ?) "
                "ON CONFLICT(module) DO UPDATE SET enabled = excluded.enabled, changed_at = excluded.changed_at",
                (module, int(enabled), utcnow()),
            )

    def module_order(self) -> list[str]:
        with self._core() as db:
            return [m for (m,) in db.execute("SELECT module FROM module_order ORDER BY position")]

    def set_module_order(self, names: list[str]) -> None:
        with _lock, self._core() as db:
            db.execute("DELETE FROM module_order")
            db.executemany("INSERT INTO module_order (module, position) VALUES (?, ?)",
                           [(n, i) for i, n in enumerate(names)])

    def connection_settings(self) -> dict[str, dict[str, str]]:
        out: dict[str, dict[str, str]] = {}
        with self._core() as db:
            for conn, key, value in db.execute("SELECT connection, key, value FROM connection_settings"):
                out.setdefault(conn, {})[key] = value
        return out

    def set_connection_settings(self, connection: str, values: dict[str, str]) -> None:
        with _lock, self._core() as db:
            db.executemany(
                "INSERT INTO connection_settings (connection, key, value, changed_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(connection, key) DO UPDATE SET value = excluded.value, changed_at = excluded.changed_at",
                [(connection, k, v, utcnow()) for k, v in values.items()],
            )

    def reset_connection_settings(self, connection: str) -> None:
        with _lock, self._core() as db:
            db.execute("DELETE FROM connection_settings WHERE connection = ?", (connection,))

    def collector_intervals(self) -> dict[tuple[str, str], int]:
        with self._core() as db:
            return {(m, c): int(i) for m, c, i in db.execute(
                "SELECT module, collector, interval_minutes FROM collector_settings")}

    def set_collector_interval(self, module: str, collector: str, minutes: int) -> None:
        with _lock, self._core() as db:
            db.execute(
                "INSERT INTO collector_settings (module, collector, interval_minutes, changed_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(module, collector) DO UPDATE SET interval_minutes = excluded.interval_minutes, "
                "changed_at = excluded.changed_at",
                (module, collector, minutes, utcnow()),
            )

    def export_schedules(self) -> list[dict[str, Any]]:
        with self._core() as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute("SELECT * FROM export_schedules ORDER BY id")]

    def add_export_schedule(self, values: dict[str, Any]) -> int:
        cols = ["module", "export", "params", "folder", "frequency", "weekday", "monthday", "at_time", "timezone", "keep"]
        with _lock, self._core() as db:
            cur = db.execute(
                f"INSERT INTO export_schedules ({', '.join(cols)}, enabled, created_at) VALUES "
                f"({', '.join('?' for _ in cols)}, 1, ?)", [values.get(c) for c in cols] + [utcnow()])
            return int(cur.lastrowid)

    def update_export_schedule(self, sid: int, values: dict[str, Any]) -> None:
        cols = ["module", "export", "params", "folder", "frequency", "weekday", "monthday", "at_time", "timezone", "keep"]
        with _lock, self._core() as db:
            db.execute(f"UPDATE export_schedules SET {', '.join(c + ' = ?' for c in cols)} WHERE id = ?",
                       [values.get(c) for c in cols] + [sid])

    def delete_export_schedule(self, sid: int) -> None:
        with _lock, self._core() as db:
            db.execute("DELETE FROM export_schedules WHERE id = ?", (sid,))

    def set_export_schedule_enabled(self, sid: int, enabled: bool) -> None:
        with _lock, self._core() as db:
            db.execute("UPDATE export_schedules SET enabled = ? WHERE id = ?", (int(enabled), sid))

    def record_export_run(self, sid: int, status: str, message: str, file: str | None) -> None:
        with _lock, self._core() as db:
            db.execute("UPDATE export_schedules SET last_run = ?, last_status = ?, last_message = ?, last_file = ? "
                       "WHERE id = ?", (utcnow(), status, message[:1000], file, sid))

    def last_runs(self) -> dict[tuple[str, str], dict[str, Any]]:
        with self._core() as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT * FROM runs WHERE id IN (SELECT MAX(id) FROM runs GROUP BY module, collector)"
            ).fetchall()
        return {(r["module"], r["collector"]): dict(r) for r in rows}

    def query(self, module: str, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        with self.readonly(module) as db:
            return [dict(r) for r in db.execute(sql, params or {}).fetchall()]
