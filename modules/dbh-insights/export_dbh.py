"""Export DBH Insights sheets: the XLSX workbook, or one sheet as CSV.

The workbook is a port of collect/js/xlsx.js, so it looks the same as the one the
website builds in the browser:

  Header row     Verdana 9pt bold, white on solid black, left aligned
  Body text      Verdana 9pt
  Integers       #,##0, right aligned        Decimals  #,##0.00, right aligned
  Timestamps     real Excel dates, yyyy/mm/dd hh:mm:ss (UTC, as vCenter reports)
  Booleans       the words True / False
  Freeze panes   first row and first column (pane at B2)
  AutoFilter     whole range, header included
  Sheet order    the inventory tool's order, then the rest

No dependencies beyond the standard library. Contract (vCommunity-Observability
exports): reads the module database named in VCO_DB, writes VCO_OUTPUT_FILE.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import sqlite3
import sys
import zipfile
from datetime import datetime
from typing import Any

SHEET_ORDER = [
    "vInfo", "vCPU", "vMemory", "vDisk", "vPartition", "vNetwork", "vCD", "vUSB", "vSnapshot", "vTools",
    "vSource", "vRP", "vCluster", "vHost", "vHBA", "vNIC", "vSwitch", "vPort", "dvSwitch", "dvPort",
    "vSC_VMK", "vDatastore", "vMultiPath", "vLicense", "vFileInfo", "vHealth", "vMetaData",
]

STYLE = {"header": 1, "text": 2, "integer": 3, "decimal": 4, "date": 5}
INVALID_XML = re.compile("[\u0000-\u0008\u000B\u000C\u000E-\u001F￾￿]")
TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")


# --------------------------------------------------------------------------- read

def load_tables(db_path: str, only: str | None = None) -> list[dict[str, Any]]:
    """Every sheet as {name, columns: [{label, kind}], rows: [[...]]}, values typed
    the way the website holds them (bool, int/float, str, None).

    With several vCenters, `sheets` has one status row per vCenter and sheet, but each
    sheet's table already holds the rows of every vCenter (with its own vCenter column).
    So every table is loaded once: one worksheet per sheet, all vCenters in it."""
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        sheets = db.execute("SELECT MIN(sheet) AS sheet, table_name, MIN(position) AS position FROM sheets "
                            "GROUP BY table_name ORDER BY position, table_name").fetchall()
        meta = db.execute("SELECT * FROM sheet_columns ORDER BY table_name, position").fetchall()
    except sqlite3.OperationalError:
        raise SystemExit("No DBH Insights data yet: run the collector first (Modules page, Run now)")
    columns: dict[str, list[sqlite3.Row]] = {}
    for m in meta:
        columns.setdefault(m["table_name"], []).append(m)

    tables = []
    for s in sheets:
        if only and only.lower() not in (s["sheet"].lower(), s["table_name"]):
            continue
        cols = columns.get(s["table_name"], [])
        names = ", ".join('"' + c["column_name"] + '"' for c in cols)
        rows = []
        for r in db.execute(f'SELECT {names} FROM "{s["table_name"]}" ORDER BY rowid'):
            rows.append([typed(c["kind"], r[i]) for i, c in enumerate(cols)])
        tables.append({"name": s["sheet"], "columns": [{"label": c["label"], "kind": c["kind"]} for c in cols],
                       "rows": rows})
    db.close()
    if only and not tables:
        raise SystemExit(f"Unknown sheet '{only}'")
    return tables


def typed(kind: str, value: Any) -> Any:
    if value is None:
        return None
    if kind == "bool":
        return bool(value)
    if kind == "number" and isinstance(value, float) and value.is_integer():
        return int(value)
    return value


# --------------------------------------------------------------------------- xlsx

def esc(value: Any) -> str:
    s = INVALID_XML.sub("", str(value))
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def column_letter(index: int) -> str:
    n, s = index + 1, ""
    while n > 0:
        m = (n - 1) % 26
        s = chr(65 + m) + s
        n = (n - 1) // 26
    return s


def excel_date(text: str) -> float | None:
    if not isinstance(text, str) or not TIMESTAMP.match(text):
        return None
    try:
        d = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d.timestamp() / 86400 + 25569


def column_format(table: dict[str, Any], i: int) -> str:
    """A column is formatted uniformly, decided from all of its values."""
    values = [r[i] for r in table["rows"] if r[i] is not None and r[i] != ""]
    kind = table["columns"][i]["kind"]
    if kind == "number":
        return "integer" if all(not isinstance(v, float) or v.is_integer() for v in values) else "decimal"
    if kind == "text" and values and all(isinstance(v, str) and excel_date(v) is not None for v in values):
        return "date"
    return "text"


def inline_string(ref: str, style: int, text: Any) -> str:
    return f'<c r="{ref}" s="{style}" t="inlineStr"><is><t xml:space="preserve">{esc(text)}</t></is></c>'


def sheet_xml(table: dict[str, Any]) -> str:
    cols = table["columns"]
    letters = [column_letter(i) for i in range(len(cols))]
    formats = [column_format(table, i) for i in range(len(cols))]
    last_ref = f"{letters[-1] if letters else 'A'}{len(table['rows']) + 1}"

    widths = []
    for i, c in enumerate(cols):
        width = len(c["label"])
        for row in table["rows"][:2000]:
            v = row[i]
            length = 0 if v is None else 5 if isinstance(v, bool) else 19 if formats[i] == "date" else len(str(v))
            width = max(width, length)
        widths.append(min(60, width + 2))

    out = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">',
        f'<dimension ref="A1:{last_ref}"/>',
        '<sheetViews><sheetView workbookViewId="0"><pane xSplit="1" ySplit="1" topLeftCell="B2" activePane="bottomRight" state="frozen"/>'
        '<selection pane="topRight"/><selection pane="bottomLeft"/><selection pane="bottomRight" activeCell="B2" sqref="B2"/></sheetView></sheetViews>',
        '<sheetFormatPr defaultRowHeight="12"/>',
        "<cols>" + "".join(f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>' for i, w in enumerate(widths)) + "</cols>",
        "<sheetData>",
        '<row r="1">' + "".join(inline_string(f"{letters[i]}1", STYLE["header"], c["label"]) for i, c in enumerate(cols)) + "</row>",
    ]
    for r, row in enumerate(table["rows"]):
        num = r + 2
        cells = []
        for i, v in enumerate(row):
            ref = f"{letters[i]}{num}"
            if v is None or v == "":
                cells.append(f'<c r="{ref}" s="{STYLE["text"]}"/>')
            elif isinstance(v, bool):
                cells.append(inline_string(ref, STYLE["text"], "True" if v else "False"))
            elif isinstance(v, (int, float)):
                if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):
                    cells.append(f'<c r="{ref}" s="{STYLE["text"]}"/>')
                else:
                    style = STYLE["integer"] if formats[i] == "integer" else STYLE["decimal"]
                    value = int(v) if formats[i] == "integer" and float(v).is_integer() else v
                    cells.append(f'<c r="{ref}" s="{style}"><v>{value}</v></c>')
            elif formats[i] == "date":
                cells.append(f'<c r="{ref}" s="{STYLE["date"]}"><v>{excel_date(v)}</v></c>')
            else:
                cells.append(inline_string(ref, STYLE["text"], v))
        out.append(f'<row r="{num}">' + "".join(cells) + "</row>")
    out += ["</sheetData>", f'<autoFilter ref="A1:{last_ref}"/>', "</worksheet>"]
    return "".join(out)


STYLES_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    '<numFmts count="1"><numFmt numFmtId="164" formatCode="yyyy/mm/dd hh:mm:ss"/></numFmts>'
    '<fonts count="2">'
    '<font><sz val="9"/><color rgb="FF000000"/><name val="Verdana"/><family val="2"/></font>'
    '<font><b/><sz val="9"/><color rgb="FFFFFFFF"/><name val="Verdana"/><family val="2"/></font>'
    "</fonts>"
    '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
    '<fill><patternFill patternType="solid"><fgColor rgb="FF000000"/><bgColor indexed="64"/></patternFill></fill></fills>'
    '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="6">'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1" applyAlignment="1"><alignment horizontal="left"/></xf>'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
    '<xf numFmtId="3" fontId="0" fillId="0" borderId="0" xfId="0" applyFont="1" applyNumberFormat="1" applyAlignment="1"><alignment horizontal="right"/></xf>'
    '<xf numFmtId="4" fontId="0" fillId="0" borderId="0" xfId="0" applyFont="1" applyNumberFormat="1" applyAlignment="1"><alignment horizontal="right"/></xf>'
    '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyFont="1" applyNumberFormat="1"/>'
    "</cellXfs>"
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    "</styleSheet>"
)


def workbook(tables: list[dict[str, Any]]) -> bytes:
    by_name: dict[str, dict[str, Any]] = {}
    for t in tables:
        by_name.setdefault(t["name"], t)   # one worksheet per name
    ordered = [by_name[n] for n in SHEET_ORDER if n in by_name] + \
              [t for n, t in by_name.items() if n not in SHEET_ORDER]

    # Excel needs unique worksheet names of at most 31 characters (case-insensitive).
    unique: dict[int, str] = {}
    used: set[str] = set()
    for t in ordered:
        base = t["name"][:31] or "Sheet"
        name, k = base, 2
        while name.lower() in used:
            suffix = f" ({k})"
            name, k = base[: 31 - len(suffix)] + suffix, k + 1
        used.add(name.lower())
        unique[id(t)] = name

    files = {
        "[Content_Types].xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            + "".join(f'<Override PartName="/xl/worksheets/sheet{i + 1}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                      for i in range(len(ordered)))
            + "</Types>",
        "_rels/.rels":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>",
        "xl/workbook.xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            "<bookViews><workbookView/></bookViews><sheets>"
            + "".join(f'<sheet name="{esc(unique[id(t)])}" sheetId="{i + 1}" r:id="rId{i + 1}"/>' for i, t in enumerate(ordered))
            + "</sheets><definedNames>"
            + "".join(
                f'<definedName name="_xlnm._FilterDatabase" localSheetId="{i}" hidden="1">'
                f"'{esc(unique[id(t)])}'!$A$1:${column_letter(max(0, len(t['columns']) - 1))}${len(t['rows']) + 1}</definedName>"
                for i, t in enumerate(ordered))
            + "</definedNames></workbook>",
        "xl/_rels/workbook.xml.rels":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(f'<Relationship Id="rId{i + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i + 1}.xml"/>'
                      for i in range(len(ordered)))
            + f'<Relationship Id="rId{len(ordered) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
            "</Relationships>",
        "xl/styles.xml": STYLES_XML,
    }
    for i, t in enumerate(ordered):
        files[f"xl/worksheets/sheet{i + 1}.xml"] = sheet_xml(t)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


# --------------------------------------------------------------------------- csv

def csv_bytes(table: dict[str, Any]) -> bytes:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow([c["label"] for c in table["columns"]])
    for row in table["rows"]:
        w.writerow(["" if v is None else ("True" if v else "False") if isinstance(v, bool) else v for v in row])
    return ("﻿" + out.getvalue()).encode("utf-8")   # BOM so Excel reads UTF-8


def main() -> None:
    db_path = os.environ.get("VCO_DB")
    target = os.environ.get("VCO_OUTPUT_FILE")
    if not db_path or not target:
        raise SystemExit("VCO_DB and VCO_OUTPUT_FILE must be set")
    params = json.loads(os.environ.get("VCO_PARAMS") or "{}")
    if os.environ.get("VCO_EXPORT") == "csv":
        if not params.get("sheet"):
            raise SystemExit("Add ?sheet=<name>, for example ?sheet=vInfo")
        body = csv_bytes(load_tables(db_path, params["sheet"])[0])
    else:
        body = workbook(load_tables(db_path))
    with open(target, "wb") as fh:
        fh.write(body)


if __name__ == "__main__":
    sys.exit(main())
