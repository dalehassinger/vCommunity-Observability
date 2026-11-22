"""Saved prompts: a prompt with its sources (knowledge sources, web fetch, web search, Brave,
MCP tools), an optional model and optional pages to fetch, kept so it can be asked again
with one click (the Saved menu under the chat box, or the Saved prompts page).

Running one starts a new chat and sends the prompt exactly as typed in the chat. When pages
are listed, a short "Read these pages" list is added under the prompt, so with
**Web fetch (Ollama)** picked the model reads only those pages (one Ollama web_fetch call
each) and does not search. Stored in ask.db, table saved_prompts.

A prompt can hold variables, {city} or {city=Dillsburg} (with a default), in the prompt or
the pages: Run asks for their values first. Pinned prompts list first, then the most used.
Export / import moves them between labs as JSON.
"""
from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import quote, urlparse

from .db import DB

TABLES = """
CREATE TABLE IF NOT EXISTS saved_prompts (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, prompt TEXT NOT NULL, scope TEXT NOT NULL DEFAULT 'all',
  model TEXT NOT NULL DEFAULT '', urls TEXT NOT NULL DEFAULT '', tags TEXT NOT NULL DEFAULT '',
  uses INTEGER NOT NULL DEFAULT 0,
  last_used REAL, created REAL NOT NULL, updated REAL NOT NULL);
"""

MAX_URLS = 10
MAX_VARS = 30
# {city} or {city=Dillsburg}: a name (letter first, then letters, digits, _), an optional default.
VAR_RE = re.compile(r"\{([A-Za-z][A-Za-z0-9_]{0,30})(?:=([^{}\n]{0,100}))?\}")
MAX_TAGS = 10
FETCH_TOOLS = ("web:fetch", "web:ollama", "web:local")      # scopes that give the model web_fetch


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
    # A page may hold a {variable}; check it with a sample value in its place.
    bad = [u for u in urls if urlparse(VAR_RE.sub("x", u)).scheme not in ("http", "https") or not urlparse(VAR_RE.sub("x", u)).netloc]
    if bad:
        errors["urls"] = "Not a full http(s) URL: " + ", ".join(bad[:3])
    elif len(urls) > MAX_URLS:
        errors["urls"] = f"At most {MAX_URLS} pages."
    elif urls and not any(t in s["scope"].split(",") for t in FETCH_TOOLS):
        errors["urls"] = "Pick Web fetch (Ollama), Web fetch (local) or Web search (Ollama) under Sources so the pages can be read."
    s["urls"] = "\n".join(urls)
    tags = tag_list(body.get("tags"))
    if len(tags) > MAX_TAGS:
        errors["tags"] = f"At most {MAX_TAGS} tags."
    s["tags"] = ",".join(tags)
    if len(variables({"prompt": s["prompt"], "urls": s["urls"]})) > MAX_VARS:
        errors["prompt"] = f"At most {MAX_VARS} different {{variables}}."
    if "pinned" in body:
        v = body.get("pinned")
        s["pinned"] = 1 if (v is True or str(v).lower() in ("1", "true", "on", "yes")) else 0
    return s, errors


def _view(r: dict[str, Any]) -> dict[str, Any]:
    r["question"] = question(r)
    r["tag_list"] = tag_list(r.get("tags"))
    r["variables"] = variables(r)
    r["pinned"] = int(r.get("pinned") or 0)
    return r


EXPORT_FIELDS = ("name", "prompt", "scope", "model", "urls", "tags", "pinned")


def variables(item: dict[str, Any]) -> list[dict[str, str]]:
    """The {variables} in the prompt and pages, in order, each once: [{"name", "default"}]."""
    out: dict[str, str] = {}
    for m in VAR_RE.finditer(str(item.get("prompt") or "") + "\n" + str(item.get("urls") or "")):
        name = m.group(1)
        if name not in out or (not out[name] and m.group(2)):
            out[name] = (m.group(2) or "").strip()
    return [{"name": k, "default": v} for k, v in out.items()]


def fill(text: str, values: dict[str, Any] | None = None, *, strict: bool = False) -> str:
    """Put the values in place of {name} / {name=default}. A missing value uses the default;
    with no default it stays as written, or raises KeyError when strict."""
    values = {str(k): str(v).strip() for k, v in (values or {}).items() if str(v).strip()}

    def one(m: re.Match) -> str:
        v = values.get(m.group(1)) or (m.group(2) or "").strip()
        if not v:
            if strict:
                raise KeyError(m.group(1))
            return m.group(0)
        return v
    return VAR_RE.sub(one, text or "")


def question(item: dict[str, Any], values: dict[str, Any] | None = None, *, strict: bool = False) -> str:
    """What is sent to the chat: the prompt, plus the pages to read when there are any. With
    values (or defaults), the {variables} are filled in."""
    if values is not None:
        # A default given once ({city=Dillsburg}) counts wherever {city} is written.
        values = {**{v["name"]: v["default"] for v in variables(item) if v["default"]},
                  **{k: v for k, v in values.items() if str(v).strip()}}
    prompt = fill(item["prompt"], values, strict=strict) if values is not None else item["prompt"]
    urls = url_list(item.get("urls"))
    if values is not None:
        # In a page address a value is URL-encoded ("New York" -> "New%20York").
        urls = [fill(u, {k: quote(str(v), safe="") for k, v in values.items()}, strict=strict) for u in urls]
    if not urls:
        return prompt
    return prompt + "\n\nRead these pages with web_fetch:\n" + "\n".join(f"- {u}" for u in urls)


def fit_scope(scope: str, available: set[str]) -> tuple[str, list[str]]:
    """A saved prompt's sources, keeping only the ones that can be used now (a source deleted or
    turned off, a web search without its key, an MCP server that is down). When none of them is
    left, the answer is "none" = No sources (just the model), never All sources.
    Returns (scope to use, the picks that were left out)."""
    items = [v.strip() for v in str(scope or "").split(",") if v.strip()]
    if not items or "all" in items:
        return "all", []
    keep = [v for v in items if v == "none" or v in available]
    missing = [v for v in items if v not in keep]
    return ",".join(keep) or "none", missing


class Saved:
    def __init__(self, db: DB):
        self.db = db
        with db.conn() as c:
            c.executescript(TABLES)
            # 2026-11-16: tags (sports, weather, news ...) to filter the list.
            have = {r[1] for r in c.execute("PRAGMA table_info(saved_prompts)")}
            if "tags" not in have:
                c.execute("ALTER TABLE saved_prompts ADD COLUMN tags TEXT NOT NULL DEFAULT ''")
            # 2026-11-16: pinned prompts list first (then the most used).
            if "pinned" not in have:
                c.execute("ALTER TABLE saved_prompts ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")

    def list(self) -> list[dict[str, Any]]:
        """Pinned first, then the most used, then by name."""
        with self.db.conn() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT * FROM saved_prompts ORDER BY pinned DESC, uses DESC, name COLLATE NOCASE")]
        return [_view(r) for r in rows]

    def all_tags(self) -> list[str]:
        return sorted({t for r in self.list() for t in r["tag_list"]})

    def get(self, pid: int) -> dict[str, Any] | None:
        with self.db.conn() as c:
            r = c.execute("SELECT * FROM saved_prompts WHERE id=?", (pid,)).fetchone()
        if not r:
            return None
        return _view(dict(r))

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

    def pin(self, pid: int, on: bool) -> bool:
        with self.db.conn() as c:
            return c.execute("UPDATE saved_prompts SET pinned=? WHERE id=?", (1 if on else 0, pid)).rowcount > 0

    def export(self) -> dict[str, Any]:
        """Every saved prompt as JSON to move or share (no use counts or ids)."""
        return {"kind": "vcommunity-ask-saved-prompts", "version": 1, "exported": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "items": [{k: r.get(k) for k in EXPORT_FIELDS} for r in self.list()]}

    def import_items(self, data: Any) -> dict[str, Any]:
        """Add saved prompts from an export. A prompt with the same name as one here replaces
        its fields (its use count is kept); the rest are added. Bad entries are skipped."""
        items = data.get("items") if isinstance(data, dict) else data
        if not isinstance(items, list):
            raise ValueError("Not a saved prompts export: it needs an \"items\" list.")
        by_name = {r["name"].lower(): r["id"] for r in self.list()}
        added = replaced = 0
        skipped: list[str] = []
        for i, it in enumerate(items[:500]):
            if not isinstance(it, dict):
                skipped.append(f"#{i + 1}: not an object")
                continue
            body = {k: it.get(k) for k in EXPORT_FIELDS if k in it}
            s, errors = clean(body)
            if errors:
                skipped.append(f"{s.get('name') or '#' + str(i + 1)}: " + "; ".join(errors.values()))
                continue
            pid = by_name.get(s["name"].lower())
            got = self.save(body, pid)
            by_name[got["name"].lower()] = got["id"]
            if pid:
                replaced += 1
            else:
                added += 1
        return {"added": added, "replaced": replaced, "skipped": skipped}

    def used(self, pid: int) -> None:
        with self.db.conn() as c:
            c.execute("UPDATE saved_prompts SET uses = uses + 1, last_used=? WHERE id=?", (time.time(), pid))
