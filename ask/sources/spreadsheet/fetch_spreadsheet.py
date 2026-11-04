"""spreadsheet source plug-in: read .xlsx / .csv files (RVTools, the dashboards'
DBH Insights export or any table-shaped workbook) and

  1. print one JSON document per row (for the searchable sheets) plus a summary
     document per sheet and per cluster or host, for the normal search path;
  2. write every sheet to <work folder>/tables.db, one SQLite table per sheet, for
     data questions (counts, totals, top N), which Ask answers with a read-only SELECT.

Files come from a folder (the dashboards' exports, mounted read-only at /exports)
or from the upload folder the Knowledge sources page fills."""

from __future__ import annotations

import csv
import datetime as dt
import fnmatch
import hashlib
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Iterator

# Main sheets of RVTools and DBH Insights that are worth one searchable document per row.
# The rest (vDisk, vNetwork, vPartition ...) go to the data tables only, unless listed
# in the source's "Searchable sheets" option.
KNOWN_SHEETS = {"vinfo", "vcpu", "vmemory", "vdisk", "vpartition", "vnetwork", "vcd", "vusb", "vsnapshot",
                "vtools", "vsource", "vrp", "vcluster", "vhost", "vhba", "vnic", "vswitch", "vport",
                "dvswitch", "dvport", "vsc_vmk", "vdatastore", "vmultipath", "vlicense", "vfileinfo",
                "vhealth", "vmetadata", "cluster capacity", "vhost performance", "vinfo performance"}
AUTO_EMBED = {"vinfo", "vhost", "vdatastore", "vcluster", "vsnapshot", "vrp", "vsource", "vlicense",
              "vhealth", "dvswitch", "cluster capacity"}
# Column that names a row, first match wins.
NAME_COLUMNS = ["VM", "Host", "Name", "Cluster", "Resource pool", "Switch", "Port Group", "Datastore"]
# What the common RVTools / DBH Insights columns mean, so the model picks the right one.
COLUMN_HELP = {
    "powerstate": "poweredOn, poweredOff or suspended",
    "template": "True when the VM is a template",
    "cpus": "number of vCPUs",
    "memory": "configured memory in MiB",
    "provisioned mib": "disk space provisioned to the VM, MiB (includes thin-provisioned space not yet used)",
    "in use mib": "disk space the VM actually uses, MiB",
    "provisioned gib": "disk space provisioned to the VM, GiB (includes thin-provisioned space not yet used)",
    "in use gib": "disk space the VM actually uses, GiB",
    "cpu usage (%)": "CPU usage percent at collection time",
    "memory usage (%)": "memory usage percent at collection time",
    "os according to the configuration file": "guest OS set in the VM configuration",
    "os according to the vmware tools": "guest OS reported by VMware Tools (empty when Tools isn't running)",
    "host": "ESXi host name",
    "cluster": "cluster name",
    "datacenter": "datacenter name",
    "# cpu": "physical CPU sockets",
    "# cores": "physical CPU cores",
    "# memory": "physical memory, MiB",
    "# memory gib": "physical memory, GiB",
    "# vms": "number of VMs",
    "# vcpus": "number of vCPUs of the VMs",
    "vcpus per core": "vCPU to physical core ratio",
    "capacity mib": "capacity in MiB",
    "capacity gib": "capacity in GiB",
    "free mib": "free space in MiB",
    "free gib": "free space in GiB",
    "free %": "free space percent",
    "in maintenance mode": "True when the host is in maintenance mode",
    "esx version": "ESXi version and build",
    "size mib (total)": "snapshot size in MiB",
    "date / time": "when the snapshot was taken",
    "vi sdk server": "vCenter the row came from",
}
TS_SUFFIX = re.compile(r"[-_ ]?\d{8}[-_]?\d{4,6}$")
MAX_TEXT_CELL = 500


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def as_list(v: Any) -> list[str]:
    if isinstance(v, str):
        v = v.splitlines()
    return [str(x).strip() for x in (v or []) if str(x).strip()]


def ident(text: str, taken: set[str]) -> str:
    """SQL-friendly lower-case name: '# CPU' -> num_cpu, 'CPU Usage (%)' -> cpu_usage_pct."""
    s = str(text).strip().lower().replace("#", " num ").replace("%", " pct ").replace("/", " ")
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_") or "col"
    if s[0].isdigit():
        s = "c_" + s
    base, n = s, 2
    while s in taken:
        s, n = f"{base}_{n}", n + 1
    taken.add(s)
    return s


def cell(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (dt.datetime, dt.date, dt.time)):
        return v.isoformat(sep=" ") if isinstance(v, dt.datetime) else v.isoformat()
    if isinstance(v, bool):
        return "True" if v else "False"
    if isinstance(v, float) and v.is_integer() and abs(v) < 2 ** 53:
        return int(v)
    if isinstance(v, str):
        v = v.strip()
        return v or None
    return v


def to_number(v: Any) -> Any:
    if isinstance(v, str) and re.fullmatch(r"-?\d+(\.\d+)?", v.replace(",", "")):
        f = float(v.replace(",", ""))
        return int(f) if f.is_integer() else f
    return v


# -- reading -------------------------------------------------------------------
def read_xlsx(path: Path) -> Iterator[tuple[str, list[str], list[list[Any]]]]:
    import openpyxl  # loaded here so the other plug-ins never need it
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        for ws in wb.worksheets:
            rows = ws.iter_rows(values_only=True)
            header: list[str] = []
            # The header is the first row with at least two filled cells (skips title rows).
            for _ in range(10):
                first = next(rows, None)
                if first is None:
                    break
                if sum(1 for c in first if c not in (None, "")) >= 2:
                    header = [str(c).strip() if c not in (None, "") else "" for c in first]
                    break
            if not header:
                continue
            data = []
            for r in rows:
                vals = [cell(c) for c in r[:len(header)]]
                if any(v is not None for v in vals):
                    data.append(vals + [None] * (len(header) - len(vals)))
            yield ws.title, header, data
    finally:
        wb.close()


def read_csv(path: Path) -> Iterator[tuple[str, list[str], list[list[Any]]]]:
    raw = path.read_bytes()
    text = raw.decode("utf-8-sig", errors="replace")
    dialect = csv.excel
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        pass
    rows = list(csv.reader(text.splitlines(), dialect))
    if not rows:
        return
    header = [h.strip() for h in rows[0]]
    data = [[to_number(cell(c)) for c in r[:len(header)]] + [None] * (len(header) - len(r)) for r in rows[1:] if any(c.strip() for c in r)]
    yield TS_SUFFIX.sub("", path.stem), header, data


def pick_files(opts: dict[str, Any], upload_dir: Path) -> list[Path]:
    if opts.get("location") == "upload":
        folder, patterns = upload_dir, ["*.xlsx", "*.csv"]
    else:
        folder, patterns = Path(opts.get("path") or "/exports"), as_list(opts.get("pattern")) or ["*.xlsx"]
    if not folder.is_dir() and opts.get("location") == "upload":
        return []  # nothing uploaded yet
    if not folder.is_dir():
        raise SystemExit(f"Folder {folder} not found. For the dashboards' exports, check that docker-compose.yml "
                         "mounts ./exports:/exports:ro on vco-ask.")
    files = [p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in (".xlsx", ".csv")
             and not p.name.startswith(("~$", "."))
             and any(fnmatch.fnmatch(p.name, pat) for pat in patterns)]
    if opts.get("pick", "newest") == "newest":
        newest: dict[str, Path] = {}
        for p in files:
            key = TS_SUFFIX.sub("", p.stem).lower() + p.suffix.lower()
            if key not in newest or p.stat().st_mtime > newest[key].stat().st_mtime:
                newest[key] = p
        files = list(newest.values())
    return sorted(files)


# -- documents -------------------------------------------------------------------
def doc_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def fmt(v: Any) -> str:
    s = str(v)
    return s if len(s) <= MAX_TEXT_CELL else s[:MAX_TEXT_CELL] + "…"


def num(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def summaries(sheet: str, header: list[str], rows: list[list[Any]], file_label: str, table: str) -> list[dict[str, Any]]:
    """Per cluster (or host) totals for VM sheets: VM count, powered on, vCPUs, memory."""
    idx = {h.lower(): i for i, h in enumerate(header)}
    group_col = next((c for c in ("cluster", "host") if c in idx), None)
    if sheet.lower() != "vinfo" or not group_col:
        return []
    groups: dict[str, dict[str, float]] = {}
    for r in rows:
        if "template" in idx and str(r[idx["template"]]).lower() == "true":
            continue
        g = str(r[idx[group_col]] or "(none)")
        t = groups.setdefault(g, {"vms": 0, "on": 0, "cpus": 0, "mem": 0})
        t["vms"] += 1
        if "powerstate" in idx and r[idx["powerstate"]] == "poweredOn":
            t["on"] += 1
        if "cpus" in idx:
            t["cpus"] += num(r[idx["cpus"]])
        if "memory" in idx:
            t["mem"] += num(r[idx["memory"]])
    docs = []
    label = group_col.title()
    for g, t in sorted(groups.items()):
        text = (f"# {label} {g}: VM summary\n\n"
                f"Totals from the {sheet} sheet of {file_label} (templates not counted).\n\n"
                f"- **VMs:** {int(t['vms'])} ({int(t['on'])} powered on, {int(t['vms'] - t['on'])} not powered on)\n"
                f"- **vCPUs:** {int(t['cpus'])}\n"
                f"- **Configured memory:** {t['mem'] / 1024:.1f} GiB\n\n"
                f"For other totals, ask a data question (table `{table}`).")
        docs.append({"id": f"summary/{label.lower()}/{g}", "title": f"{label} {g} · VM summary", "path": file_label,
                     "url": "", "text": text, "status": "", "meta": {"kind": "summary", "sheet": sheet},
                     "links": {}, "images": [], "hash": doc_hash(text)})
    return docs


# -- main --------------------------------------------------------------------------
def main() -> None:
    opts = json.loads(os.environ.get("ASK_SOURCE_OPTIONS") or "{}")
    work = Path(os.environ.get("ASK_SOURCE_WORKDIR") or ".")
    upload_dir = Path(os.environ.get("ASK_SOURCE_UPLOAD_DIR") or work / "uploads")
    prev = json.loads(os.environ.get("ASK_SOURCE_STATE") or "{}")
    work.mkdir(parents=True, exist_ok=True)

    files = pick_files(opts, upload_dir)
    signature = hashlib.sha256(json.dumps(
        {"opts": opts, "files": [[p.name, p.stat().st_size, int(p.stat().st_mtime)] for p in files]},
        sort_keys=True).encode()).hexdigest()
    tables_db = work / "tables.db"
    newest = max((p.stat().st_mtime for p in files), default=0)
    version = f"{len(files)} file{'s' if len(files) != 1 else ''}" + (
        f" · {dt.datetime.fromtimestamp(newest).strftime('%Y-%m-%d %H:%M')}" if newest else "")
    if prev.get("signature") == signature and tables_db.exists():
        log(f"no changes ({version})")
        print(json.dumps({"unchanged": True, "version": version, "state": prev}))
        return
    if not files:
        log("no matching files" + (" uploaded yet" if opts.get("location") == "upload" else f" in {opts.get('path') or '/exports'}"))

    only = {s.lower() for s in as_list(opts.get("sheets"))}
    embed_list = {s.lower() for s in as_list(opts.get("embed_sheets"))}
    max_rows = int(float(opts.get("max_embed_rows") or 5000))

    # Build the tables in a new file, then swap it in, so data questions never see a half-written file.
    tmp = work / "tables.db.part"
    tmp.unlink(missing_ok=True)
    tdb = sqlite3.connect(tmp)
    tables: dict[str, dict[str, Any]] = {}
    documents: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    multi = len(files) > 1

    for path in files:
        file_label = path.name
        reader = read_csv if path.suffix.lower() == ".csv" else read_xlsx
        try:
            sheets = list(reader(path))
        except Exception as e:  # a broken or password-protected file: skip it, keep the rest
            log(f"skipped {path.name}: {e.__class__.__name__}: {e}")
            continue
        workbook_known = any(s.lower() in KNOWN_SHEETS for s, _, _ in sheets)
        for sheet, header, rows in sheets:
            if only and sheet.lower() not in only:
                continue
            # -- data table (all sheets) --
            t = tables.get(sheet.lower())
            if t is None:
                taken: set[str] = {"source_file"}
                cols = [ident(h or f"column {i + 1}", taken) for i, h in enumerate(header)]
                tname = ident(sheet, {x["table"] for x in tables.values()})
                t = {"table": tname, "sheet": sheet, "headers": header, "cols": cols, "rows": 0, "numeric": [True] * len(cols)}
                tables[sheet.lower()] = t
            else:  # same sheet in another file: map by header text, add new columns
                for h in header:
                    if h not in t["headers"]:
                        t["headers"].append(h)
                        t["cols"].append(ident(h, set(t["cols"]) | {"source_file"}))
                        t["numeric"].append(True)
            pos = {h: t["headers"].index(h) for h in header}
            t.setdefault("data", []).extend(
                [(file_label, {pos[h]: v for h, v in zip(header, r)}) for r in rows])
            # -- searchable documents --
            sl = sheet.lower()
            searchable = (sl in embed_list) if embed_list else (sl in AUTO_EMBED if workbook_known else True)
            if not searchable:
                continue
            if len(rows) > max_rows:
                log(f"{sheet}: {len(rows)} rows, more than {max_rows}: data questions only")
                searchable = False
            name_i = next((header.index(c) for c in NAME_COLUMNS if c in header), None)
            if searchable:
                for n, r in enumerate(rows, 1):
                    name = str(r[name_i]) if name_i is not None and r[name_i] is not None else f"row {n}"
                    base = (f"{path.stem}/" if multi else "") + f"{sheet}/{name}"
                    did, k = base, 2
                    while did in seen_ids:
                        did, k = f"{base} ({k})", k + 1
                    seen_ids.add(did)
                    lines = [f"- **{h}:** {fmt(v)}" for h, v in zip(header, r) if h and v not in (None, "")]
                    text = f"# {sheet}: {name}\n\nOne row of the {sheet} sheet in {file_label}.\n\n" + "\n".join(lines)
                    documents.append({"id": did, "title": f"{name} · {sheet}", "path": file_label, "url": "",
                                      "text": text, "status": "", "meta": {"kind": "row", "sheet": sheet, "row": n},
                                      "links": {}, "images": [], "hash": doc_hash(text)})
            documents.extend(d for d in summaries(sheet, header, rows, file_label, t["table"]) if d["id"] not in seen_ids)
            seen_ids.update(d["id"] for d in documents)

    # Write the tables: a column is numeric when every filled value is a number.
    meta_tables = []
    for t in tables.values():
        data = t.pop("data", [])
        width = len(t["cols"])
        for _, vals in data:
            for i, v in vals.items():
                if v is not None and not isinstance(v, (int, float)):
                    t["numeric"][i] = False
        coldefs = ", ".join(f'"{c}" {"REAL" if t["numeric"][i] else "TEXT"}' for i, c in enumerate(t["cols"]))
        tdb.execute(f'CREATE TABLE "{t["table"]}" ("source_file" TEXT, {coldefs})')
        marks = ",".join("?" * (width + 1))
        tdb.executemany(f'INSERT INTO "{t["table"]}" VALUES ({marks})',
                        [[f] + [vals.get(i) for i in range(width)] for f, vals in data])
        t["rows"] = len(data)
        samples: list[list[Any]] = [list(r) for r in tdb.execute(f'SELECT * FROM "{t["table"]}" LIMIT 3')]
        meta_tables.append({
            "table": t["table"], "sheet": t["sheet"], "rows": t["rows"],
            "columns": [{"name": "source_file", "header": "(file the row came from)", "type": "TEXT", "help": ""}] + [
                {"name": c, "header": h, "type": "REAL" if t["numeric"][i] else "TEXT",
                 "help": COLUMN_HELP.get(h.lower(), "")} for i, (c, h) in enumerate(zip(t["cols"], t["headers"]))],
            "samples": samples})
    tdb.commit()
    tdb.close()
    os.replace(tmp, tables_db)

    # One overview document per sheet, so "which sheets are there" finds something.
    for t in meta_tables:
        text = (f"# Sheet {t['sheet']}\n\n{t['rows']} rows, loaded into the data table `{t['table']}` for counting "
                f"and totals questions.\n\nColumns: " + ", ".join(c["header"] for c in t["columns"][1:] if c["header"]))
        documents.append({"id": f"sheet/{t['sheet']}", "title": f"Sheet {t['sheet']} · overview", "path": "",
                          "url": "", "text": text, "status": "", "meta": {"kind": "sheet"}, "links": {},
                          "images": [], "hash": doc_hash(text)})

    log(f"{len(files)} files, {len(meta_tables)} tables, {sum(t['rows'] for t in meta_tables)} rows, "
        f"{len(documents)} searchable documents")
    state = {"signature": signature, "files": [p.name for p in files], "tables_db": str(tables_db),
             "tables": meta_tables}
    print(json.dumps({"version": version, "root": "", "state": state, "documents": documents}, default=str))


if __name__ == "__main__":
    main()
