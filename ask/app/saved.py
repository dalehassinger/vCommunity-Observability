"""Saved prompts: a prompt with its sources (knowledge sources, web fetch, web search, Brave,
MCP tools), an optional model and optional pages to fetch, kept so it can be asked again
with one click (the Saved menu under the chat box, or the Saved prompts page).

Running one starts a new chat and sends the prompt exactly as typed in the chat. When pages
are listed, a short "Read these pages" list is added under the prompt, so with
**Web fetch (Ollama)** picked the model reads only those pages (one Ollama web_fetch call
each) and does not search. Stored in ask.db, table saved_prompts.
"""
from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlparse

from .db import DB

TABLES = """
CREATE TABLE IF NOT EXISTS saved_prompts (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, prompt TEXT NOT NULL, scope TEXT NOT NULL DEFAULT 'all',
  model TEXT NOT NULL DEFAULT '', urls TEXT NOT NULL DEFAULT '', tags TEXT NOT NULL DEFAULT '',
  uses INTEGER NOT NULL DEFAULT 0,
  last_used REAL, created REAL NOT NULL, updated REAL NOT NULL);
"""

MAX_URLS = 10
MAX_TAGS = 10
FETCH_TOOLS = ("web:fetch", "web:ollama")      # scopes that give the model web_fetch


def url_list(value: Any) -> list[str]:
    items = value if isinstance(value, list) else str(value or "").replace(",", "\n").split()
    return [str(u).strip() for u in items if str(u).strip()]


def tag_list(value: Any) -> list[str]:
    """Tags such as sports, weather, news: lower case, commas or spaces between, no repeats."""
    items = value if isinstance(value, list) else str(value or "").replace(",", " ").split()
    out: list[str] = []
    for t in items:
        t = str(t).strip().lstrip("#").lower()[:30]
        if t and t not in out:
            out.append(t)
    return out


def scope_text(scope: Any) -> str:
    s = ",".join(map(str, scope)) if isinstance(scope, list) else str(scope or "")
    return ",".join(p.strip() for p in s.split(",") if p.strip()) or "all"


def clean(body: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """The saved fields from a form or API body, and errors per field."""
    errors: dict[str, str] = {}
    s: dict[str, Any] = {"name": str(body.get("name") or "").strip()[:120],
                         "prompt": str(body.get("prompt") or "").strip(),
                         "scope": scope_text(body.get("scope")),
                         "model": str(body.get("model") or "").strip()[:200]}
    if not s["prompt"]:
        errors["prompt"] = "Write the prompt."
    if not s["name"]:
        s["name"] = " ".join(s["prompt"].split())[:60] or "Saved prompt"
    urls = url_list(body.get("urls"))
    bad = [u for u in urls if urlparse(u).scheme not in ("http", "https") or not urlparse(u).netloc]
    if bad:
        errors["urls"] = "Not a full http(s) URL: " + ", ".join(bad[:3])
    elif len(urls) > MAX_URLS:
        errors["urls"] = f"At most {MAX_URLS} pages."
    elif urls and not any(t in s["scope"].split(",") for t in FETCH_TOOLS):
        errors["urls"] = "Pick Web fetch (Ollama) or Web search (Ollama) under Sources so the pages can be read."
    s["urls"] = "\n".join(urls)
    tags = tag_list(body.get("tags"))
    if len(tags) > MAX_TAGS:
        errors["tags"] = f"At most {MAX_TAGS} tags."
    s["tags"] = ",".join(tags)
    return s, errors


def question(item: dict[str, Any]) -> str:
    """What is sent to the chat: the prompt, plus the pages to read when there are any."""
    urls = url_list(item.get("urls"))
    if not urls:
        return item["prompt"]
    return item["prompt"] + "\n\nRead these pages with web_fetch:\n" + "\n".join(f"- {u}" for u in urls)


class Saved:
    def __init__(self, db: DB):
        self.db = db
        with db.conn() as c:
            c.executescript(TABLES)
            # 2026-11-16: tags (sports, weather, news ...) to filter the list.
            if "tags" not in {r[1] for r in c.execute("PRAGMA table_info(saved_prompts)")}:
                c.execute("ALTER TABLE saved_prompts ADD COLUMN tags TEXT NOT NULL DEFAULT ''")

    def list(self) -> list[dict[str, Any]]:
        with self.db.conn() as c:
            rows = [dict(r) for r in c.execute("SELECT * FROM saved_prompts ORDER BY name COLLATE NOCASE")]
        for r in rows:
            r["question"] = question(r)
            r["tag_list"] = tag_list(r.get("tags"))
        return rows

    def all_tags(self) -> list[str]:
        return sorted({t for r in self.list() for t in r["tag_list"]})

    def get(self, pid: int) -> dict[str, Any] | None:
        with self.db.conn() as c:
            r = c.execute("SELECT * FROM saved_prompts WHERE id=?", (pid,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["question"] = question(d)
        d["tag_list"] = tag_list(d.get("tags"))
        return d

    def save(self, body: dict[str, Any], pid: int | None = None) -> dict[str, Any]:
        s, errors = clean(body)
        if errors:
            raise ValueError(errors)
        now = time.time()
        cols = list(s)
        with self.db.conn() as c:
            if pid:
                if not c.execute("UPDATE saved_prompts SET " + ", ".join(f"{k}=?" for k in cols) + ", updated=? WHERE id=?",
                                 [s[k] for k in cols] + [now, pid]).rowcount:
                    raise KeyError(pid)
            else:
                pid = c.execute(f"INSERT INTO saved_prompts({', '.join(cols)}, created, updated) "
                                f"VALUES({', '.join('?' * len(cols))}, ?, ?)", [s[k] for k in cols] + [now, now]).lastrowid
        return self.get(pid)  # type: ignore[arg-type,return-value]

    def delete(self, pid: int) -> bool:
        with self.db.conn() as c:
            return c.execute("DELETE FROM saved_prompts WHERE id=?", (pid,)).rowcount > 0

    def delete_many(self, ids: list[int]) -> int:
        if not ids:
            return 0
        with self.db.conn() as c:
            return c.execute(f"DELETE FROM saved_prompts WHERE id IN ({','.join('?' * len(ids))})", ids).rowcount

    def used(self, pid: int) -> None:
        with self.db.conn() as c:
            c.execute("UPDATE saved_prompts SET uses = uses + 1, last_used=? WHERE id=?", (time.time(), pid))
