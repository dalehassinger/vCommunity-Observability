"""Activity log: what vCommunity Ask is doing, sent as syslog to the Logs module.

Every question (prompt), the model's thinking, each tool call, the answer (or the error)
goes to the syslog server as RFC 5424 messages with app-name ``vcommunity-ask`` and
msgid ``prompt`` | ``thinking`` | ``tool`` | ``data`` | ``answer`` | ``error`` | ``stopped``.
Structured data ``[ask@32473 chat=... model=... part=n/m ...]`` holds the details, so
the Logs page can search them (``app:vcommunity-ask msgid:prompt``).

Default target ``tcp://vco:5514``: the vco container's listener on the compose network.
TCP uses octet counting (RFC 6587), so a message may hold new lines. Long text (the
thinking) is cut into parts that each fit one syslog frame.

Sending never slows down or breaks an answer: messages go on a queue and one background
thread writes them. When the server can't be reached the messages are dropped and the
reason is kept for the Settings page (status()). Standard library only.
"""

from __future__ import annotations

import logging
import os
import queue
import socket
import threading
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

log = logging.getLogger("ask.activity")

APP = "vcommunity-ask"
SD_ID = "ask@32473"           # 32473 is the private enterprise number reserved for examples (RFC 5612)
FACILITY = 21                 # local5
SEV = {"error": 3, "stopped": 4, "prompt": 5, "answer": 6, "tool": 6, "data": 6, "thinking": 6}
PART_CHARS = 6000             # one part of a long text; UTF-8 keeps it well under the 64 KB frame
MAX_PARTS = 40                # 240,000 characters of thinking is plenty
QUEUE_MAX = 5000
DEFAULT_URL = "tcp://vco:5514"


def parse_url(url: str) -> tuple[str, str, int]:
    """'tcp://vco:5514', 'udp://10.0.0.5', 'vco:5514' (TCP) -> (proto, host, port)."""
    url = (url or "").strip() or DEFAULT_URL
    if "://" not in url:
        url = "tcp://" + url
    u = urlsplit(url)
    proto = (u.scheme or "tcp").lower()
    if proto not in ("tcp", "udp"):
        raise ValueError(f"use tcp:// or udp:// (not {proto}://)")
    if not u.hostname:
        raise ValueError("no server name")
    return proto, u.hostname, u.port or 514


def _sd_value(v: Any) -> str:
    s = str(v).replace("\\", "\\\\").replace('"', '\\"').replace("]", "\\]")
    return " ".join(s.split())[:400]


def _sd_name(k: str) -> str:
    return "".join(ch for ch in k if ch.isalnum() or ch in "_.-")[:32] or "x"


def frame(msgid: str, text: str, sd: dict[str, Any], hostname: str, severity: int | None = None) -> bytes:
    pri = FACILITY * 8 + (SEV.get(msgid, 6) if severity is None else severity)
    ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    params = " ".join(f'{_sd_name(k)}="{_sd_value(v)}"' for k, v in sd.items() if v not in (None, ""))
    sd_text = f"[{SD_ID} {params}]" if params else "-"
    return f"<{pri}>1 {ts} {hostname} {APP} {os.getpid()} {msgid} {sd_text} {text}".encode("utf-8", "replace")


def split(text: str, size: int = PART_CHARS, limit: int = MAX_PARTS) -> list[str]:
    """Cut at a blank line, a new line or a space near the end of each part."""
    text = text or ""
    parts: list[str] = []
    while text and len(parts) < limit:
        if len(text) <= size:
            parts.append(text)
            break
        cut = -1
        for sep in ("\n\n", "\n", " "):
            cut = text.rfind(sep, int(size * 0.7), size)
            if cut > 0:
                break
        cut = cut if cut > 0 else size
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n ")
    if text and len(parts) >= limit:
        parts[-1] += f"\n… ({len(text):,} more characters not sent)"
    return parts


class Sender:
    def __init__(self) -> None:
        self.q: queue.Queue[tuple[str, bytes]] = queue.Queue(maxsize=QUEUE_MAX)
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.sock: socket.socket | None = None
        self.target: tuple[str, str, int] | None = None
        self.sent = 0
        self.dropped = 0
        self.last_error = ""
        self.last_error_at = 0.0
        self.last_sent_at = 0.0
        self._warned = 0.0
        self.down_until = 0.0

    def put(self, url: str, data: bytes) -> None:
        with self.lock:
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._run, name="activity-log", daemon=True)
                self.thread.start()
        try:
            self.q.put_nowait((url, data))
        except queue.Full:
            self.dropped += 1

    def _close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None

    def _send(self, url: str, data: bytes) -> None:
        target = parse_url(url)
        if target != self.target:
            self._close()
            self.target = target
        proto, host, port = target
        if proto == "udp":
            if self.sock is None:
                self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.sendto(data[:65000], (host, port))
            return
        payload = str(len(data)).encode() + b" " + data
        for attempt in (1, 2):      # a server restart closes the old connection: reconnect once
            if self.sock is None:
                self.sock = socket.create_connection((host, port), timeout=5)
            try:
                self.sock.sendall(payload)
                return
            except OSError:
                self._close()
                if attempt == 2:
                    raise

    def _run(self) -> None:
        while True:
            url, data = self.q.get()
            if time.time() < self.down_until:     # the server just failed: don't wait on it per message
                self.dropped += 1
                continue
            try:
                self._send(url, data)
                self.sent += 1
                self.last_sent_at = time.time()
            except (OSError, ValueError) as e:
                self._close()
                self.dropped += 1
                self.last_error = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
                self.last_error_at = time.time()
                if time.time() - self._warned > 300:
                    self._warned = time.time()
                    log.warning("activity log: can't send to %s (%s); messages are dropped until it answers",
                                url, self.last_error)
                # Don't hammer a server that is down: drop messages for the next 10 seconds.
                self.down_until = time.time() + 10


SENDER = Sender()


def enabled(cfg: dict[str, Any]) -> bool:
    return bool(cfg.get("activity_log"))


def hostname() -> str:
    return os.environ.get("ASK_SYSLOG_HOSTNAME") or "vco-ask"


def emit(cfg: dict[str, Any], msgid: str, text: str, sd: dict[str, Any] | None = None, *,
         head: str = "", severity: int | None = None) -> None:
    """Send one activity message (several parts when the text is long). Never raises."""
    if not enabled(cfg):
        return
    try:
        url = cfg.get("activity_log_url") or DEFAULT_URL
        parts = split(text) or [""]
        for i, part in enumerate(parts, 1):
            d = dict(sd or {})
            if len(parts) > 1:
                d["part"] = f"{i}/{len(parts)}"
            body = (head + (f" (part {i} of {len(parts)})" if len(parts) > 1 else "") + (":\n" if part else "")
                    if head else "") + part
            SENDER.put(url, frame(msgid, body, d, hostname(), severity))
    except Exception:  # an activity log problem must never break an answer
        log.exception("activity log")


def status(cfg: dict[str, Any]) -> dict[str, Any]:
    try:
        proto, host, port = parse_url(cfg.get("activity_log_url") or DEFAULT_URL)
        target, bad = f"{proto}://{host}:{port}", ""
    except ValueError as e:
        target, bad = cfg.get("activity_log_url") or "", str(e)
    return {"enabled": enabled(cfg), "target": target, "url_error": bad, "sent": SENDER.sent,
            "dropped": SENDER.dropped, "last_error": SENDER.last_error, "last_error_at": SENDER.last_error_at,
            "last_sent_at": SENDER.last_sent_at, "queued": SENDER.q.qsize()}


def test(cfg: dict[str, Any]) -> dict[str, Any]:
    """Send one message right now (not through the queue) and say what happened."""
    url = cfg.get("activity_log_url") or DEFAULT_URL
    try:
        proto, host, port = parse_url(url)
        data = frame("test", "vCommunity Ask activity log test message", {"via": "settings"}, hostname(), 5)
        if proto == "udp":
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.sendto(data, (host, port))
            return {"ok": True, "message": f"Sent over UDP to {host}:{port}. UDP can't confirm delivery: "
                                           "look for it on the Logs page (app:vcommunity-ask msgid:test)."}
        with socket.create_connection((host, port), timeout=5) as s:
            s.sendall(str(len(data)).encode() + b" " + data)
        return {"ok": True, "message": f"Sent over TCP to {host}:{port}. It shows on the Logs page as app "
                                       "vcommunity-ask, message ID test."}
    except (OSError, ValueError) as e:
        return {"ok": False, "message": f"Can't send to {url}: {e}"}


# -- what answer_stream reports ---------------------------------------------------------------

def _short(s: Any, n: int = 160) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def prompt(cfg: dict[str, Any], chat_id: str, question: str, sources: list[str] | None,
           tools: list[str] | None, interactive: bool) -> None:
    if sources == [] and not tools:
        scope = "no sources (just the model)"
    elif sources is None:
        scope = "all sources"
    else:
        scope = ", ".join(sources) or "none (tools only)"
    via = "chat" if interactive else "api"
    emit(cfg, "prompt", question,
         {"chat": chat_id, "model": cfg.get("chat_model"), "sources": scope, "tools": ",".join(tools or []),
          "via": via, "think": "on" if cfg.get("think") else "off", "chars": len(question or "")},
         head=f"Prompt in chat {chat_id} ({via}, model {cfg.get('chat_model')}, sources: {_short(scope, 120)})")


def data_query(cfg: dict[str, Any], chat_id: str, res: dict[str, Any]) -> None:
    sql = res.get("sql") or ""
    if isinstance(res.get("panels"), list):
        sql = "\n\n".join(f"-- {p.get('title', '')}\n{p.get('sql', '')}" for p in res["panels"] if isinstance(p, dict))
    if not sql:
        return
    emit(cfg, "data", sql, {"chat": chat_id, "source": res.get("source"), "rows": len(res.get("rows") or [])},
         head=f"Data question in chat {chat_id}: SQL the model wrote")


def tool(cfg: dict[str, Any], chat_id: str, card: dict[str, Any]) -> None:
    status_ = card.get("status") or "?"
    name = card.get("tool") or "?"
    args = card.get("arguments") or {}
    try:
        import json
        args_text = json.dumps(args, ensure_ascii=False)[:2000]
    except (TypeError, ValueError):
        args_text = str(args)[:2000]
    detail = card.get("error") or (f"{card.get('chars', 0):,} characters back" if status_ == "ok" else "")
    emit(cfg, "tool", f"{name} {args_text}" + (f"\n{detail}" if detail else ""),
         {"chat": chat_id, "tool": name, "server": card.get("server"), "status": status_, "ms": card.get("ms")},
         head=f"Tool call in chat {chat_id}: {name} ({card.get('server') or '-'}) {status_}",
         severity=4 if status_ in ("error", "refused") else None)


def finish(cfg: dict[str, Any], chat_id: str, model: str, thinking: str, content: str,
           stats: dict[str, Any], finished: bool) -> None:
    sd = {"chat": chat_id, "model": model}
    if thinking and cfg.get("activity_log_thinking", True):
        emit(cfg, "thinking", thinking, {**sd, "chars": len(thinking)},
             head=f"Model thinking in chat {chat_id} ({len(thinking):,} characters)")
    nums = {"seconds": stats.get("seconds"), "tokens": stats.get("tokens"), "tok_per_s": stats.get("tok_per_s"),
            "prompt_tokens": stats.get("prompt_tokens"), "tool_calls": stats.get("tool_calls"),
            "chars": len(content or "")}
    if stats.get("error"):
        emit(cfg, "error", str(stats["error"]), {**sd, **nums}, head=f"Answer failed in chat {chat_id}")
        return
    if not finished:
        emit(cfg, "stopped", _short(content, 400) if content else "", {**sd, **nums},
             head=f"Answer stopped in chat {chat_id} before it finished (the reader closed the page or pressed Stop)")
        return
    extra = ", ".join(f"{k} {v}" for k, v in (("seconds", nums["seconds"]), ("tokens", nums["tokens"]),
                                                  ("tok/s", nums["tok_per_s"])) if v is not None)
    head = f"Answer in chat {chat_id} ({extra or 'done'})"
    body = content if cfg.get("activity_log_answers", True) else f"{len(content or ''):,} characters (answer text is not sent)"
    emit(cfg, "answer", body, {**sd, **nums}, head=head)
