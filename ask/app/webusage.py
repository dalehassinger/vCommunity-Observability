"""Web search usage and estimated cost, counted from Ask's own calls.

Neither ollama.com nor Brave has an API for the usage and cost numbers on their account
pages, so Ask records every web search call it makes (app/websearch.py) in ask.db, table
web_usage, and the Performance page shows them:

- requests per day of the current month and per tool (Ollama web_search and web_fetch, Brave brave_search),
  with failures and rate-limit (429) answers counted separately;
- an estimated cost = successful calls x the price per call set on the Settings page
  (defaults from the providers' pay-as-you-go prices in November 2026: Ollama web search
  about $0.01, Brave Search $5 per 1,000 = $0.005; failed calls are not counted);
- Brave's own quota: every Brave answer has X-RateLimit-Limit / -Remaining / -Reset headers
  (per second, per month), so the latest monthly numbers are shown as Brave reports them.

Only searches made by Ask are counted, not other apps that use the same keys. Months are
calendar months in UTC (Ollama's account page uses the same). Rows are kept 400 days."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Callable

SCHEMA = """CREATE TABLE IF NOT EXISTS web_usage (
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, provider TEXT NOT NULL, tool TEXT NOT NULL,
  ok INTEGER NOT NULL, limited INTEGER NOT NULL DEFAULT 0, ms INTEGER,
  quota_limit INTEGER, quota_remaining INTEGER, quota_reset REAL)"""
INDEX = "CREATE INDEX IF NOT EXISTS web_usage_ts ON web_usage(ts)"
KEEP_DAYS = 400
PRICE_KEYS = {("ollama", "web_search"): "web_price_ollama_search", ("ollama", "web_fetch"): "web_price_ollama_fetch",
              ("brave", "brave_search"): "web_price_brave_search"}
LABELS = {("ollama", "web_search"): "Ollama web search", ("ollama", "web_fetch"): "Ollama web fetch",
          ("brave", "brave_search"): "Brave search"}


class Usage:
    def __init__(self, db: Any):
        self.db = db
        with db.conn() as c:
            c.execute(SCHEMA)
            c.execute(INDEX)

    def record(self, provider: str, tool: str, ok: bool, limited: bool = False, ms: int | None = None,
               quota: dict[str, Any] | None = None) -> None:
        q = quota or {}
        try:
            with self.db.conn() as c:
                c.execute("""INSERT INTO web_usage(ts, provider, tool, ok, limited, ms, quota_limit, quota_remaining, quota_reset)
                             VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                          (time.time(), provider, tool, int(bool(ok)), int(bool(limited)), ms,
                           q.get("limit"), q.get("remaining"), q.get("reset_at")))
                c.execute("DELETE FROM web_usage WHERE ts < ?", (time.time() - KEEP_DAYS * 86400,))
        except Exception:   # counting must never break an answer
            pass

    @staticmethod
    def _price(cfg: dict[str, Any], key: tuple[str, str]) -> float:
        try:
            return max(0.0, float(cfg.get(PRICE_KEYS.get(key, ""), 0) or 0))
        except (TypeError, ValueError):
            return 0.0

    def summary(self, cfg: dict[str, Any]) -> dict[str, Any]:
        """The current calendar month (UTC): totals, estimated cost and one bar per day so far."""
        now = datetime.now(timezone.utc)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
        day0, days, since = month_start, now.day, month_start
        with self.db.conn() as c:
            rows = c.execute("SELECT ts, provider, tool, ok, limited FROM web_usage WHERE ts >= ? ORDER BY ts",
                             (since,)).fetchall()
            q = c.execute("""SELECT ts, quota_limit, quota_remaining, quota_reset FROM web_usage
                             WHERE provider='brave' AND quota_remaining IS NOT NULL ORDER BY ts DESC LIMIT 1""").fetchone()
        prices = {LABELS[k]: self._price(cfg, k) for k in LABELS}
        tools = {k: {"label": LABELS[k], "ok": 0, "failed": 0, "limited": 0, "cost": 0.0, "price": self._price(cfg, k)}
                 for k in LABELS}
        per_day: dict[str, dict[str, Any]] = {}
        for i in range(days):
            d = datetime.fromtimestamp(day0 + i * 86400, timezone.utc).strftime("%Y-%m-%d")
            per_day[d] = {"date": d, **{LABELS[k]: 0 for k in LABELS}, "failed": 0, "cost": 0.0}
        for r in rows:
            key = (r["provider"], r["tool"])
            if key not in LABELS:
                continue
            price = tools[key]["price"]
            if r["ts"] >= month_start:
                t = tools[key]
                if r["ok"]:
                    t["ok"] += 1
                    t["cost"] += price
                else:
                    t["failed"] += 1
                    t["limited"] += int(r["limited"])
            if r["ts"] >= day0:
                d = per_day.get(datetime.fromtimestamp(r["ts"], timezone.utc).strftime("%Y-%m-%d"))
                if d is not None:
                    if r["ok"]:
                        d[LABELS[key]] += 1
                        d["cost"] += price
                    else:
                        d["failed"] += 1
        providers = {}
        for p, name in (("ollama", "Ollama"), ("brave", "Brave")):
            ts = [t for k, t in tools.items() if k[0] == p]
            providers[p] = {"name": name, "requests": sum(t["ok"] for t in ts), "failed": sum(t["failed"] for t in ts),
                            "limited": sum(t["limited"] for t in ts), "cost": round(sum(t["cost"] for t in ts), 4)}
        quota = None
        if q:
            quota = {"limit": q["quota_limit"], "remaining": q["quota_remaining"], "reset_at": q["quota_reset"], "seen": q["ts"]}
        return {"month": now.strftime("%B %Y"), "month_start": month_start, "providers": providers,
                "tools": [{**t, "cost": round(t["cost"], 4)} for t in tools.values()],
                "total_cost": round(sum(p["cost"] for p in providers.values()), 4),
                "days": [{**d, "cost": round(d["cost"], 4)} for d in per_day.values()],
                "series": [LABELS[k] for k in LABELS], "prices": prices, "brave_quota": quota}


def parse_quota(headers: Any) -> dict[str, Any] | None:
    """Brave's monthly numbers from X-RateLimit-* headers ("1, 15000": per second, per month)."""
    def last(name: str) -> int | None:
        v = (headers.get(name) or "").split(",")
        try:
            return int(v[-1].strip()) if v and v[-1].strip() else None
        except ValueError:
            return None
    limit, remaining, reset = last("X-RateLimit-Limit"), last("X-RateLimit-Remaining"), last("X-RateLimit-Reset")
    if remaining is None and limit is None:
        return None
    return {"limit": limit, "remaining": remaining, "reset_at": time.time() + reset if reset is not None else None}


RECORDER: Callable[..., None] | None = None   # Usage.record, set by create_app
