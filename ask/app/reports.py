"""HTML reports for data questions.

A report is ONE self-contained .html file: the stylesheet, the data, the chart library
(Apache ECharts), marked + DOMPurify for the written summary and the small report script are
all inlined, so it opens offline, can be emailed or archived, and needs nothing from Ask.

Two kinds:
  report     one query: KPI cards, the chart (and a bar version of a pie/donut), the full table
  dashboard  several panels the model planned (KPIs, charts, top-N tables), each query checked
             like any data question

The page is built from the fixed template report.html. The model only wrote the SQL and the
summary; the summary is markdown, sanitized by DOMPurify when the report is opened, and the
data goes in as JSON. Files live in <data_dir>/reports/<id>.html with a <id>.json sidecar."""

from __future__ import annotations

import json
import re
import secrets
import time
from pathlib import Path
from typing import Any

REPORT_ID = re.compile(r"^[0-9a-f]{12}$")
LEAD = re.compile(r"^\s*(please\s+)?(create|make|generate|build|give me|show( me)?|produce|prepare)?\s*((an?|the|me)\s+)*"
                  r"(modern\s+)?(html\s+|web\s+)?(dashboard[- ]style\s+|dashboard\s+)?(html\s+)?(report|dashboard|page)s?\s*"
                  r"(of|for|on|about|showing|with)?\s*", re.I)


def report_title(question: str, fallback: str, dashboard: bool = False) -> str:
    """'HTML report of VMs by OS using the vInfo sheet' -> 'VMs by OS using the vInfo sheet';
    'Dashboard report of the vInfo sheet' -> 'vInfo sheet dashboard'."""
    t = LEAD.sub("", question.strip().splitlines()[0] if question.strip() else "").strip(" .?!")
    if dashboard:
        t = re.sub(r"^the\s+", "", t, flags=re.I)
        t = (t + " dashboard") if t and "dashboard" not in t.lower() else (t or "Dashboard")
    if not t:
        return fallback
    if t[:1].islower() and not t[1:2].isupper():   # keep names such as vInfo or vSAN as written
        t = t[:1].upper() + t[1:]
    return t[:110]


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def fmt(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:,.1f}".rstrip("0").rstrip(".") if abs(v) < 1e15 else str(v)
    if isinstance(v, int) and not isinstance(v, bool):
        return f"{v:,}"
    return "" if v is None else str(v)


class Reports:
    def __init__(self, folder: Path, templates: Any, shared: Path, static: Path):
        self.folder = Path(folder)
        self.templates = templates      # Jinja2 environment
        self.shared = Path(shared)      # app.css and vendor/ (from the first container)
        self.static = Path(static)      # charts.js, report.js

    # -- files ------------------------------------------------------------------
    def path(self, rid: str) -> Path | None:
        if not REPORT_ID.match(rid or ""):
            return None
        p = self.folder / f"{rid}.html"
        return p if p.is_file() else None

    def meta(self, rid: str) -> dict[str, Any] | None:
        p = self.path(rid)
        if not p:
            return None
        try:
            return json.loads(p.with_suffix(".json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"id": rid, "title": rid, "kind": "report", "created": p.stat().st_mtime}

    def list(self) -> list[dict[str, Any]]:
        if not self.folder.is_dir():
            return []
        out = []
        for p in self.folder.glob("*.html"):
            m = self.meta(p.stem)
            if m:
                m["size_kb"] = round(p.stat().st_size / 1024)
                out.append(m)
        return sorted(out, key=lambda m: m.get("created", 0), reverse=True)

    def delete(self, rid: str) -> bool:
        p = self.path(rid)
        if not p:
            return False
        p.unlink(missing_ok=True)
        p.with_suffix(".json").unlink(missing_ok=True)
        return True

    # -- building ---------------------------------------------------------------
    def _asset(self, path: Path) -> str:
        # Inlined into <script>/<style>: a literal "</script" would end the element early.
        return path.read_text(encoding="utf-8").replace("</script", "<\\/script").replace("</style", "<\\/style")

    def view(self, res: dict[str, Any], question: str, summary: str, model: str) -> dict[str, Any]:
        """Everything the template shows, from a data-question result (single or dashboard)."""
        kind = "dashboard" if res.get("panels") else "report"
        kpis: list[dict[str, Any]] = []
        panels: list[dict[str, Any]] = []
        if kind == "dashboard":
            for p in res["panels"]:
                if p["kind"] == "kpi":
                    kpis.append({"label": p["title"], "value": fmt(p["value"])})
                else:
                    panels.append({"title": p["title"], "kind": p["kind"], "chart": p.get("chart"),
                                   "columns": p["columns"], "rows": p["rows"], "row_count": p["row_count"],
                                   "truncated": p["truncated"], "sql": p["sql"], "wide": p["kind"] == "table"})
        else:
            cols, rows, chart = res["columns"], res["rows"], res.get("chart")
            kpis.append({"label": "Rows", "value": fmt(len(rows)) + ("+" if res.get("truncated") else "")})
            if chart:
                vals = [v[0] for v in chart["values"] if _num(v[0])]
                total = sum(vals)
                kpis.append({"label": chart["label"] + " values", "value": fmt(len(chart["categories"]))})
                kpis.append({"label": "Total " + chart["series"][0], "value": fmt(total)})
                if vals and total:
                    i = max(range(len(vals)), key=lambda k: vals[k])
                    kpis.append({"label": "Largest", "value": f"{vals[i] / total * 100:.0f}%", "sub": chart["categories"][i]})
                ct = chart["title"] or "Chart"
                ct = ct[:1].upper() + ct[1:] if not ct[1:2].isupper() else ct
                panels.append({"title": ct, "kind": chart["type"], "chart": {**chart, "title": ct}})
                if chart["type"] in ("pie", "donut") and len(chart["categories"]) > 1:
                    panels.append({"title": ct + " (ranked)", "kind": "bar",
                                   "chart": {**chart, "type": "bar", "title": ct + " (ranked)"}})
            panels.append({"title": "Data", "kind": "table", "columns": cols, "rows": rows, "row_count": len(rows),
                           "truncated": res.get("truncated"), "sql": res["sql"], "wide": True})
        files = res.get("files") or []
        return {
            "kind": kind, "title": report_title(question, "Data report", kind == "dashboard"), "question": question,
            "source": res.get("source_title") or res.get("source"), "files": files, "tables": res.get("tables") or [],
            "generated": time.strftime("%Y-%m-%d %H:%M %Z"), "model": model, "summary": summary or "",
            "kpis": kpis, "panels": panels, "skipped": res.get("skipped") or [],
            "queries": [{"title": p["title"], "sql": p["sql"]} for p in (res.get("panels") or [{"title": "Query", "sql": res["sql"]}])],
        }

    def create(self, res: dict[str, Any], question: str, summary: str, model: str, chat_id: str = "") -> dict[str, Any]:
        v = self.view(res, question, summary, model)
        html = self.templates.get_template("report.html").render(
            r=v, data={"kpis": v["kpis"], "panels": v["panels"], "summary": v["summary"]},
            css=self._asset(self.shared / "app.css"), report_css=self._asset(self.static / "report.css"),
            echarts=self._asset(self.shared / "vendor" / "echarts" / "echarts.min.js"),
            marked=self._asset(self.shared / "vendor" / "marked" / "marked.umd.js"),
            purify=self._asset(self.shared / "vendor" / "dompurify" / "purify.min.js"),
            charts_js=self._asset(self.static / "charts.js"), report_js=self._asset(self.static / "report.js"))
        self.folder.mkdir(parents=True, exist_ok=True)
        rid = secrets.token_hex(6)
        (self.folder / f"{rid}.html").write_text(html, encoding="utf-8")
        meta = {"id": rid, "title": v["title"], "kind": v["kind"], "source": v["source"], "question": question,
                "panels": len(v["panels"]) + len(v["kpis"]), "created": time.time(), "chat_id": chat_id, "model": model}
        (self.folder / f"{rid}.json").write_text(json.dumps(meta), encoding="utf-8")
        return meta
