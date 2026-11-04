"""Source plug-ins and the indexer.

A plug-in is a folder with source.yaml + a Python or PowerShell script (see
sources/markdown-repo/source.yaml for the contract). The indexer runs the script,
splits each changed document into chunks by '## ' heading, embeds the chunks
with the Ollama embedding model and stores them in SQLite (FTS5 + sqlite-vec)."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from .db import DB
from .ollama import Ollama, OllamaError

log = logging.getLogger("ask.indexer")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
MAX_CHUNK_CHARS = 3500
EMBED_BATCH = 16


@dataclass
class Plugin:
    name: str
    title: str
    description: str
    folder: Path
    runtime: str
    entry: str
    timeout: int
    options: list[dict[str, Any]] = field(default_factory=list)
    version: str = ""

    def command(self) -> list[str]:
        script = str(self.folder / self.entry)
        if self.runtime == "pwsh":
            return ["pwsh", "-NoProfile", "-NonInteractive", "-File", script]
        return [sys.executable, script]


def discover_plugins(dirs: list[Path]) -> dict[str, Plugin]:
    """Later folders win, so a plug-in dropped into /sources overrides the built-in one."""
    found: dict[str, Plugin] = {}
    for d in dirs:
        if not d.is_dir():
            continue
        for manifest in sorted(d.glob("*/source.yaml")):
            try:
                m = yaml.safe_load(manifest.read_text()) or {}
                p = Plugin(name=m["name"], title=m.get("title", m["name"]), description=m.get("description", ""),
                           folder=manifest.parent, runtime=m.get("runtime", "python"), entry=m["entry"],
                           timeout=int(m.get("timeout_seconds", 900)), options=m.get("options") or [],
                           version=str(m.get("version", "")))
                found[p.name] = p
            except Exception as e:
                log.error("bad source plug-in %s: %s", manifest, e)
    return found


# -- chunking ----------------------------------------------------------------
def chunk_markdown(text: str) -> list[dict[str, Any]]:
    """Split on '## ' headings (not inside code fences). The '# Title' intro is kept
    with the first section when it has no text of its own. Long sections are split
    on blank lines."""
    sections: list[tuple[str, list[str]]] = [("", [])]
    fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fence = not fence
        if not fence and line.startswith("## "):
            sections.append((line[3:].strip(), [line]))
        else:
            sections[-1][1].append(line)
    # Merge a title-only intro into the next section.
    intro_body = "\n".join(l for l in sections[0][1] if not l.startswith("# ")).strip()
    if len(sections) > 1 and not intro_body:
        sections = sections[1:]
    out: list[dict[str, Any]] = []
    for heading, lines in sections:
        body = "\n".join(lines).strip()
        if not body:
            continue
        for part in _split_long(body):
            out.append({"ord": len(out), "heading": heading, "text": part})
    return out


def _split_long(body: str) -> list[str]:
    if len(body) <= MAX_CHUNK_CHARS:
        return [body]
    parts, cur = [], ""
    for para in re.split(r"\n\s*\n", body):
        if cur and len(cur) + len(para) > MAX_CHUNK_CHARS:
            parts.append(cur.strip())
            cur = ""
        cur += para + "\n\n"
    if cur.strip():
        parts.append(cur.strip())
    return parts


def embed_text(doc: dict[str, Any], chunk: dict[str, Any], prefix: str) -> str:
    """What gets embedded: the file title and id first, so a chunk is understood
    without its neighbours (the isvara contract asks for this)."""
    head = f"{doc.get('title') or doc['id']} ({doc['id']})"
    if chunk["heading"]:
        head += f" > {chunk['heading']}"
    return f"{prefix}{head}\n{chunk['text']}"


def embed_prefixes(model: str) -> tuple[str, str]:
    """Task prefixes some embedding models are trained with (document, query)."""
    m = model.lower()
    if m.startswith("nomic-embed"):
        return "search_document: ", "search_query: "
    if "mxbai-embed" in m:
        return "", "Represent this sentence for searching relevant passages: "
    return "", ""


# -- indexer -----------------------------------------------------------------
class Indexer:
    def __init__(self, db: DB, data_dir: Path, plugins: Callable[[], dict[str, Plugin]],
                 settings: Callable[[], dict[str, Any]]):
        self.db = db
        self.data_dir = data_dir
        self.plugins = plugins
        self.settings = settings
        self._locks: dict[str, threading.Lock] = {}
        self._again: dict[str, bool] = {}  # sync requested while one was running: run again after it (value = full)

    def workdir(self, name: str) -> Path:
        return self.data_dir / "sources" / name

    def is_running(self, name: str) -> bool:
        lock = self._locks.get(name)
        return bool(lock and lock.locked())

    def start(self, name: str, full: bool = False) -> bool:
        """Run a sync in a background thread. False when one is already running."""
        lock = self._locks.setdefault(name, threading.Lock())
        if lock.locked():
            if full:
                self._again[name] = True
            return False
        threading.Thread(target=self.sync, args=(name, full), daemon=True, name=f"sync-{name}").start()
        return True

    def sync(self, name: str, full: bool = False) -> None:
        lock = self._locks.setdefault(name, threading.Lock())
        if not lock.acquire(blocking=False):
            return
        t0 = time.time()
        try:
            self.db.set_state(name, status="running", progress="starting", last_start=t0, last_error="")
            self._sync(name, full)
            self.db.set_state(name, status="ok", last_end=time.time(), last_ok=time.time())
            log.info("sync %s done in %.1fs", name, time.time() - t0)
        except Exception as e:
            log.exception("sync %s failed", name)
            self.db.set_state(name, status="error", progress="", last_end=time.time(), last_error=str(e)[:2000])
        finally:
            lock.release()
            if name in self._again:
                self.start(name, full=self._again.pop(name))

    def _sync(self, name: str, full: bool) -> None:
        src = self.db.get_source(name)
        if not src:
            raise RuntimeError(f"unknown source {name}")
        plugin = self.plugins().get(src["plugin"])
        if not plugin:
            raise RuntimeError(f"source plug-in '{src['plugin']}' not found")
        cfg = self.settings()
        ollama = Ollama(cfg["ollama_url"])
        model = cfg["embed_model"]
        prev = self.db.get_state(name)
        # Re-embed everything if the embedding model changed since this source was indexed.
        if prev["state"].get("embed_model") not in (None, model):
            full = True

        self.db.set_state(name, progress="fetching")
        work = self.workdir(name)
        work.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "ASK_SOURCE_OPTIONS": json.dumps(src["options"]),
               "ASK_SOURCE_WORKDIR": str(work), "ASK_SOURCE_NAME": name,
               "ASK_SOURCE_STATE": json.dumps({} if full else prev["state"])}
        if full:
            env["ASK_SOURCE_FULL"] = "1"
        p = subprocess.run(plugin.command(), env=env, capture_output=True, text=True, timeout=plugin.timeout,
                           cwd=plugin.folder)
        for line in p.stderr.splitlines()[-30:]:
            log.info("[%s] %s", name, line)
        if p.returncode != 0:
            tail = "\n".join(p.stderr.strip().splitlines()[-5:])
            raise RuntimeError(f"plug-in exited with {p.returncode}: {tail}")
        try:
            out = json.loads(p.stdout)
        except ValueError as e:
            raise RuntimeError(f"plug-in did not print one JSON object: {e}; output starts {p.stdout[:200]!r}")

        state = {**(out.get("state") or {}), "embed_model": model}
        if out.get("unchanged"):
            self.db.set_state(name, progress="no changes", version=str(out.get("version", "")),
                              root=str(out.get("root", "")), state=state)
            return

        docs = out.get("documents") or []
        existing = self.db.doc_hashes(name)
        changed = [d for d in docs if full or existing.get(d["id"], (0, ""))[1] != d["hash"]]
        gone = sorted(set(existing) - {d["id"] for d in docs})
        doc_prefix, _ = embed_prefixes(model)
        log.info("sync %s: %d documents, %d changed, %d removed", name, len(docs), len(changed), len(gone))

        self.db.delete_documents(name, gone)
        for i, doc in enumerate(changed, 1):
            self.db.set_state(name, progress=f"embedding {i}/{len(changed)}")
            chunks = chunk_markdown(doc.get("text", ""))
            if not chunks:
                continue
            vectors: list[list[float]] = []
            texts = [embed_text(doc, c, doc_prefix) for c in chunks]
            for b in range(0, len(texts), EMBED_BATCH):
                try:
                    vectors += ollama.embed(model, texts[b:b + EMBED_BATCH], keep_alive=cfg["keep_alive"])
                except OllamaError as e:
                    raise RuntimeError(f"embedding with '{model}' failed: {e}") from e
            # Cheap check, done per document: the index can be reset (new embedding model) mid-sync.
            self.db.ensure_vec(len(vectors[0]))
            self.db.save_document(name, doc, chunks, vectors)
        self.db.set_state(name, progress=f"{len(changed)} updated, {len(gone)} removed",
                          version=str(out.get("version", "")), root=str(out.get("root", "")), state=state)

    def remove_workdir(self, name: str) -> None:
        shutil.rmtree(self.workdir(name), ignore_errors=True)
