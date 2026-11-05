"""Data questions over spreadsheet sources.

The spreadsheet plug-in loads every sheet into <work folder>/tables.db. For a
counting, totals or top-N question the chat model writes ONE SQLite SELECT over
those tables; Ask checks it, runs it on a read-only connection with a time and row
limit, and the result goes back to the model as source [1]. The answer shows the
SQL and the result table, so a reader can check the numbers."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from .db import DB
from .ollama import Ollama

MAX_ROWS = 500            # rows a query may return
ROWS_TO_MODEL = 60        # rows sent to the model as the answer's source
QUERY_SECONDS = 5
MAX_TABLES = 5            # tables described to the model per question

DATA_WORDS = re.compile(
    r"\b(how many|how much|number of|count|total|sum|average|avg|mean|median|top \d+|top ten|top five|"
    r"most|least|largest|smallest|biggest|highest|lowest|maximum|minimum|max|min|percent|percentage|ratio|"
    r"per (cluster|host|datastore|vcenter|os)|group(ed)? by|list (all|every)|which (vms|hosts|datastores|clusters)|"
    r"more than|less than|greater than|fewer than|over \d|under \d|above \d|below \d|at least|at most)\b", re.I)
# Listing questions: "show all hosts", "list the VMs on cluster X", "give me every datastore".
ENTITY = (r"(vms?|virtual machines?|hosts?|esxi( hosts)?|servers?|datastores?|clusters?|snapshots?|templates?|"
          r"disks?|nics?|networks?|port ?groups?|switch(es)?|resource pools?|licen[cs]es?|partitions?|rows?|entries)")
LIST_WORDS = re.compile(
    rf"\b((show|list|display|give|get|print|export|find|return)( me)?( all| every| the| each)?( of)?( the| my)? ([a-z0-9_-]+ )?{ENTITY}|"
    rf"(all|every|each) (the |my |of the )?([a-z0-9_-]+ )?{ENTITY}|(which|what) ([a-z0-9_-]+ )?{ENTITY}|"
    r"(sheet|table|tab|column|spreadsheet|workbook|xlsx|rvtools)s?)\b", re.I)
# Chart requests: "pie chart of VMs per cluster", "donut of datastores by type", "bar graph ...".
CHART_WORDS = re.compile(r"\b(pie|donut|doughnut|bar|column|line|area)?\s*(chart|graph|plot|diagram)s?\b|\b(pie|donut|doughnut)\b|"
                         r"\b(chart|graph|plot|visuali[sz]e)\b", re.I)
CHART_TYPES = ("pie", "donut", "bar", "line")
MAX_SLICES = 12           # pie and donut: the largest slices, the rest becomes "Other"
MAX_CATEGORIES = 50       # bar and line: categories drawn
MAX_SERIES = 4            # bar and line: numeric columns drawn
# HTML reports: "HTML report of VMs by OS", "dashboard report of the vInfo sheet".
REPORT_WORDS = re.compile(r"\b(html|web)\s+(page|report|dashboard)\b|\breports?\b|\bdashboards?\b", re.I)
MAX_PANELS = 8            # dashboard: panels kept (KPIs count too)
PANEL_ROWS = 200          # dashboard: rows kept per panel
FORBIDDEN = re.compile(r"\b(attach|detach|pragma|insert|update|delete|drop|create|alter|replace|vacuum|reindex|"
                       r"analyze|load_extension|readfile|writefile|begin|commit|rollback|savepoint)\b", re.I)

SQL_SYSTEM = """You write ONE SQLite SELECT statement that answers the user's question from the tables below.
Rules:
- Use only the tables and columns listed. Quote nothing unless a name needs it.
- Text values are case sensitive; use LIKE or lower() when matching names.
- Boolean columns hold the text 'True' or 'False'.
- Templates are VMs too: leave them out (template = 'False') unless the question is about templates.
- Prefer readable column aliases, round decimals with round(x, 1), and ORDER BY for rankings.
- To show or list rows ("show all hosts"), return every matching row (no LIMIT unless the question asks for a number)
  with the 4 to 8 columns that identify and describe them best (name first, then cluster, state, sizes), ordered by name.
- Return the SQL only, in a ```sql code block, no explanation."""


def _near(a: str, b: str) -> bool:
    """Same word, or one typo apart (vhosst ~ vhost)."""
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1 or min(len(a), len(b)) < 4:
        return False
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1] <= 1


def named_tables(src: dict[str, Any], question: str) -> set[str]:
    """Tables whose sheet or table name appears in the question (allowing one typo)."""
    q = re.findall(r"[a-z0-9_]+", question.lower())
    out = set()
    for t in src["tables"]:
        names = {t["table"], re.sub(r"[^a-z0-9_]", "", t["sheet"].lower())}
        if any(_near(w, n) for w in q for n in names):
            out.add(t["table"])
    return out


def chart_type(question: str) -> str | None:
    """The chart the question asks for (pie, donut, bar or line), or None."""
    if not CHART_WORDS.search(question):
        return None
    q = question.lower()
    if re.search(r"\b(donut|doughnut|ring)\b", q):
        return "donut"
    if re.search(r"\bpie\b", q):
        return "pie"
    if re.search(r"\b(line|area|trend|over time|timeline)\b", q):
        return "line"
    return "bar"


def report_kind(question: str) -> str | None:
    """'dashboard' (several panels), 'report' (one query as a report page) or None."""
    if not REPORT_WORDS.search(question):
        return None
    return "dashboard" if re.search(r"\bdashboards?\b", question, re.I) else "report"


def is_data_question(question: str, srcs: list[dict[str, Any]] | None = None) -> bool:
    """Counting, totals, top-N, listing, chart and report questions, or any question that names a sheet."""
    if DATA_WORDS.search(question) or LIST_WORDS.search(question) or chart_type(question) or report_kind(question):
        return True
    return any(named_tables(s, question) for s in srcs or [])


def tables_for(db: DB, sources: list[str] | None) -> list[dict[str, Any]]:
    """Spreadsheet sources in scope that have loaded tables."""
    out = []
    for s in db.list_sources():
        if not s["enabled"] or (sources and s["name"] not in sources):
            continue
        st = db.get_state(s["name"])["state"]
        path = st.get("tables_db")
        if path and st.get("tables") and Path(path).exists():
            out.append({"source": s["name"], "title": s["title"], "path": path, "tables": st["tables"],
                        "files": st.get("files") or []})
    return out


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2}


def pick_source(srcs: list[dict[str, Any]], question: str) -> dict[str, Any]:
    """With several spreadsheet sources in scope, the one whose title or sheets share
    the most words with the question (the first one on a tie)."""
    q = _words(question)
    return max(srcs, key=lambda s: 5 * len(named_tables(s, question)) + len(q & _words(
        s["title"] + " " + s["source"] + " " + " ".join(t["sheet"] for t in s["tables"]))))


def pick_tables(src: dict[str, Any], question: str) -> list[dict[str, Any]]:
    """The tables most likely needed: sheet and column names that share words with the
    question, with vInfo / vHost / vDatastore / vCluster as tie-breakers."""
    q = _words(question)
    synonyms = {"vm": "vinfo", "vms": "vinfo", "virtual": "vinfo", "host": "vhost", "hosts": "vhost", "esxi": "vhost",
                "datastore": "vdatastore", "datastores": "vdatastore", "cluster": "vcluster", "clusters": "vcluster",
                "snapshot": "vsnapshot", "snapshots": "vsnapshot", "disk": "vdisk", "disks": "vdisk",
                "tools": "vtools", "license": "vlicense", "licenses": "vlicense", "nic": "vnetwork", "network": "vnetwork"}
    hinted = {synonyms[w] for w in q if w in synonyms}
    named = named_tables(src, question)
    base = {"vinfo": 3, "vhost": 2, "vdatastore": 1, "vcluster": 1}

    def score(t: dict[str, Any]) -> float:
        name = t["table"]
        cols = " ".join(f'{c["name"]} {c["header"]}' for c in t["columns"])
        s = 30 * (name in named) + 10 * (name in hinted) + 3 * len(q & _words(t["sheet"])) + len(q & _words(cols)) + base.get(name, 0) * 0.5
        return s
    return sorted(src["tables"], key=score, reverse=True)[:MAX_TABLES]


def describe(tables: list[dict[str, Any]]) -> str:
    parts = []
    for t in tables:
        cols = []
        for c in t["columns"]:
            line = f'  {c["name"]} {c["type"]}'
            if c["header"] and c["header"] != c["name"]:
                line += f'  -- "{c["header"]}"'
            if c.get("help"):
                line += f' ({c["help"]})'
            cols.append(line)
        sample = "\n".join("  " + " | ".join("" if v is None else str(v)[:30] for v in r) for r in t.get("samples", [])[:2])
        parts.append(f'TABLE {t["table"]}  -- sheet "{t["sheet"]}", {t["rows"]} rows\n' + "\n".join(cols)
                     + (f"\n  sample rows (same column order):\n{sample}" if sample else ""))
    return "\n\n".join(parts)


def extract_sql(text: str) -> str:
    m = re.search(r"```(?:sql|sqlite)?\s*(.+?)```", text, re.S | re.I)
    sql = (m.group(1) if m else text).strip()
    return sql.rstrip(";").strip()


def check_sql(sql: str, allowed: set[str]) -> str | None:
    """None when the statement is acceptable, otherwise the reason."""
    if not re.match(r"^(select|with)\b", sql, re.I):
        return "The statement must start with SELECT (or WITH)."
    if ";" in sql:
        return "Only one statement is allowed."
    if FORBIDDEN.search(re.sub(r"'[^']*'", "''", sql)):
        return "Only reading is allowed."
    return None


def run_sql(path: str, sql: str, allowed: set[str]) -> tuple[list[str], list[list[Any]], bool]:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    try:
        def authorizer(action: int, arg1: Any, arg2: Any, dbname: Any, source: Any) -> int:
            if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE):
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_READ:
                # The file only holds this source's sheets (and the connection is read-only);
                # SQLite's own tables stay out of reach. CTE names are reads too, so no allow-list.
                return sqlite3.SQLITE_DENY if (arg1 or "").lower().startswith("sqlite_") else sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY
        con.set_authorizer(authorizer)
        deadline = time.time() + QUERY_SECONDS
        con.set_progress_handler(lambda: 1 if time.time() > deadline else 0, 10000)
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description or []]
        rows = [list(r) for r in cur.fetchmany(MAX_ROWS + 1)]
        return cols, rows[:MAX_ROWS], len(rows) > MAX_ROWS
    finally:
        con.close()


def query(cfg: dict[str, Any], src: dict[str, Any], question: str) -> dict[str, Any]:
    """Ask the model for SQL, check and run it (one retry with the error). Returns the
    result, or {"error": ...} when no working query came back."""
    tables = pick_tables(src, question)
    allowed = {t["table"] for t in src["tables"]}  # every table of this source may be read
    system = SQL_SYSTEM
    kind = chart_type(question)
    if kind:
        system += (f"\n- The result will be drawn as a {kind} chart: return the category (label) column first, then "
                   + ("ONE numeric value column" if kind in ("pie", "donut") else "one to four numeric value columns")
                   + ", one row per category (GROUP BY it), ordered by "
                   + ("the category (time order)." if kind == "line" else "the value, largest first."))
    msgs = [{"role": "system", "content": system},
            {"role": "user", "content": f"Tables:\n\n{describe(tables)}\n\nQuestion: {question}"}]
    ollama = Ollama(cfg["ollama_url"])
    err = ""
    sql = ""
    for attempt in range(2):
        reply = ollama.chat(cfg["chat_model"], msgs, options={"temperature": 0, "num_ctx": cfg["num_ctx"]},
                            think=False, keep_alive=cfg["keep_alive"])
        sql = extract_sql(reply)
        err = check_sql(sql, allowed) or ""
        if not err:
            try:
                cols, rows, truncated = run_sql(src["path"], sql, allowed)
                used = sorted(t for t in allowed if re.search(rf"\b{re.escape(t)}\b", sql, re.I))
                return {"sql": sql, "columns": cols, "rows": rows, "truncated": truncated,
                        "chart": build_chart(kind, cols, rows) if kind else None,
                        "tables": used or sorted(t["table"] for t in tables), "source": src["source"], "source_title": src["title"],
                        "files": src.get("files") or []}
            except sqlite3.Error as e:
                err = str(e)
                if "interrupted" in err:
                    err = f"The query took longer than {QUERY_SECONDS} seconds."
                elif "not authorized" in err:
                    err = "The query did something other than read the sheets."
        msgs += [{"role": "assistant", "content": f"```sql\n{sql}\n```"},
                 {"role": "user", "content": f"That query failed: {err}\nWrite a corrected query."}]
    return {"error": err, "sql": sql}


def build_chart(kind: str, cols: list[str], rows: list[list[Any]]) -> dict[str, Any] | None:
    """A chart spec from the query result (no model involved): the first text column gives
    the labels, the numeric columns the values. None when the result can't be charted."""
    if kind not in CHART_TYPES or not rows or len(cols) < 2:
        return None

    def numeric(i: int) -> bool:
        vals = [r[i] for r in rows if r[i] is not None]
        return bool(vals) and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals)
    nums = [i for i in range(len(cols)) if numeric(i)]
    label = next((i for i in range(len(cols)) if i not in nums), None)
    if label is None:           # every column is a number: the first one is the label (a year, a size bucket)
        label = 0
        nums = [i for i in nums if i != 0]
    if not nums:
        return None
    nums = nums[:1] if kind in ("pie", "donut") else nums[:MAX_SERIES]
    data = [("(empty)" if r[label] in (None, "") else str(r[label]), [r[i] or 0 for i in nums]) for r in rows]
    note = ""
    if kind in ("pie", "donut"):
        data = [d for d in data if d[1][0] > 0]
        data.sort(key=lambda d: d[1][0], reverse=True)
        if len(data) > MAX_SLICES:
            rest = data[MAX_SLICES - 1:]
            data = data[:MAX_SLICES - 1] + [(f"Other ({len(rest)})", [sum(d[1][0] for d in rest)])]
            note = f"The {MAX_SLICES - 1} largest slices; {len(rest)} smaller ones are grouped as Other."
        if not data:
            return None
    elif len(data) > MAX_CATEGORIES:
        note = f"The first {MAX_CATEGORIES} of {len(data)} categories are drawn."
        data = data[:MAX_CATEGORIES]
    names = [cols[i] for i in nums]
    return {"type": kind, "label": cols[label], "series": names,
            "title": f"{', '.join(names[:-1]) + ' and ' if len(names) > 1 else ''}{names[-1]} by {cols[label]}",
            "categories": [d[0] for d in data],
            "values": [[round(v, 2) if isinstance(v, float) else v for v in d[1]] for d in data], "note": note}


def auto_chart(cols: list[str], rows: list[list[Any]], prefer: str | None = None) -> dict[str, Any] | None:
    """A chart for a report when the question named none: a donut for up to 8 categories, else bars."""
    if prefer:
        return build_chart(prefer, cols, rows)
    return build_chart("donut" if len(rows) <= 8 else "bar", cols, rows)


PLAN_SYSTEM = """You plan a dashboard over the SQLite tables below. Return ONLY a JSON array (no prose, no code fence) of
4 to 8 panels, each {"title": "...", "chart": "kpi|donut|pie|bar|line|table", "sql": "one SQLite SELECT"}.
- Start with 2 to 4 "kpi" panels: each query returns exactly ONE row with ONE number (a count, a total or an average).
- Then charts: "donut"/"pie" = a label column and ONE number, GROUP BY the label, largest first;
  "bar" = a label column and one to three numbers; "line" = a time or ordered label and numbers;
  "table" = a top-N list (LIMIT 10 to 20) with the 3 to 6 most useful columns.
- Titles are short (2 to 6 words) and say what is counted, e.g. "VMs by OS", "Top 10 VMs by storage".
- Use only the tables and columns listed. Text values are case sensitive; use LIKE or lower() for names.
- Boolean columns hold the text 'True' or 'False'. Leave templates out (template = 'False') unless asked.
- Round decimals with round(x, 1) and use readable column aliases."""


def _panel_json(text: str) -> list[dict[str, Any]]:
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return []
    try:
        items = json.loads(m.group(0))
    except ValueError:
        return []
    return [i for i in items if isinstance(i, dict) and isinstance(i.get("sql"), str)][:MAX_PANELS + 2]


def dashboard(cfg: dict[str, Any], src: dict[str, Any], question: str) -> dict[str, Any]:
    """Ask the model for a dashboard plan (panels with SQL), run every panel's query with the
    same checks as a data question and keep the ones that work. Returns
    {"kind": "dashboard", "panels": [...], "skipped": [...], ...} or {"error": ...}."""
    tables = pick_tables(src, question)
    allowed = {t["table"] for t in src["tables"]}
    msgs = [{"role": "system", "content": PLAN_SYSTEM},
            {"role": "user", "content": f"Tables:\n\n{describe(tables)}\n\nDashboard request: {question}"}]
    ollama = Ollama(cfg["ollama_url"])
    plan: list[dict[str, Any]] = []
    for attempt in range(2):
        reply = ollama.chat(cfg["chat_model"], msgs, options={"temperature": 0, "num_ctx": cfg["num_ctx"]},
                            think=False, keep_alive=cfg["keep_alive"])
        plan = _panel_json(reply)
        if plan:
            break
        msgs += [{"role": "assistant", "content": reply[:2000]},
                 {"role": "user", "content": "Return only the JSON array of panels, nothing else."}]
    if not plan:
        return {"error": "The model did not return a dashboard plan."}
    panels, skipped = [], []
    for item in plan:
        if len(panels) >= MAX_PANELS:
            break
        title = str(item.get("title") or "Panel")[:80]
        kind = str(item.get("chart") or "table").lower()
        kind = kind if kind in ("kpi", "table") + CHART_TYPES else "table"
        sql = extract_sql(item["sql"])
        err = check_sql(sql, allowed)
        if not err:
            try:
                cols, rows, truncated = run_sql(src["path"], sql, allowed)
            except sqlite3.Error as e:
                err = "took too long" if "interrupted" in str(e) else str(e)
        if err:
            skipped.append({"title": title, "sql": sql, "error": err})
            continue
        panel: dict[str, Any] = {"title": title, "kind": kind, "sql": sql, "columns": cols,
                                 "rows": rows[:PANEL_ROWS], "row_count": len(rows), "truncated": truncated or len(rows) > PANEL_ROWS}
        if kind == "kpi":
            nums = [v for v in (rows[0] if rows else []) if isinstance(v, (int, float)) and not isinstance(v, bool)]
            if len(rows) != 1 or not nums:
                panel["kind"] = "table"
            else:
                panel["value"] = round(nums[0], 1) if isinstance(nums[0], float) else nums[0]
        elif kind in CHART_TYPES:
            panel["chart"] = build_chart(kind, cols, rows)
            if not panel["chart"]:
                panel["kind"] = "table"
            else:
                panel["chart"]["title"] = title
        panels.append(panel)
    if not panels:
        return {"error": "None of the dashboard's queries worked: " + "; ".join(s["error"] for s in skipped[:3])}
    first = next((p for p in panels if p["kind"] != "kpi"), panels[0])
    return {"kind": "dashboard", "panels": panels, "skipped": skipped, "sql": first["sql"], "columns": first["columns"],
            "rows": first["rows"], "truncated": first["truncated"], "chart": first.get("chart"),
            "tables": sorted(t for t in allowed if any(re.search(rf"\b{re.escape(t)}\b", p["sql"], re.I) for p in panels)),
            "source": src["source"], "source_title": src["title"], "files": src.get("files") or []}


def panels_markdown(res: dict[str, Any]) -> str:
    """Every dashboard panel's query and result for the model (15 rows each)."""
    out = []
    for i, p in enumerate(res["panels"], 1):
        part = f"Panel {i}: {p['title']} ({p['kind']})\n```sql\n{p['sql']}\n```\n"
        part += result_markdown({**p, "rows": p["rows"][:15], "truncated": p["row_count"] > 15})
        out.append(part)
    return "\n\n".join(out)


def result_markdown(res: dict[str, Any]) -> str:
    cols, rows = res["columns"], res["rows"]
    if not rows:
        return "(the query returned no rows)"
    shown = rows[:ROWS_TO_MODEL]

    def cellfmt(v: Any) -> str:
        if isinstance(v, float):
            v = round(v, 2)
            if v.is_integer():
                v = int(v)
        return "" if v is None else str(v).replace("|", "/")
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    lines += ["| " + " | ".join(cellfmt(v) for v in r) + " |" for r in shown]
    more = len(rows) - len(shown)
    if more > 0 or res.get("truncated"):
        lines.append(f"\n({len(rows)}{'+' if res.get('truncated') else ''} rows in total; the first {len(shown)} are shown)")
    return "\n".join(lines)


def card(res: dict[str, Any]) -> dict[str, Any]:
    """What the UI shows under the answer, saved with the message."""
    return {"n": 1, "kind": "data", "source": res["source"], "title": f"{'Dashboard' if res.get('panels') else 'Data query'} · {res['source_title']}",
            "heading": "", "doc_id": ", ".join(res["tables"]), "doc_row": None, "url": "", "status": "",
            "sql": res["sql"], "columns": res["columns"], "rows": res["rows"], "chart": res.get("chart"),
            "panels": res.get("panels"), "skipped": res.get("skipped"),
            "row_count": len(res["rows"]), "truncated": res["truncated"], "snippet": "", "images": []}
