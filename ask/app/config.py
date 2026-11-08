"""Load ask.yaml. Secrets never live in the file: a key ending in `_env` holds the
NAME of an environment variable (set in .env) that holds the real value.

Precedence for every setting: built-in default < ask.yaml < environment variable
(ASK_OLLAMA_URL and friends) < value saved on the Settings page (SQLite)."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger("ask.config")
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
HERE = Path(__file__).resolve().parent

DEFAULT_SYSTEM_PROMPT = """You are vCommunity Ask, an assistant for VMware vSphere and VCF operations.
Answer ONLY from the numbered sources in the context. They were retrieved from the knowledge base for this question.

Rules:
- Cite every fact with the source number in square brackets, for example [2]. Cite only sources you used.
- If the sources do not cover the question, say so plainly and stop. Do not fill the gap from general knowledge.
- Prefer the mechanism and the reason over a short definition.
- Keep object types apart: a VM metric, an ESXi host metric and a cluster metric with the same label are different metrics.
- Quote a threshold only when a source states it, and name that source.
- A source marked status: draft is unreviewed. Say so when the answer depends on it.
- For a troubleshooting question, follow the numbered steps of a diagnosis source in order, from step 1.
  At each step name the metric, the object and the threshold. If the user gave no value, ask for it or state both branches.
- Answer in clear markdown: short paragraphs, lists or a table where they help."""

# Settings the UI can change. Each has a default, an optional env var and a type.
SETTINGS: dict[str, dict[str, Any]] = {
    "ollama_url":       {"default": "http://host.docker.internal:11434", "env": "ASK_OLLAMA_URL", "type": "str"},
    "chat_model":       {"default": "qwen3.8:27b-mlx", "env": "ASK_CHAT_MODEL", "type": "str"},
    "embed_model":      {"default": "nomic-embed-text", "env": "ASK_EMBED_MODEL", "type": "str"},
    "think":            {"default": False, "env": "ASK_THINK", "type": "bool"},
    "temperature":      {"default": 0.2, "type": "float"},
    "num_ctx":          {"default": 16384, "type": "int"},
    "top_k":            {"default": 8, "type": "int"},
    "context_chars":    {"default": 24000, "type": "int"},
    "expand_links":     {"default": True, "type": "bool"},
    "data_questions":   {"default": True, "type": "bool"},
    "overview_questions": {"default": True, "type": "bool"},
    "history_turns":    {"default": 3, "type": "int"},
    "keep_alive":       {"default": "10m", "type": "str"},
    "system_prompt":    {"default": DEFAULT_SYSTEM_PROMPT, "type": "text"},
    "agent_url":        {"default": "http://host.docker.internal:9101", "env": "ASK_AGENT_URL", "type": "str"},
    # MCP tools: per-tool policy as JSON {"server/tool": "allow" | "ask" | "off"} (MCP tools page),
    # how many tool turns one answer may take, and how much of one tool result the model reads.
    "mcp_tools":        {"default": "{}", "type": "str"},
    "mcp_max_rounds":   {"default": 6, "type": "int"},
    "mcp_result_chars": {"default": 16000, "type": "int"},
    "observability_url": {"default": "", "env": "ASK_OBSERVABILITY_URL", "type": "str"},
    # Ollama web search (cloud): off by default (dark sites). The API key is OLLAMA_API_KEY in .env.
    "web_search":       {"default": False, "env": "ASK_WEB_SEARCH", "type": "bool"},
    "web_search_url":   {"default": "https://ollama.com", "env": "ASK_WEB_SEARCH_URL", "type": "str"},
    "web_search_results": {"default": 5, "type": "int"},
    # Brave web search (cloud): off by default. The API key is BRAVE_API_KEY in .env.
    "brave_search":     {"default": False, "env": "ASK_BRAVE_SEARCH", "type": "bool"},
    "brave_search_url": {"default": "https://api.search.brave.com", "env": "ASK_BRAVE_SEARCH_URL", "type": "str"},
    # Both web searches ticked: which searches first (ollama_first | brave_first), the other one
    # only when the first fails, finds nothing or isn't good enough; "model" lets the model pick.
    "web_order":        {"default": "ollama_first", "env": "ASK_WEB_ORDER", "type": "str"},
    # Estimated cost per successful web call (US$), for the Performance page's usage panel.
    "web_price_ollama_search": {"default": 0.01, "type": "float"},
    "web_price_ollama_fetch":  {"default": 0.001, "type": "float"},
    "web_price_brave_search":  {"default": 0.005, "type": "float"},
}


def coerce(kind: str, value: Any) -> Any:
    if kind == "bool":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    if kind == "int":
        return int(value)
    if kind == "float":
        return float(value)
    return "" if value is None else str(value)


def _resolve_env(section: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in (section or {}).items():
        if key.endswith("_env") and isinstance(value, str):
            name = key[: -len("_env")]
            if not ENV_NAME.match(value):
                log.error("config: '%s' must NAME an environment variable, not hold the secret itself.", key)
                out[name] = ""
                continue
            out[name] = os.environ.get(value, "")
        else:
            out[key] = value
    return out


@dataclass
class Config:
    path: Path
    data_dir: Path
    sources_dirs: list[Path]
    api_tokens: list[str]
    defaults: dict[str, Any]                       # yaml/env layer of SETTINGS
    sources: list[dict[str, Any]] = field(default_factory=list)  # seed source definitions


def load_config(path: str | os.PathLike | None = None) -> Config:
    path = Path(path or os.environ.get("ASK_CONFIG", HERE.parent / "config" / "ask.yaml"))
    if not path.exists():
        example = path.with_name("ask.example.yaml")
        if example.exists():
            log.info("config: %s not found, using %s", path, example)
            path = example
    raw: dict[str, Any] = {}
    if path.exists():
        raw = yaml.safe_load(path.read_text()) or {}

    data_dir = Path(os.environ.get("ASK_DATA_DIR", raw.get("data_dir", "data")))
    sources_dirs = [Path(p) for p in (raw.get("sources_dirs") or ["sources", "/sources"])]
    # Relative folders are relative to the app folder (ask/), not the working directory.
    sources_dirs = [p if p.is_absolute() else HERE.parent / p for p in sources_dirs]

    tokens: list[str] = []
    for t in (raw.get("api") or {}).get("tokens") or []:
        t = _resolve_env(t) if isinstance(t, dict) else {"token": t}
        if t.get("token"):
            tokens.append(str(t["token"]))
    if os.environ.get("ASK_API_TOKEN"):
        tokens.append(os.environ["ASK_API_TOKEN"])

    defaults: dict[str, Any] = {}
    yaml_settings = raw.get("settings") or {}
    for key, spec in SETTINGS.items():
        value = spec["default"]
        if key in yaml_settings and yaml_settings[key] not in (None, ""):
            value = yaml_settings[key]
        env = spec.get("env")
        if env and os.environ.get(env):
            value = os.environ[env]
        defaults[key] = coerce(spec["type"], value)

    return Config(path=path, data_dir=data_dir, sources_dirs=sources_dirs, api_tokens=tokens,
                  defaults=defaults, sources=list(raw.get("sources") or []))
