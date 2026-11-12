"""Links between the two containers (Observability <-> vCommunity Ask) that follow the
address the browser used.

VCO_ASK_URL and Ask's Observability URL usually say http://localhost:8081 and
http://localhost:8080. That works on the computer running Docker, but from another
computer "localhost" is the wrong machine. So when the configured link points at a
local-only name (localhost, 127.0.0.1, ::1, 0.0.0.0, host.docker.internal) and the
page was opened by another name or IP, the link keeps its scheme, port and path and
takes the host name from the current request instead.

A link that names a real host (for example https://ask.lab.local) is left as it is.
Identical copies: core/links.py and ask/app/links.py.
"""
from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

LOCAL_NAMES = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal"}


def follow_host(url: str, request_host: str | None) -> str:
    """`url` with a local-only host swapped for `request_host` (the host the browser used)."""
    if not url or not request_host:
        return url or ""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host not in LOCAL_NAMES or request_host.lower() in LOCAL_NAMES:
        return url
    new_host = f"[{request_host}]" if ":" in request_host else request_host
    netloc = new_host + (f":{parts.port}" if parts.port else "")
    if parts.username:
        netloc = parts.username + (f":{parts.password}" if parts.password else "") + "@" + netloc
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
