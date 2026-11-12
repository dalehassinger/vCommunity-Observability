"""Syslog server for the Logs module (a core service: the core keeps it running while
the module is on).

  * UDP and TCP on VCO_SYSLOG_UDP_PORT / VCO_SYSLOG_TCP_PORT (default 5514; compose maps
    the host's port 514 to it). TCP takes newline-delimited and octet-counted frames
    (RFC 6587; ESXi uses octet counting).
  * Messages are parsed (RFC 5424, RFC 3164 or plain text) and written in batches, every
    second or every 1,000 messages, to data/syslog.db.
  * Housekeeping: retention (days) and the size cap (GB) from the Log settings page, oldest
    rows first, space given back to the disk.
  * Alerts: every 30 seconds each enabled rule counts its matches in its window; at the
    threshold it records an alert and sends email / webhook (cooldown between repeats).

Standard library only.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import sqlite3
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import logstore  # noqa: E402
import notify    # noqa: E402

DATA_DIR = Path(os.environ.get("VCO_DATA_DIR", "data"))
MAX_FRAME = 64 * 1024
MAX_QUEUE = 200_000
BATCH = 1000
COLS = ("ts", "host", "ip", "facility", "severity", "app", "procid", "msgid", "msg_time", "message", "sd", "proto")


def log(msg: str) -> None:
    print(msg, flush=True)


class Server:
    def __init__(self) -> None:
        self.queue: list[tuple] = []
        self.minute_counts: Counter[int] = Counter()
        self.minute_dropped: Counter[int] = Counter()
        self.received = 0
        self.dropped = 0
        self.started = time.time()
        self.db = ThreadPoolExecutor(max_workers=1, thread_name_prefix="db")   # the one writer
        self.mail = ThreadPoolExecutor(max_workers=2, thread_name_prefix="notify")
        self.con: sqlite3.Connection | None = None
        self.settings = dict(logstore.DEFAULTS)
        self.udp_port, self.tcp_port = logstore.env_ports()
        self.errors: list[str] = []
        self.last_housekeeping = 0.0
        self.tcp_clients = 0

    # -- intake ------------------------------------------------------------------------
    def take(self, raw: bytes, ip: str, proto: str) -> None:
        minute = int(time.time() // 60)
        if len(self.queue) >= MAX_QUEUE:
            self.dropped += 1
            self.minute_dropped[minute] += 1
            return
        text = raw[:MAX_FRAME].decode("utf-8", "replace")
        if not text.strip():
            return
        try:
            row = logstore.parse(text, ip)
        except Exception as exc:  # noqa: BLE001 - keep the raw text rather than lose it
            row = {"ip": ip, "host": ip, "message": text, "facility": None, "severity": None, "app": None,
                   "procid": None, "msgid": None, "msg_time": None, "sd": None}
            self.note(f"parse error: {exc}")
        row["ts"] = time.time()
        row["proto"] = proto
        self.queue.append(tuple(row.get(c) for c in COLS))
        self.received += 1
        self.minute_counts[minute] += 1

    def note(self, msg: str) -> None:
        self.errors = (self.errors + [f"{datetime.now(timezone.utc):%H:%M:%S} {msg}"])[-10:]

    # -- database work (runs in the single db thread) ------------------------------------
    def open_db(self) -> None:
        self.con = logstore.connect(DATA_DIR)
        self.settings = logstore.load_settings(self.con)

    def write(self, rows: list[tuple], counts: dict[int, int], dropped: dict[int, int]) -> None:
        con = self.con
        with con:
            if rows:
                con.executemany(f"INSERT INTO logs({', '.join(COLS)}) VALUES ({', '.join('?' * len(COLS))})", rows)
            for minute in set(counts) | set(dropped):
                con.execute("INSERT INTO rates(minute, received, dropped) VALUES (?, ?, ?) ON CONFLICT(minute) DO UPDATE "
                            "SET received = received + excluded.received, dropped = dropped + excluded.dropped",
                            (minute, counts.get(minute, 0), dropped.get(minute, 0)))

    def housekeeping(self) -> None:
        con = self.con
        self.settings = logstore.load_settings(con)
        days = float(self.settings.get("retention_days") or 5)
        cap = float(self.settings.get("max_size_gb") or 5) * 1024 ** 3
        cutoff = time.time() - days * 86400
        removed = 0
        while True:   # in slices, so the writer never holds the database for long
            with con:
                n = con.execute("DELETE FROM logs WHERE id IN (SELECT id FROM logs WHERE ts < ? ORDER BY id LIMIT 20000)",
                                (cutoff,)).rowcount
            removed += n
            if n < 20000:
                break
        with con:
            con.execute("DELETE FROM rates WHERE minute < ?", (int(cutoff // 60),))
            con.execute("DELETE FROM alert_events WHERE fired_at < ?", (time.time() - max(days, 30) * 86400,))
        # Size cap: delete the oldest 5 % at a time until the data fits.
        capped = 0
        for _ in range(40):
            used = logstore.size_info(con, DATA_DIR)["used_bytes"]
            if used <= cap:
                break
            total = con.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
            if not total:
                break
            n = max(1000, total // 20)
            with con:
                capped += con.execute("DELETE FROM logs WHERE id IN (SELECT id FROM logs ORDER BY id LIMIT ?)", (n,)).rowcount
        if removed or capped:
            con.execute("PRAGMA incremental_vacuum").fetchall()
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            log(f"housekeeping: removed {removed} older than {days:g} days, {capped} for the {cap / 1024 ** 3:g} GB cap")
        self.last_housekeeping = time.time()

    def evaluate_alerts(self) -> list[tuple[dict, dict]]:
        """Check every enabled rule; returns (event, rule) pairs to notify."""
        con = self.con
        self.settings = logstore.load_settings(con)    # notification settings may have just changed
        if not self.settings.get("alerts_enabled", True):
            return []
        now = time.time()
        fired = []
        for rule in [dict(r) for r in con.execute("SELECT * FROM alert_rules WHERE enabled = 1")]:
            if rule["last_fired"] and now - rule["last_fired"] < rule["cooldown_minutes"] * 60:
                continue
            try:
                since = now - rule["window_minutes"] * 60
                if rule["threshold"] <= 1 and rule["last_fired"]:
                    # "Any match" rules miss nothing: matches that came during the wait are sent
                    # together when it is over, even if they are older than the window by then.
                    since = min(since, rule["last_fired"])
                where, params = logstore.build_where(rule["query"], since=since, min_severity=rule["min_severity"])
                # Only messages not alerted on yet: a message fires a rule once, even with no wait.
                where += " AND id > ?"
                params = params + [rule.get("last_id") or 0]
                count = con.execute(f"SELECT COUNT(*) FROM logs WHERE {where}", params).fetchone()[0]
            except (ValueError, sqlite3.Error) as exc:
                self.note(f"alert '{rule['name']}': {exc}")
                continue
            if count < max(1, rule["threshold"]):
                continue
            sample = [logstore.row_dict(r) for r in con.execute(
                f"SELECT * FROM logs WHERE {where} ORDER BY id DESC LIMIT 5", params)]
            for s in sample:
                s.pop("sd", None)
            event = {"rule_id": rule["id"], "rule_name": rule["name"], "fired_at": now, "count": count,
                     "window_minutes": rule["window_minutes"], "query": rule["query"],
                     "min_severity": rule["min_severity"], "sample": sample,
                     "fired_at_text": datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")}
            with con:
                cur = con.execute("INSERT INTO alert_events(rule_id, rule_name, fired_at, count, window_minutes, query, sample, notified)"
                                  " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                  (rule["id"], rule["name"], now, count, rule["window_minutes"], rule["query"],
                                   json.dumps(sample), "" if (rule["notify_email"] or rule["notify_webhook"]) else "shown on the Log alerts page"))
                newest = con.execute(f"SELECT MAX(id) FROM logs WHERE {where}", params).fetchone()[0] or 0
                con.execute("UPDATE alert_rules SET last_fired = ?, last_count = ?, last_id = ? WHERE id = ?",
                            (now, count, newest, rule["id"]))
            event["id"] = cur.lastrowid
            log(f"alert '{rule['name']}' fired: {count} message(s) in {rule['window_minutes']} min")
            if rule["notify_email"] or rule["notify_webhook"]:
                fired.append((event, rule))
        return fired

    def record_notified(self, event_id: int, result: str) -> None:
        with self.con:
            self.con.execute("UPDATE alert_events SET notified = ? WHERE id = ?", (result, event_id))

    def save_state(self) -> None:
        logstore.set_state(self.con, {
            "pid": os.getpid(), "started": self.started, "updated": time.time(), "udp_port": self.udp_port,
            "tcp_port": self.tcp_port, "received": self.received, "dropped": self.dropped, "queue": len(self.queue),
            "tcp_clients": self.tcp_clients, "errors": self.errors, "last_housekeeping": self.last_housekeeping,
        })

    # -- loops -------------------------------------------------------------------------
    async def run_db(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(self.db, fn, *args)

    async def flusher(self) -> None:
        while True:
            await asyncio.sleep(1)
            await self.flush()

    async def flush(self) -> None:
        rows, self.queue = self.queue, []
        counts, self.minute_counts = dict(self.minute_counts), Counter()
        dropped, self.minute_dropped = dict(self.minute_dropped), Counter()
        for i in range(0, max(len(rows), 1), 20000):
            try:
                await self.run_db(self.write, rows[i:i + 20000], counts if i == 0 else {}, dropped if i == 0 else {})
            except sqlite3.Error as exc:
                self.note(f"database write failed: {exc}")
                log(f"database write failed ({exc}); {len(rows)} messages lost")

    async def janitor(self) -> None:
        tick = 0
        while True:
            try:
                if tick % 10 == 0:          # every 5 minutes (and at start)
                    await self.run_db(self.housekeeping)
                fired = await self.run_db(self.evaluate_alerts)
                for event, rule in fired:
                    settings = dict(self.settings)
                    result = await asyncio.get_running_loop().run_in_executor(
                        self.mail, notify.notify, settings, event, bool(rule["notify_email"]), bool(rule["notify_webhook"]))
                    await self.run_db(self.record_notified, event["id"], result)
                    log(f"alert '{rule['name']}': {result}")
                await self.run_db(self.save_state)
            except Exception as exc:  # noqa: BLE001
                self.note(f"housekeeping: {exc}")
                log(f"housekeeping error: {exc}")
            tick += 1
            await asyncio.sleep(30)

    async def state_writer(self) -> None:
        while True:
            await asyncio.sleep(5)
            try:
                await self.run_db(self.save_state)
            except sqlite3.Error:
                pass


class Udp(asyncio.DatagramProtocol):
    def __init__(self, server: Server):
        self.server = server

    def datagram_received(self, data: bytes, addr) -> None:
        # One datagram can carry several messages separated by newlines (some senders batch).
        for part in data.split(b"\n") if data.count(b"\n") > 1 else [data]:
            self.server.take(part, addr[0], "udp")


async def tcp_client(server: Server, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    ip = (writer.get_extra_info("peername") or ("?",))[0]
    server.tcp_clients += 1
    buf = b""
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            buf += chunk
            while buf:
                if buf[:1].isdigit():
                    # Octet counting: "<length> <message>"
                    sp = buf.find(b" ")
                    if sp == -1:
                        if len(buf) > 10:
                            buf = buf.lstrip(b"0123456789")
                            continue
                        break
                    if not buf[:sp].isdigit():
                        # A digit-started plain line: fall back to newline framing.
                        nl = buf.find(b"\n")
                        if nl == -1:
                            break
                        server.take(buf[:nl], ip, "tcp")
                        buf = buf[nl + 1:]
                        continue
                    n = int(buf[:sp])
                    if n > MAX_FRAME * 4:
                        buf = b""
                        break
                    if len(buf) < sp + 1 + n:
                        break
                    server.take(buf[sp + 1:sp + 1 + n], ip, "tcp")
                    buf = buf[sp + 1 + n:]
                else:
                    nl = min((i for i in (buf.find(b"\n"), buf.find(b"\x00")) if i != -1), default=-1)
                    if nl == -1:
                        if len(buf) > MAX_FRAME:
                            server.take(buf, ip, "tcp")
                            buf = b""
                        break
                    server.take(buf[:nl], ip, "tcp")
                    buf = buf[nl + 1:]
        if buf.strip():
            server.take(buf, ip, "tcp")
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        server.tcp_clients -= 1
        writer.close()


async def main() -> int:
    server = Server()
    await server.run_db(server.open_db)
    loop = asyncio.get_running_loop()
    try:
        # A large receive buffer, so a burst (every host rebooting, a vMotion storm) isn't lost
        # before the listener reads it. The kernel may cap it (net.core.rmem_max).
        udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
        except OSError:
            pass
        udp_sock.bind(("0.0.0.0", server.udp_port))
        transport, _ = await loop.create_datagram_endpoint(lambda: Udp(server), sock=udp_sock)
        tcp = await asyncio.start_server(lambda r, w: tcp_client(server, r, w), "0.0.0.0", server.tcp_port, limit=MAX_FRAME * 4)
    except OSError as exc:
        log(f"cannot listen on port {server.udp_port}/{server.tcp_port}: {exc}")
        server.note(f"cannot listen: {exc}")
        await server.run_db(server.save_state)
        return 2
    log(f"syslog server listening on UDP {server.udp_port} and TCP {server.tcp_port}, data in {logstore.db_path(DATA_DIR)}")
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass
    tasks = [asyncio.create_task(t) for t in (server.flusher(), server.janitor(), server.state_writer())]
    await stop.wait()
    transport.close()
    tcp.close()
    for t in tasks:
        t.cancel()
    await server.flush()
    await server.run_db(server.save_state)
    log(f"syslog server stopped ({server.received} received, {server.dropped} dropped)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
