"""markdown-repo source plug-in: clone or pull a Git repo (or read a folder) and
print its markdown files as JSON documents for the vCommunity Ask indexer.

Understands the isvara knowledge-base format: a trailing '## Metadata' section
holding a ```yaml block (id, status, related/same_name/... links, images). That
block becomes metadata and is NOT part of the text that gets embedded."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import posixpath
import re
import subprocess
import sys
import time
from pathlib import Path

import yaml

LINK_TYPES = ("same_name", "internal_derive", "same_meaning", "related")
IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
META_RE = re.compile(r"^## Metadata\s*$", re.M)
YAML_BLOCK_RE = re.compile(r"```ya?ml\s*\n(.*?)\n```", re.S)


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """`**` matches any number of folders, `*` anything inside one folder."""
    out, i = "", 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
            continue
        if pattern.startswith("**", i):
            out += ".*"
            i += 2
            continue
        out += "[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c)
        i += 1
    return re.compile(f"^{out}$")


def git(*args: str, cwd: Path | None = None, token: str = "") -> str:
    cmd = ["git"]
    if token:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        cmd += ["-c", f"http.extraHeader=Authorization: Basic {basic}"]
    cmd += list(args)
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=600,
                       env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1"})
    if p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {p.stderr.strip()[-500:]}")
    return p.stdout.strip()


def checkout(repo_url: str, branch: str, work: Path, token: str) -> tuple[Path, str]:
    repo = work / "repo"
    if (repo / ".git").exists():
        current = git("remote", "get-url", "origin", cwd=repo)
        if current.rstrip("/") != repo_url.rstrip("/"):
            log(f"repository URL changed ({current} -> {repo_url}), cloning again")
            subprocess.run(["rm", "-rf", str(repo)], check=True)
    if not (repo / ".git").exists():
        log(f"cloning {repo_url} ({branch})")
        work.mkdir(parents=True, exist_ok=True)
        git("clone", "--depth", "1", "--branch", branch, "--single-branch", repo_url, str(repo), token=token)
    else:
        log(f"pulling {repo_url} ({branch})")
        git("fetch", "--depth", "1", "origin", branch, cwd=repo, token=token)
        git("reset", "--hard", "FETCH_HEAD", cwd=repo)
    return repo, git("rev-parse", "HEAD", cwd=repo)


def default_web_url(repo_url: str, branch: str) -> str:
    m = re.match(r"https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$", repo_url or "")
    return f"https://github.com/{m.group(1)}/{m.group(2)}/blob/{branch}/" if m else ""


def split_metadata(raw: str, mode: str) -> tuple[str, dict]:
    if mode == "none":
        return raw, {}
    matches = list(META_RE.finditer(raw))
    if not matches:
        return raw, {}
    m = matches[-1]
    tail = raw[m.end():]
    block = YAML_BLOCK_RE.search(tail)
    if not block:
        return raw, {}
    try:
        meta = yaml.safe_load(block.group(1)) or {}
    except yaml.YAMLError as e:
        log(f"  bad metadata yaml ignored: {e}")
        return raw[:m.start()].rstrip(), {}
    return raw[:m.start()].rstrip(), meta if isinstance(meta, dict) else {}


def build_document(root: Path, file: Path, opts: dict, web_url: str) -> dict:
    rel = file.relative_to(root).as_posix()
    raw = file.read_text(encoding="utf-8", errors="replace")
    body, meta = split_metadata(raw, opts.get("metadata") or "auto")

    title = next((ln[2:].strip() for ln in body.splitlines() if ln.startswith("# ")), Path(rel).stem)
    doc_id = str(meta.get("id") or rel[:-3] if rel.endswith(".md") else rel)
    links = {t: [str(x) for x in (meta.get(t) or []) if x] for t in LINK_TYPES}
    links = {t: v for t, v in links.items() if v}

    # Images: inline ![alt](path) plus the metadata `images` list; paths resolved
    # relative to the repo root so the app can serve them.
    images: dict[str, dict] = {}
    base = posixpath.dirname(rel)
    for alt, src in IMAGE_RE.findall(body):
        if re.match(r"^[a-z]+://", src):
            continue
        p = posixpath.normpath(posixpath.join(base, src))
        if (root / p).is_file():
            images.setdefault(p, {"path": p, "alt": alt})
    for img in meta.get("images") or []:
        if isinstance(img, dict) and img.get("path"):
            p = posixpath.normpath(posixpath.join(base, img["path"]))
            if (root / p).is_file():
                entry = images.setdefault(p, {"path": p, "alt": ""})
                entry.update({k: v for k, v in img.items() if k != "path"})

    keep = {k: v for k, v in meta.items() if k not in LINK_TYPES and k not in ("images", "id")}
    text = body.strip()
    digest = hashlib.sha256(json.dumps([text, keep, links], sort_keys=True, default=str).encode()).hexdigest()
    return {"id": doc_id, "title": title, "path": rel, "url": (web_url + rel) if web_url else "",
            "text": text, "status": str(meta.get("status") or ""), "meta": keep, "links": links,
            "images": list(images.values()), "hash": digest}


def main() -> int:
    opts = json.loads(os.environ.get("ASK_SOURCE_OPTIONS") or "{}")
    work = Path(os.environ.get("ASK_SOURCE_WORKDIR") or "work")
    state = json.loads(os.environ.get("ASK_SOURCE_STATE") or "{}")
    include = opts.get("include") or ["**/*.md"]
    exclude = opts.get("exclude") or []
    if isinstance(include, str):
        include = [x for x in include.splitlines() if x.strip()]
    if isinstance(exclude, str):
        exclude = [x for x in exclude.splitlines() if x.strip()]
    inc = [glob_to_regex(p.strip()) for p in include]
    exc = [glob_to_regex(p.strip()) for p in exclude]
    opts_hash = hashlib.sha256(json.dumps(opts, sort_keys=True).encode()).hexdigest()[:16]

    repo_url = (opts.get("repo_url") or "").strip()
    branch = (opts.get("branch") or "main").strip()
    if repo_url:
        token = os.environ.get(opts["token_env"], "") if opts.get("token_env") else ""
        root, version = checkout(repo_url, branch, work, token)
        web_url = opts.get("web_url") or default_web_url(repo_url, branch)
    else:
        root = Path(opts.get("path") or "")
        if not str(root) or not root.is_dir():
            raise SystemExit(f"Local folder not found: '{root}'. Set Repository URL or a folder inside the container.")
        newest = max((f.stat().st_mtime for f in root.rglob("*.md")), default=0)
        version = time.strftime("%Y-%m-%d %H:%M", time.gmtime(newest)) if newest else "empty"
        web_url = opts.get("web_url") or ""

    if state.get("version") == version and state.get("options") == opts_hash and not os.environ.get("ASK_SOURCE_FULL"):
        log(f"no change since last sync ({version})")
        print(json.dumps({"version": version, "root": str(root), "unchanged": True,
                          "state": {"version": version, "options": opts_hash}, "documents": []}))
        return 0

    docs, seen = [], set()
    for f in sorted(root.rglob("*")):
        if not f.is_file() or ".git" in f.parts:
            continue
        rel = f.relative_to(root).as_posix()
        if not any(r.match(rel) for r in inc) or any(r.match(rel) for r in exc):
            continue
        try:
            d = build_document(root, f, opts, web_url)
        except Exception as e:  # one bad file must not stop the sync
            log(f"  skipped {rel}: {e}")
            continue
        if d["id"] in seen:
            log(f"  duplicate id {d['id']} in {rel}, using the path as id")
            d["id"] = rel
        seen.add(d["id"])
        docs.append(d)
    log(f"{len(docs)} markdown files at {version}")
    print(json.dumps({"version": version, "root": str(root), "unchanged": False,
                      "state": {"version": version, "options": opts_hash}, "documents": docs}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log(f"ERROR: {e}")
        sys.exit(1)
