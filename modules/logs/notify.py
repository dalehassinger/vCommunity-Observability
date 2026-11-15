"""Alert notifications: email (SMTP, standard library) and a webhook (JSON POST).

Both are optional and work at dark sites with a local mail relay or chat server. The
webhook body has a "text" field, which Slack, Mattermost, Rocket.Chat and most chat
incoming webhooks show as the message, plus the alert details for scripts.
"""

from __future__ import annotations

import json
import re
import smtplib
import ssl
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


def send_webhook(settings: dict[str, Any], subject: str, body: str, event: dict[str, Any]) -> str:
    url = (settings.get("webhook_url") or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("the webhook needs an http:// or https:// URL (Log alerts → Notifications)")
    payload = {"text": body, "title": subject, "alert": {k: v for k, v in event.items() if k != "fired_at_text"}}
    req = urllib.request.Request(url, data=json.dumps(payload, default=str).encode(),
                                 headers={"Content-Type": "application/json", "User-Agent": "vcommunity-logs"}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return f"webhook answered {resp.status}"


def notify(settings: dict[str, Any], event: dict[str, Any], email: bool, webhook: bool) -> str:
    """Send what the rule asks for. Returns a short result line for the alert history."""
    subject, body = describe(event)
    results = []
    for wanted, name, fn in ((email, "email", lambda: send_email(settings, subject, body)),
                             (webhook, "webhook", lambda: send_webhook(settings, subject, body, event))):
        if not wanted:
            continue
        try:
            results.append(fn())
        except Exception as exc:  # noqa: BLE001 - record, never crash the listener
            results.append(f"{name} failed: {str(exc)[:200]}")
    return "; ".join(results)
