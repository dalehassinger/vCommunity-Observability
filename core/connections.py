"""Connection settings edited in the UI, and the "Test connection" check."""

from __future__ import annotations

from typing import Any

# Fields every connection shows on the Settings page, in this order.
STANDARD_FIELDS = ["host", "user", "password", "verify_ssl"]
LABELS = {"host": "Address (FQDN or IP)", "user": "Username", "password": "Password",
          "verify_ssl": "Verify TLS certificate", "auth_source": "Auth source"}


def fields_for(file_conn: dict[str, Any]) -> list[str]:
    extra = [k for k, v in file_conn.items() if k not in STANDARD_FIELDS and not isinstance(v, (dict, list))]
    return STANDARD_FIELDS + extra


def as_bool(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def test_connection(name: str, conn: dict[str, Any], mock: bool) -> tuple[bool, str]:
    """Log in and out once. Returns (ok, message)."""
    if mock:
        return True, "Sample data mode is on, so no real connection was made."
    host = str(conn.get("host") or "").strip()
    if not host:
        return False, "No address set."
    if not conn.get("password"):
        return False, "No password set."
    verify = as_bool(conn.get("verify_ssl", False))
    try:
        if name == "vcfops":
            import requests
            r = requests.post(
                f"https://{host}/suite-api/api/auth/token/acquire",
                json={"username": conn.get("user", ""), "password": conn["password"],
                      "authSource": conn.get("auth_source") or "local"},
                headers={"Accept": "application/json"}, verify=verify, timeout=20,
            )
            if r.status_code != 200:
                return False, f"VCF Operations answered HTTP {r.status_code}: {r.text[:200]}"
            return True, f"Logged in to VCF Operations at {host}."
        # Anything else is treated as a vCenter / ESXi endpoint.
        from pyVim.connect import Disconnect, SmartConnect
        si = SmartConnect(host=host, user=conn.get("user", ""), pwd=conn["password"],
                          disableSslCertValidation=not verify, connectionPoolTimeout=20)
        about = si.content.about
        Disconnect(si)
        return True, f"Logged in to {about.fullName}."
    except Exception as exc:
        msg = getattr(exc, "msg", None) or str(exc) or type(exc).__name__
        return False, f"{type(exc).__name__}: {msg}"[:400]
