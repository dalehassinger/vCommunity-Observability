"""openapi source plug-in: turn OpenAPI 3 / Swagger 2 API specs (JSON or YAML) into
markdown documents for the vCommunity Ask indexer.

A raw spec is a poor search target: one huge JSON file where every endpoint is buried
in nested schemas. So each API operation becomes its own short document:

    # GET /api/alerts/{id} - Look up an alert
    what it does, area (tag), operationId
    ## Request: base path, auth, parameters, request body fields, JSON example
    ## Response: status codes and the response fields
    ## PowerShell: an Invoke-RestMethod call

plus one overview document per spec with every area and its operations, for questions
such as "which APIs are there for policies?". $ref schemas are expanded a few levels
deep (with a guard against loops), HTML in descriptions becomes plain text.

Everything runs offline; YAML needs PyYAML (in the Ask image)."""

from __future__ import annotations

import fnmatch
import hashlib
import html
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

METHODS = ("get", "post", "put", "patch", "delete", "head", "options")
MAX_FIELDS = 60          # field lines per schema
MAX_DEPTH = 3            # nesting levels expanded per schema
MAX_EXAMPLE = 1500       # characters of a JSON example
SPEC_EXT = {".json", ".yaml", ".yml"}


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def as_list(v: Any) -> list[str]:
    if v is None or v == "":
        return []
    if isinstance(v, str):
        return [s.strip() for s in v.splitlines() if s.strip()]
    return [str(s).strip() for s in v if str(s).strip()]


def plain(text: Any, limit: int = 0) -> str:
    """HTML from the spec as plain text (keeps <code> as `code`, line breaks as spaces)."""
    if not text:
        return ""
    t = str(text)
    t = re.sub(r"(?is)<\s*code\s*>(.*?)<\s*/\s*code\s*>", lambda m: "`" + m.group(1).strip() + "`", t)
    t = re.sub(r"(?i)<\s*br\s*/?\s*>|</p>|<p>|</li>", " ", t)
    t = re.sub(r"(?i)<li>", " - ", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t)
    t = re.sub(r"[ \t\r\n]+", " ", t).strip()
    # A line starting with '#' would become a heading and split the chunk.
    t = re.sub(r"^#+", "", t)
    if limit and len(t) > limit:
        t = t[:limit].rsplit(" ", 1)[0] + "…"
    return t


def load_spec(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".json" or raw.lstrip().startswith("{"):
        return json.loads(raw)
    import yaml  # PyYAML, only needed for YAML specs
    return yaml.safe_load(raw)


class Spec:
    def __init__(self, d: dict, name: str):
        self.d = d
        self.name = name
        self.v3 = str(d.get("openapi", "")).startswith("3")
        info = d.get("info") or {}
        self.title = plain(info.get("title")) or name
        self.version = str(info.get("version") or "")
        if self.v3:
            servers = d.get("servers") or []
            self.base = (servers[0].get("url") if servers and isinstance(servers[0], dict) else "") or ""
            self.schemes = (d.get("components") or {}).get("securitySchemes") or {}
        else:
            self.base = (d.get("basePath") or "").rstrip("/")
            self.schemes = d.get("securityDefinitions") or {}
        self.security = d.get("security") or []
        self.tag_text = {t.get("name"): plain(t.get("description"), 300) for t in d.get("tags") or [] if isinstance(t, dict)}

    # -- $ref and schemas --------------------------------------------------------
    def ref(self, r: str) -> Any:
        if not r.startswith("#/"):
            return {}
        node: Any = self.d
        for part in r[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(node, dict) or part not in node:
                return {}
            node = node[part]
        return node

    def resolve(self, s: Any, seen: tuple = ()) -> tuple[dict, str, tuple]:
        """(schema, ref name, seen refs) with $ref followed."""
        name = ""
        while isinstance(s, dict) and "$ref" in s:
            r = s["$ref"]
            if r in seen:
                return {}, r.rsplit("/", 1)[-1], seen
            seen = seen + (r,)
            name = r.rsplit("/", 1)[-1]
            s = self.ref(r)
        return (s if isinstance(s, dict) else {}), name, seen

    def type_of(self, s: Any, seen: tuple = ()) -> str:
        s, name, seen = self.resolve(s, seen)
        if not s:
            return name or "object"
        if "allOf" in s or "oneOf" in s or "anyOf" in s:
            key = "allOf" if "allOf" in s else ("oneOf" if "oneOf" in s else "anyOf")
            parts = [self.type_of(x, seen) for x in s[key][:4]]
            return name or (" or ".join(parts) if key != "allOf" else " + ".join(parts))
        t = s.get("type") or ("object" if "properties" in s else "")
        if t == "array":
            return f"array of {self.type_of(s.get('items') or {}, seen)}"
        if name and t in ("object", ""):
            return name
        fmt = s.get("format")
        enum = s.get("enum")
        out = t or name or "any"
        if fmt:
            out += f" ({fmt})"
        if enum:
            vals = ", ".join(str(e) for e in enum[:12]) + (", …" if len(enum) > 12 else "")
            out += f": one of {vals}"
        return out

    def props(self, s: Any, seen: tuple = ()) -> tuple[dict, list]:
        """Properties and required list of an object schema (allOf merged)."""
        s, _, seen = self.resolve(s, seen)
        props: dict = {}
        req: list = list(s.get("required") or [])
        for sub in s.get("allOf") or []:
            p, r = self.props(sub, seen)
            props.update(p)
            req += r
        props.update(s.get("properties") or {})
        return props, req

    def fields(self, s: Any, prefix: str = "", depth: int = 0, seen: tuple = (), out: list | None = None) -> list[str]:
        """Field lines: "- `a.b` (type, required): description"."""
        out = [] if out is None else out
        s2, _, seen2 = self.resolve(s, seen)
        if s2.get("type") == "array" and not prefix:
            return self.fields(s2.get("items") or {}, "[]", depth, seen2, out)
        props, req = self.props(s2, seen)
        for key, val in props.items():
            if len(out) >= MAX_FIELDS:
                out.append("- … more fields in the spec")
                return out
            v, _, vseen = self.resolve(val, seen2)
            path = f"{prefix}.{key}" if prefix else key
            desc = plain(v.get("description") or (val.get("description") if isinstance(val, dict) else ""), 200)
            flags = ", required" if key in req else ""
            flags += ", read-only" if v.get("readOnly") else ""
            out.append(f"- `{path}` ({self.type_of(val, seen2)}{flags})" + (f": {desc}" if desc else ""))
            if depth + 1 < MAX_DEPTH:
                inner = v.get("items") if v.get("type") == "array" else v
                inner_s, _, inner_seen = self.resolve(inner or {}, vseen)
                if self.props(inner_s, inner_seen)[0]:
                    self.fields(inner_s, path + ("[]" if v.get("type") == "array" else ""), depth + 1, inner_seen, out)
        return out

    # -- one operation -----------------------------------------------------------
    def parameters(self, path_item: dict, op: dict) -> list[dict]:
        merged: dict = {}
        for p in (path_item.get("parameters") or []) + (op.get("parameters") or []):
            p, _, _ = self.resolve(p)
            if p.get("name"):
                merged[(p.get("in"), p["name"])] = p
        return list(merged.values())

    def body(self, op: dict, params: list[dict]) -> tuple[str, Any, Any]:
        """(content type, schema, example) of the request body."""
        if self.v3:
            rb, _, _ = self.resolve(op.get("requestBody") or {})
            return self.pick_content(rb.get("content") or {})
        for p in params:
            if p.get("in") == "body":
                return "application/json", p.get("schema") or {}, None
        return "", None, None

    @staticmethod
    def pick_content(content: dict) -> tuple[str, Any, Any]:
        if not content:
            return "", None, None
        ctype = next((c for c in content if "json" in c), next(iter(content)))
        c = content[ctype] or {}
        ex = c.get("example")
        if ex is None and c.get("examples"):
            first = next(iter(c["examples"].values()), {})
            ex = first.get("value") if isinstance(first, dict) else None
        return ctype, c.get("schema"), ex

    def responses(self, op: dict) -> list[tuple[str, str, Any]]:
        out = []
        for code, r in (op.get("responses") or {}).items():
            r, _, _ = self.resolve(r)
            if self.v3:
                _, schema, _ = self.pick_content(r.get("content") or {})
            else:
                schema = r.get("schema")
            out.append((str(code), plain(r.get("description"), 200), schema))
        return out

    def auth_line(self, op: dict) -> str:
        sec = op.get("security", self.security)
        if not sec:
            return "none"
        names = []
        for req in sec:
            for n in req or {}:
                s = self.schemes.get(n) or {}
                where = f"{s.get('in', '')} `{s.get('name')}`" if s.get("name") else s.get("scheme") or s.get("type", "")
                names.append(f"{n} ({where.strip()})" if where else n)
        return ", ".join(dict.fromkeys(names)) or "none"

    def document(self, method: str, path: str, path_item: dict, op: dict, base_url: str) -> dict:
        summary = plain(op.get("summary"), 160)
        desc = plain(op.get("description"), 1500)
        if not summary:
            summary = desc.split(". ")[0][:120] if desc else ""
        tags = op.get("tags") or ["Other"]
        title = f"{method.upper()} {path}" + (f" - {summary}" if summary else "")
        params = self.parameters(path_item, op)
        lines = [f"# {title}", "",
                 f"API: {self.title} {self.version} · Area: {', '.join(tags)}"
                 + (f" · operationId `{op['operationId']}`" if op.get("operationId") else "")
                 + (" · **deprecated**" if op.get("deprecated") else ""), ""]
        if desc and desc != summary:
            lines += [desc, ""]
        full_path = (self.base.rstrip("/") + path) if self.base.startswith("/") or not self.base else path
        lines += ["## Request", "", f"`{method.upper()} {full_path}`" + (f" (server base path `{self.base}`)" if self.base else ""),
                  f"Authentication: {self.auth_line(op)}", ""]
        shown = [p for p in params if p.get("in") != "body"]
        if shown:
            lines.append("Parameters:")
            for p in shown:
                ptype = self.type_of(p.get("schema") or {k: p[k] for k in ("type", "format", "enum", "items") if k in p})
                d = plain(p.get("description"), 200)
                req = ", required" if p.get("required") else ""
                default = (p.get("schema") or {}).get("default", p.get("default"))
                dflt = f", default {default}" if default not in (None, "") else ""
                lines.append(f"- `{p['name']}` ({p.get('in')}, {ptype}{req}{dflt})" + (f": {d}" if d else ""))
            lines.append("")
        ctype, bschema, bex = self.body(op, params)
        if bschema or bex:
            # Own section, so a long field list is its own chunk (headed with the call's title).
            lines += ["## Request body", "", f"Content type {ctype}" + (f", schema {self.type_of(bschema)}" if bschema else "") + ":"]
            if bschema:
                lines += self.fields(bschema) or [f"- {self.type_of(bschema)}"]
            if bex is not None:
                ex = bex if isinstance(bex, str) else json.dumps(bex, indent=2)
                if len(ex) > MAX_EXAMPLE:
                    ex = ex[:MAX_EXAMPLE] + "\n…"
                lines += ["", "Example body:", "", "```json", ex, "```"]
            lines.append("")
        resp = self.responses(op)
        if resp:
            lines += ["## Response", ""]
            for code, d, schema in resp:
                lines.append(f"- **{code}**" + (f" {d}" if d else "") + (f" → {self.type_of(schema)}" if schema else ""))
            ok = next(((c, s) for c, _, s in resp if c.startswith("2") and s), None)
            if ok:
                f = self.fields(ok[1])
                if f:
                    lines += ["", "## Response fields", "", f"Fields of the {ok[0]} response:"] + f
            lines.append("")
        lines += ["## PowerShell", "", "```powershell", self.powershell(method, full_path, params, bool(bschema or bex), base_url), "```"]
        text = "\n".join(lines).strip() + "\n"
        op_id = op.get("operationId") or f"{method}-{path}"
        return {"title": title, "text": text, "tags": tags, "op_id": op_id}

    def powershell(self, method: str, full_path: str, params: list[dict], has_body: bool, base_url: str) -> str:
        server = base_url.rstrip("/") if base_url else "$server"
        url_path = re.sub(r"\{([^}]+)\}", lambda m: "$" + re.sub(r"\W", "", m.group(1)), full_path)
        query = [p["name"] for p in params if p.get("in") == "query" and p.get("required")]
        q = ("?" + "&".join(f"{n}=<{n}>" for n in query)) if query else ""
        if "/api/auth/token/acquire" in (self.d.get("paths") or {}):
            # VCF Operations (and Aria Operations): get an OpsToken first.
            base = self.base.rstrip("/") if self.base.startswith("/") else ""
            out = ['$login = @{ username = "admin"; password = "<password>" } | ConvertTo-Json',
                   f'$token = (Invoke-RestMethod -Method Post -Uri "{server}{base}/api/auth/token/acquire" -Body $login '
                   "-ContentType 'application/json' -Headers @{ Accept = 'application/json' } -SkipCertificateCheck).token",
                   '$headers = @{ Authorization = "OpsToken $token"; Accept = "application/json" }']
        else:
            out = ['$headers = @{ Authorization = "<token>"; Accept = "application/json" }']
        call = f'Invoke-RestMethod -Method {method.capitalize()} -Uri "{server}{url_path}{q}" -Headers $headers -SkipCertificateCheck'
        if has_body:
            out.append("$body = @{ } | ConvertTo-Json -Depth 10   # fields: see Request body above")
            call += " -ContentType 'application/json' -Body $body"
        out.append(call)
        return "\n".join(out)


def main() -> int:
    opts = json.loads(os.environ.get("ASK_SOURCE_OPTIONS") or "{}")
    work = Path(os.environ.get("ASK_SOURCE_WORKDIR") or "work")
    state = json.loads(os.environ.get("ASK_SOURCE_STATE") or "{}")
    full = bool(os.environ.get("ASK_SOURCE_FULL"))

    location = (opts.get("location") or "upload").strip()
    if location == "upload":
        root = Path(os.environ.get("ASK_SOURCE_UPLOAD_DIR") or work / "uploads")
        patterns = ["*.json", "*.yaml", "*.yml"]
    else:
        root = Path((opts.get("path") or "/knowledge").strip())
        patterns = as_list(opts.get("pattern")) or ["**/*openapi*.json"]
    only_tags = {t.lower() for t in as_list(opts.get("tags"))}
    skip_deprecated = (opts.get("deprecated") or "skip") == "skip"
    base_url = (opts.get("base_url") or "").strip()

    if not root.is_dir():
        if location == "upload":
            files: list[Path] = []
        else:
            raise SystemExit(f"Folder {root} does not exist in the container (mount it, e.g. ./ask-knowledge -> /knowledge)")
    else:
        files = sorted({p for pat in patterns for p in root.glob(pat)
                        if p.is_file() and p.suffix.lower() in SPEC_EXT and not p.name.startswith(".")})
    log(f"{len(files)} spec file(s) in {root}")

    opts_hash = hashlib.sha256(json.dumps(opts, sort_keys=True).encode()).hexdigest()[:16]
    signature = hashlib.sha256(json.dumps([opts_hash, [(str(p.relative_to(root)), p.stat().st_size, p.stat().st_mtime_ns)
                                                       for p in files]]).encode()).hexdigest()[:24]
    if not full and files and state.get("signature") == signature:
        log("no change since last sync")
        print(json.dumps({"version": state.get("version", ""), "root": str(root), "unchanged": True,
                          "state": state, "documents": []}))
        return 0

    docs: list[dict[str, Any]] = []
    totals = {"specs": 0, "operations": 0, "skipped_deprecated": 0, "skipped_tags": 0}
    for f in files:
        rel = str(f.relative_to(root))
        try:
            d = load_spec(f)
        except Exception as e:  # noqa: BLE001 - one broken file must not stop the others
            log(f"  {rel}: cannot read ({e.__class__.__name__}: {str(e)[:200]}), skipped")
            continue
        if not isinstance(d, dict) or not isinstance(d.get("paths"), dict) or not (d.get("openapi") or d.get("swagger")):
            log(f"  {rel}: not an OpenAPI or Swagger spec (no openapi/swagger and paths), skipped")
            continue
        spec = Spec(d, f.stem)
        stem = re.sub(r"\.(json|ya?ml)$", "", rel, flags=re.I)
        by_tag: dict[str, list[str]] = {}
        n = 0
        used_ids: set[str] = set()
        for path, item in d["paths"].items():
            if not isinstance(item, dict):
                continue
            for method in METHODS:
                op = item.get(method)
                if not isinstance(op, dict):
                    continue
                if skip_deprecated and op.get("deprecated"):
                    totals["skipped_deprecated"] += 1
                    continue
                tags = op.get("tags") or ["Other"]
                if only_tags and not any(t.lower() in only_tags for t in tags):
                    totals["skipped_tags"] += 1
                    continue
                doc = spec.document(method, path, item, op, base_url)
                op_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", doc["op_id"]).strip("-")[:120] or f"op{n}"
                while op_id in used_ids:
                    op_id += "-x"
                used_ids.add(op_id)
                for t in tags:
                    by_tag.setdefault(t, []).append(f"- `{method.upper()} {path}`" + (f" - {doc['title'].split(' - ', 1)[1]}" if " - " in doc["title"] else ""))
                docs.append({
                    "id": f"{stem}/{op_id}", "title": doc["title"], "path": rel, "url": "",
                    "text": doc["text"],
                    "meta": {"api": spec.title, "api_version": spec.version, "method": method.upper(), "path": path,
                             "tags": tags, "file": rel, "deprecated": bool(op.get("deprecated"))},
                    "hash": hashlib.sha256(doc["text"].encode()).hexdigest(),
                })
                n += 1
        # Overview: every area with its calls, for "what can the API do" questions. One
        # "## <area>" section per tag, so each area is its own chunk.
        ov = [f"# {spec.title} {spec.version} - API overview", "",
              f"{n} API calls in {len(by_tag)} areas. Base path `{spec.base or '/'}`. "
              f"Authentication: {spec.auth_line({}) if spec.security else 'see each call'}.", ""]
        for tag in sorted(by_tag, key=str.lower):
            ov += [f"## {tag} ({len(by_tag[tag])} calls)", ""]
            if spec.tag_text.get(tag):
                ov += [spec.tag_text[tag], ""]
            ov += by_tag[tag] + [""]
        ov_text = "\n".join(ov).strip() + "\n"
        docs.append({"id": f"{stem}/overview", "title": f"{spec.title} {spec.version} - API overview", "path": rel, "url": "",
                     "text": ov_text, "meta": {"api": spec.title, "api_version": spec.version, "file": rel, "overview": True},
                     "hash": hashlib.sha256(ov_text.encode()).hexdigest()})
        totals["specs"] += 1
        totals["operations"] += n
        log(f"  {rel}: {spec.title} {spec.version}, {n} calls in {len(by_tag)} areas")

    version = f"{totals['specs']} spec{'s' if totals['specs'] != 1 else ''}, {totals['operations']} API calls"
    skipped = ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in totals.items() if v and k.startswith("skipped"))
    log(version + (f"; {skipped}" if skipped else ""))
    print(json.dumps({"version": version, "root": str(root), "unchanged": False,
                      "state": {"signature": signature, "version": version}, "documents": docs}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
