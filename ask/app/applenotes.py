"""Apple Notes: Ask answers saved as notes on the Mac.

Ask runs in a container and can't reach Notes, so it hands the note to the host agent on the
Mac (POST <agent>/notes/create, agent 0.5.0+), which makes it with osascript. Used by the
"Save to Apple Notes" button under answers and by scheduled prompts (Deliver to: Apple Notes).
Setting notes_folder (ASK_NOTES_FOLDER, default "vCommunity Ask") is the Notes folder."""

from __future__ import annotations

from html import escape

import requests
from markdown_it import MarkdownIt

from . import mcp

_MD = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False}).enable(["table", "strikethrough"])


def to_html(markdown: str, link: str = "", footer: str = "") -> str:
    """The note body (Notes keeps headings, lists, bold, links and simple tables). Raw HTML in
    the answer is shown as text, not run."""
    html = _MD.render(markdown or "")
    # Notes draws tables without borders; give the cells some.
    html = html.replace("<table>", '<table border="1" cellpadding="4" style="border-collapse:collapse">')
    tail = []
    if link:
        tail.append(f'<a href="{escape(link, quote=True)}">Open in vCommunity Ask</a>')
    if footer:
        tail.append(escape(footer))
    if tail:
        html += "<p><br></p><p><small>" + " · ".join(tail) + "</small></p>"
    return html


def send(agent_url: str, title: str, markdown: str, folder: str = "", link: str = "", footer: str = "") -> str:
    """Make the note. Returns a short result, raises RuntimeError with a plain reason."""
    url = (agent_url or "").strip().rstrip("/")
    if not url:
        raise RuntimeError("No host agent (Settings > Host agent URL); Apple Notes needs the agent on the Mac.")
    folder = " ".join((folder or "").split()) or "vCommunity Ask"
    try:
        r = requests.post(url + "/notes/create", headers=mcp._headers(), timeout=90,
                          json={"title": title[:200], "html": to_html(markdown, link, footer), "folder": folder})
    except requests.RequestException as e:
        raise RuntimeError(f"The host agent did not answer ({e.__class__.__name__}).") from None
    if r.status_code == 404:
        raise RuntimeError("The host agent is older than 0.5.0: run the agent installer again.")
    if r.status_code == 401:
        raise RuntimeError("The host agent refused the token (ASK_AGENT_TOKEN in .env).")
    try:
        d = r.json()
    except ValueError:
        d = {}
    if r.status_code != 200 or not d.get("ok"):
        raise RuntimeError(d.get("error") or f"Host agent: HTTP {r.status_code}")
    return f"saved to Apple Notes ({folder})"
