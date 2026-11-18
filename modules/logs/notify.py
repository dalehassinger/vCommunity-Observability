"""Alert notifications: email (SMTP, standard library) and webhooks (JSON POST).

Both are optional and work at dark sites with a local mail relay or chat server. The
webhook body has a "text" field, which Slack, Mattermost, Rocket.Chat and most chat
incoming webhooks show as the message, plus the alert details for scripts. Discord gets
embeds. Besides the default webhook there can be named ones (for example "daily"), picked
per alert rule or per Ask scheduled prompt.
"""

from __future__ import annotations

import json
import re
import smtplib
import ssl
import urllib.error
import urllib.request
from email.message import EmailMessage
from typing import Any


def describe(event: dict[str, Any]) -> tuple[str, str]:
    """(subject, body) for one fired alert."""
    subject = f"[vCommunity Logs] {event['rule_name']}: {event['count']} message(s) in {event['window_minutes']} min"
    lines = [subject, "", f"Search: {event.get('query') or '(all messages)'}", f"Fired at (UTC): {event['fired_at_text']}", "",
             "Newest matching messages:"]
    for s in event.get("sample") or []:
        lines.append(f"- {s.get('time', '')} {s.get('host', '')} {s.get('severity_name') or ''} {s.get('app') or ''}: {s.get('message', '')[:400]}")
    return subject, "\n".join(lines)


def _smtp_send(settings: dict[str, Any], msg: EmailMessage) -> None:
    host = (settings.get("smtp_host") or "").strip()
    port = int(settings.get("smtp_port") or 25)
    security = settings.get("smtp_security") or "none"
    if security == "ssl":
        server: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=15, context=ssl.create_default_context())
    else:
        server = smtplib.SMTP(host, port, timeout=15)
    with server:
        if security == "starttls":
            server.starttls(context=ssl.create_default_context())
        if settings.get("smtp_user"):
            server.login(settings["smtp_user"], settings.get("smtp_password") or "")
        server.send_message(msg)


def send_email(settings: dict[str, Any], subject: str, body: str) -> str:
    host = (settings.get("smtp_host") or "").strip()
    to = [a.strip() for a in str(settings.get("smtp_to") or "").replace(";", ",").split(",") if a.strip()]
    if not host or not to:
        raise ValueError("email needs an SMTP server and at least one To address (Log alerts → Notifications)")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = (settings.get("smtp_from") or "").strip() or f"vcommunity-logs@{host}"
    msg["To"] = ", ".join(to)
    msg.set_content(body)
    _smtp_send(settings, msg)
    return f"email sent to {', '.join(to)}"


EMAIL_RE = re.compile(r"^[^@\s,;<>\"]+@[^@\s,;<>\"]+\.[^@\s,;<>\"]+$")
MAX_RECIPIENTS = 20
MAX_ATTACH_BYTES = 15 * 1024 * 1024


def send_message(settings: dict[str, Any], to: list[str], subject: str, text: str, html: str = "",
                 attachments: list[dict[str, Any]] | None = None, sender_name: str = "") -> str:
    """Email for other apps (vCommunity Ask scheduled prompts), through the mail server set on
    Log alerts → Notifications. to: the recipients; html: optional HTML version of text;
    attachments: [{"filename", "content_type", "data" (bytes)}]."""
    host = (settings.get("smtp_host") or "").strip()
    if not host:
        raise ValueError("no mail server is set up: add one on Log alerts → Notifications in vCommunity Observability")
    to = [a.strip() for a in to if a and a.strip()]
    bad = [a for a in to if not EMAIL_RE.match(a)]
    if not to or bad:
        raise ValueError(f"not a valid email address: {', '.join(bad)}" if bad else "no recipient")
    if len(to) > MAX_RECIPIENTS:
        raise ValueError(f"at most {MAX_RECIPIENTS} recipients")
    if sum(len(a["data"]) for a in attachments or []) > MAX_ATTACH_BYTES:
        raise ValueError("attachments are larger than 15 MB")
    msg = EmailMessage()
    msg["Subject"] = re.sub(r"[\r\n]+", " ", subject)[:200]
    sender = (settings.get("smtp_from") or "").strip() or f"vcommunity-logs@{host}"
    msg["From"] = f"{sender_name} <{sender}>" if sender_name and "<" not in sender else sender
    msg["To"] = ", ".join(to)
    msg.set_content(text or " ")
    if html:
        msg.add_alternative(html, subtype="html")
    for a in attachments or []:
        maintype, _, subtype = (a.get("content_type") or "application/octet-stream").partition("/")
        msg.add_attachment(a["data"], maintype=maintype, subtype=subtype or "octet-stream",
                           filename=re.sub(r"[^\w. -]+", "_", a.get("filename") or "attachment")[:120])
    _smtp_send(settings, msg)
    return f"email sent to {', '.join(to)}"


def send_webhook(settings: dict[str, Any], subject: str, body: str, event: dict[str, Any], name: str = "") -> str:
    url = resolve_webhook(settings, name)
    alert = {k: v for k, v in event.items() if k != "fired_at_text"}
    return post_webhook(url, subject, body, {"alert": alert}, username="vCommunity Logs", parts=1, color="fail")


def webhook_names(settings: dict[str, Any]) -> list[str]:
    """The named webhooks (Log alerts → Notifications), besides the default one."""
    return [str(w.get("name")) for w in settings.get("webhooks") or [] if isinstance(w, dict) and w.get("name")]


def resolve_webhook(settings: dict[str, Any], name: str = "") -> str:
    """The URL of a webhook by name: "" or "default" is the Webhook URL field, any other name
    one of the named webhooks."""
    name = str(name or "").strip()
    if not name or name.lower() == "default":
        return str(settings.get("webhook_url") or "").strip()
    for w in settings.get("webhooks") or []:
        if isinstance(w, dict) and str(w.get("name", "")).lower() == name.lower():
            return str(w.get("url") or "").strip()
    raise ValueError(f"no webhook named '{name}': add it under Log alerts → Notifications, or pick another")


def mask_url(url: str) -> str:
    """What the settings page shows for a saved webhook: the site only (the rest works like a password)."""
    url = str(url or "").strip()
    if not url:
        return ""
    head, sep, rest = url.partition("://")
    host = rest.split("/", 1)[0]
    return f"{head}{sep}{host}/…" if sep else "…"


DISCORD_LIMIT = 2000      # characters in one Discord message (plain content)
DISCORD_EMBED_LIMIT = 4000  # characters in one Discord embed description (Discord allows 4,096)
DISCORD_PARTS = 5         # a long text goes out as at most this many Discord messages
COLORS = {"fail": 0xD93025, "warn": 0xE37400, "ok": 0x2E7D32, "info": 0x0072A3}


def post_webhook(url: str, title: str, text: str, extra: dict[str, Any] | None = None, *,
                 username: str = "vCommunity", parts: int = DISCORD_PARTS, color: str = "info", link: str = "") -> str:
    """POST text to a chat webhook. Discord URLs get Discord embeds (a title, a color bar: red
    for failures, green for ok, blue for information; a link; the text split on paragraph
    lines into at most `parts` messages, no @mentions); any other URL gets one JSON
    {"text", "title", **extra} (Slack, Mattermost, Rocket.Chat, scripts)."""
    url = (url or "").strip()
    if not url:
        raise ValueError("no webhook URL is set: enter one under Log alerts → Notifications and click Save")
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("the webhook needs an http:// or https:// URL (Log alerts → Notifications)")
    if DISCORD_RE.match(url):
        chunks = split_text(text, DISCORD_EMBED_LIMIT, parts)
        payloads = []
        for i, chunk in enumerate(chunks):
            embed: dict[str, Any] = {"description": chunk, "color": COLORS.get(color, COLORS["info"])}
            if i == 0 and title:
                embed["title"] = title[:256]
                if link.lower().startswith(("http://", "https://")):
                    embed["url"] = link
            if len(chunks) > 1:
                embed["footer"] = {"text": f"Part {i + 1} of {len(chunks)}"}
            payloads.append({"embeds": [embed], "username": username, "allowed_mentions": {"parse": []}})
    else:
        payloads = [{"text": text, "title": title, **({"link": link} if link else {}), **(extra or {})}]
    status = 0
    for payload in payloads:
        req = urllib.request.Request(url, data=json.dumps(payload, default=str).encode(),
                                     headers={"Content-Type": "application/json", "User-Agent": "vcommunity-logs"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                status = resp.status
        except urllib.error.HTTPError as exc:  # say what the service answered (never the URL, it holds the secret)
            detail = exc.read(300).decode("utf-8", "replace").strip()
            raise ValueError(f"the webhook answered {exc.code} {exc.reason}" + (f": {detail}" if detail else "")) from None
    return f"webhook answered {status}" + (f" ({len(payloads)} messages)" if len(payloads) > 1 else "")


def split_text(text: str, limit: int, parts: int) -> list[str]:
    """Cut text into at most `parts` pieces of at most `limit` characters, at line breaks
    where it can (blank lines first). The last piece says so when text was left out."""
    text = (text or "").strip() or " "
    out: list[str] = []
    while text and len(out) < parts:
        if len(text) <= limit:
            out.append(text)
            text = ""
            break
        cut = text.rfind("\n\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        out.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        note = "\n… (cut: too long for the chat)"
        out[-1] = out[-1][:limit - len(note)].rstrip() + note
    return out


DISCORD_RE = re.compile(r"^https://(?:\w+\.)?discord(?:app)?\.com/api/webhooks/", re.I)


def notify(settings: dict[str, Any], event: dict[str, Any], email: bool, webhook: bool, webhook_name: str = "") -> str:
    """Send what the rule asks for. Returns a short result line for the alert history."""
    subject, body = describe(event)
    results = []
    for wanted, name, fn in ((email, "email", lambda: send_email(settings, subject, body)),
                             (webhook, "webhook", lambda: send_webhook(settings, subject, body, event, webhook_name))):
        if not wanted:
            continue
        try:
            results.append(fn())
        except Exception as exc:  # noqa: BLE001 - record, never crash the listener
            results.append(f"{name} failed: {str(exc)[:200]}")
    return "; ".join(results)
