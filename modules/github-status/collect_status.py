"""Collect service status from a Statuspage site (default: www.githubstatus.com).

Uses the public Statuspage API v2 (no key needed):
  /api/v2/summary.json    overall status, components, open incidents, maintenance
  /api/v2/incidents.json  the 50 most recent incidents, including resolved ones

Module setting "url" (config.yaml -> modules.github-status.settings.url) points it
at another Statuspage site. Follows the collector contract: JSON keyed by table
name to VCO_OUTPUT_FILE (or stdout), logs to stderr.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

DEFAULT_URL = "https://www.githubstatus.com"
SKIP_PREFIX = "Visit "   # GitHub lists a pseudo-component "Visit www.githubstatus.com for more information"
DONE = {"resolved", "postmortem", "completed"}


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def ts(value):
    """ISO 8601 with offset -> 'YYYY-MM-DD HH:MM:SS' UTC, sortable in SQLite."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return value


def comp_names(items):
    return ", ".join(c.get("name", "") for c in (items or []))


def latest_update(inc):
    ups = inc.get("incident_updates") or []
    return (ups[0].get("body") or "").strip() if ups else ""


def incident_row(inc):
    return {
        "name": inc.get("name"), "status": inc.get("status"), "impact": inc.get("impact"),
        "active": 0 if inc.get("status") in DONE else 1,
        "started_at": ts(inc.get("started_at") or inc.get("created_at")),
        "updated_at": ts(inc.get("updated_at")), "resolved_at": ts(inc.get("resolved_at")),
        "shortlink": inc.get("shortlink"), "latest_update": latest_update(inc)[:1000],
        "components": comp_names(inc.get("components")),
    }


def transform(summary: dict, incidents: dict, base: str) -> dict:
    page = summary.get("page") or {}
    status = summary.get("status") or {}
    comps = [c for c in summary.get("components") or []
             if not c.get("group") and not str(c.get("name", "")).startswith(SKIP_PREFIX)]
    # Recent incidents (incl. resolved) plus any open ones the summary has.
    by_id = {i["id"]: i for i in (incidents.get("incidents") or [])[:30] if "id" in i}
    for i in summary.get("incidents") or []:
        by_id[i.get("id", i.get("name"))] = i
    return {
        "page_status": [{
            "name": page.get("name") or "GitHub", "url": page.get("url") or base,
            "indicator": status.get("indicator"), "description": status.get("description"),
            "updated_at": ts(page.get("updated_at")),
        }],
        "components": [{
            "name": c.get("name"), "status": c.get("status"), "description": c.get("description") or "",
            "position": c.get("position"), "updated_at": ts(c.get("updated_at")),
        } for c in comps],
        "incidents": [incident_row(i) for i in by_id.values()],
        "maintenances": [{
            "name": m.get("name"), "status": m.get("status"), "impact": m.get("impact"),
            "scheduled_for": ts(m.get("scheduled_for")), "scheduled_until": ts(m.get("scheduled_until")),
            "shortlink": m.get("shortlink"), "components": comp_names(m.get("components")),
        } for m in summary.get("scheduled_maintenances") or []],
    }


def fetch(base: str):
    import requests
    headers = {"User-Agent": "vCommunity-Observability/0.1 (+status collector)", "Accept": "application/json"}
    out = []
    for path in ("/api/v2/summary.json", "/api/v2/incidents.json"):
        r = requests.get(base.rstrip("/") + path, headers=headers, timeout=20)
        r.raise_for_status()
        out.append(r.json())
    return out


def sample_payloads(base: str):
    now = datetime.now(timezone.utc)
    iso = lambda d: (now - d).isoformat()
    names = ["Git Operations", "Webhooks", "API Requests", "Issues", "Pull Requests", "Actions",
             "Packages", "Pages", "Codespaces", "Copilot"]
    comps = [{"id": f"c{i}", "name": n, "position": i + 1, "group": False,
              "status": "degraded_performance" if n == "Actions" else "operational",
              "updated_at": iso(timedelta(minutes=12 if n == "Actions" else 300))} for i, n in enumerate(names)]
    comps.append({"id": "cx", "name": "Visit www.githubstatus.com for more information", "position": 99,
                  "group": False, "status": "operational"})
    active = {"id": "i1", "name": "Disruption with some GitHub services", "status": "investigating", "impact": "minor",
              "started_at": iso(timedelta(minutes=34)), "created_at": iso(timedelta(minutes=34)),
              "updated_at": iso(timedelta(minutes=12)), "shortlink": "https://stspg.io/example1",
              "components": [{"name": "Actions"}],
              "incident_updates": [{"body": "We are investigating reports of delayed Actions workflow runs."}]}
    resolved = []
    for k, (title, comp, impact, days) in enumerate([
        ("Incident with Pull Requests", "Pull Requests", "minor", 1),
        ("Degraded performance for Copilot", "Copilot", "minor", 2),
        ("Incident with Git Operations and API Requests", "Git Operations", "major", 4),
        ("Disruption with Codespaces", "Codespaces", "minor", 6),
        ("Incident with Webhooks", "Webhooks", "minor", 9)]):
        start = timedelta(days=days, hours=k)
        resolved.append({"id": f"r{k}", "name": title, "status": "resolved", "impact": impact,
                         "started_at": iso(start), "created_at": iso(start), "updated_at": iso(start - timedelta(hours=1)),
                         "resolved_at": iso(start - timedelta(hours=1)), "shortlink": f"https://stspg.io/example-r{k}",
                         "components": [{"name": comp}],
                         "incident_updates": [{"body": "This incident has been resolved."}]})
    summary = {
        "page": {"name": "GitHub", "url": base, "updated_at": iso(timedelta(minutes=12))},
        "status": {"indicator": "minor", "description": "Minor Service Outage"},
        "components": comps, "incidents": [active],
        "scheduled_maintenances": [{"name": "Scheduled maintenance for GitHub Packages", "status": "scheduled",
                                    "impact": "maintenance", "scheduled_for": (now + timedelta(days=2)).isoformat(),
                                    "scheduled_until": (now + timedelta(days=2, hours=2)).isoformat(),
                                    "shortlink": "https://stspg.io/example-m1", "components": [{"name": "Packages"}]}],
    }
    return summary, {"incidents": [active] + resolved}


if __name__ == "__main__":
    settings = json.loads(os.environ.get("VCO_SETTINGS") or "{}")
    base = settings.get("url") or DEFAULT_URL
    if os.environ.get("VCO_MOCK") == "1":
        summary, incidents = sample_payloads(base)
    else:
        try:
            summary, incidents = fetch(base)
        except Exception as exc:
            raise SystemExit(f"Could not reach {base} ({exc}). This module needs internet access or HTTPS_PROXY; "
                             "switch it off on the Modules page at dark sites.")
    data = transform(summary, incidents, base)
    log(f"{base}: {data['page_status'][0]['description']}, {len(data['components'])} components, "
        f"{sum(i['active'] for i in data['incidents'])} open incident(s)")
    payload = json.dumps(data)
    if os.environ.get("VCO_OUTPUT_FILE"):
        with open(os.environ["VCO_OUTPUT_FILE"], "w") as fh:
            fh.write(payload)
    else:
        print(payload)
