"""Plain-English explanations for collector and export errors.

A failed collector stores its raw error (a Python traceback, a PowerShell error record, a
pyVmomi fault...). That is useful for debugging but hard to read on a dashboard, so the UI
shows `explain(message)` instead: a one-line summary and a hint on what to do, with the
raw text kept behind a "Details" toggle.

Add a new case by appending to RULES: a regex that matches the raw text, a summary and a
hint. `{host}` in either is replaced with the server name found in the error, if any.
"""
from __future__ import annotations

import re

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

# Server names as they appear in the common error formats.
HOST_PATTERNS = [
    re.compile(r"\(([A-Za-z0-9.-]+\.[A-Za-z]{2,}):\d+\)"),                 # PowerCLI: (vcsa.lab.local:443)
    re.compile(r"host=['\"]([^'\"]+)['\"]"),                                 # urllib3 / requests
    re.compile(r"(?:Server|server|vCenter|host)\s+['\"]?([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)"),
    re.compile(r"https?://([A-Za-z0-9.-]+)"),
]

RULES: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"No address associated with hostname|Name or service not known|nodename nor servname|"
                r"Temporary failure in name resolution|getaddrinfo failed|NameResolutionError|"
                r"No such host is known|could not resolve", re.I),
     "Can't find {host} on the network: the DNS lookup failed.",
     "The container could not turn the server name into an IP address. Check that the DNS server for this "
     "name is up and that this computer is on that network (or VPN). If the computer itself can resolve the "
     "name but the container can't, add it under extra_hosts in docker-compose.yml, or use the IP address "
     "on the Settings page."),
    (re.compile(r"Connection refused|actively refused|ECONNREFUSED", re.I),
     "{host} refused the connection.",
     "The server answered but nothing is listening on that port. Check that vCenter (or the service) is "
     "running and that the host name and port on the Settings page are right."),
    (re.compile(r"No route to host|Network is unreachable|Host is down|EHOSTUNREACH", re.I),
     "Can't reach {host}: there is no network route to it.",
     "Check that this computer is on the lab network or VPN, and that the server is powered on."),
    (re.compile(r"InvalidLogin|incorrect user name or password|Cannot complete login|"
                r"Authentication failed|401 Unauthorized|status code 401", re.I),
     "Logging in to {host} failed: the user name or password was not accepted.",
     "Check the user and password on the Settings page (Save and test), and that the account is not locked."),
    (re.compile(r"CERTIFICATE_VERIFY_FAILED|certificate verify failed|SSL: |self[- ]signed certificate", re.I),
     "{host}'s certificate is not trusted.",
     "Turn off Verify SSL for this vCenter on the Settings page (normal in a lab), or add the vCenter's CA "
     "certificate to the container."),
    (re.compile(r"password is empty", re.I),
     "No password is set for this vCenter.",
     "Enter the password on the Settings page, or set it in .env."),
    (re.compile(r"has no host|No vCenter configured", re.I),
     "No vCenter is configured.",
     "Add a vCenter on the Settings page (host, user and password), then press Run now."),
    (re.compile(r"PowerShell \(pwsh\) is not installed|pwsh: not found|No such file or directory: 'pwsh'", re.I),
     "PowerShell is not installed in this container.",
     "This module is written in PowerShell. Build and run the core-pwsh image (see Getting started)."),
    (re.compile(r"timed out after|TimeoutExpired", re.I),
     "The collector took too long and was stopped.",
     "The server may be slow or very large. Raise timeout_seconds for this collector in its module.yaml or "
     "config.yaml, or check that the server is responding."),
    (re.compile(r"timed out|Read timed out|ConnectTimeout|Connect timeout", re.I),
     "{host} did not answer in time.",
     "Check that the server is up and reachable from this computer (firewall, VPN, lab network)."),
    (re.compile(r"status code 403|403 Forbidden|NoPermission", re.I),
     "{host} refused access: the account lacks permission.",
     "Use an account with read access to the inventory (at least Read-only at the vCenter root)."),
    (re.compile(r"status code 5\d\d|50[0-4] (?:Server|Internal|Bad|Service|Gateway)", re.I),
     "{host} returned a server error.",
     "The service had a problem of its own. Try again later; if it keeps happening, check the service."),
    (re.compile(r"collector output is not JSON", re.I),
     "The collector printed something unexpected instead of its data.",
     "This is usually a bug or a warning printed by the script. The details below show what it printed."),
    (re.compile(r"missing table|unknown table|not declared in module.yaml", re.I),
     "The collector's data did not match the module's tables.",
     "Nothing was saved from this run. The module's collector and module.yaml disagree; see the details."),
]


def clean(text: str) -> str:
    """Strip terminal colour codes (PowerShell error records are full of them)."""
    return ANSI.sub("", text or "")


def find_host(text: str) -> str:
    for pat in HOST_PATTERNS:
        m = pat.search(text)
        if m and m.group(1) not in ("testserver",):
            return m.group(1)
    return ""


def _last_line(text: str) -> str:
    """Best short line for an unknown error: the last line that looks like an error message."""
    # PowerShell error records put a "| (server:443)" pointer line last; skip such lines.
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("|")]
    for ln in reversed(lines):
        if re.search(r"(Error|Exception|failed|Fault|msg = )", ln, re.I) and not ln.startswith("File \""):
            ln = re.sub(r"^msg = ", "", ln).strip("'\"")
            return ln[:240]
    return (lines[-1] if lines else "Unknown error")[:240]


def explain(message: str | None, default_host: str = "") -> dict:
    """{summary, hint, details}: a plain-English summary, what to do, and the raw text."""
    raw = clean(message or "").strip()
    if not raw:
        return {"summary": "", "hint": "", "details": ""}
    host = find_host(raw) or default_host
    for pat, summary, hint in RULES:
        if pat.search(raw):
            name = host or "the server"
            s = summary.replace("{host}", name)
            return {"summary": s[0].upper() + s[1:], "hint": hint.replace("{host}", name), "details": raw}
    short = _last_line(raw)
    return {"summary": short, "hint": "", "details": raw if raw != short else ""}
