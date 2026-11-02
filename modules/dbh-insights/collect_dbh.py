"""DBH Insights collector for vCommunity-Observability.

Builds every DBH Insights inventory sheet from one vCenter and writes one table
per sheet, plus `sheets` (status per sheet) and `sheet_columns` (the original
column labels, kinds and totals rules, used by the pages and the XLSX export).

Contract: VCO_VCENTER_* connection details in, one JSON object keyed by table
name out to VCO_OUTPUT_FILE, logs to stderr. VCO_MOCK=1 uses sample data.
"""

import json
import os
import sys
import time

from sheets import VcenterData, collect
from vsphere import log


def main() -> None:
    settings = json.loads(os.environ.get("VCO_SETTINGS") or "{}")
    host = os.environ.get("VCO_VCENTER_HOST", "")
    user = os.environ.get("VCO_VCENTER_USER", "")
    # The website's "VI SDK Server" column shows the vCenter's display name.
    server = settings.get("vcenter_name") or host or "vCenter"

    if os.environ.get("VCO_MOCK") == "1":
        from sample_data import SampleSource
        source = SampleSource()
        server = settings.get("vcenter_name") or "vcsa91.vcrocs.local"
        user = user or "administrator@vcrocs.local"
    else:
        if not host:
            raise SystemExit("VCO_VCENTER_HOST is not set; configure the vcenter connection in config.yaml")
        if not os.environ.get("VCO_VCENTER_PASSWORD"):
            raise SystemExit("vCenter password is empty: set VCO_VCENTER_PASSWORD in .env and make sure "
                             "config.yaml has password_env: VCO_VCENTER_PASSWORD")
        from vsphere import LiveSource
        verify = os.environ.get("VCO_VCENTER_VERIFY_SSL", "false").lower() == "true"
        source = LiveSource(host, user, os.environ["VCO_VCENTER_PASSWORD"], verify)

    started = time.monotonic()
    try:
        data = collect(VcenterData(source, server, user))
    finally:
        if hasattr(source, "close"):
            source.close()

    failed = [s for s in data["sheets"] if s["error"]]
    for s in failed:
        log(f"sheet {s['sheet']} failed: {s['error']}")
    log(f"{len(data['sheets']) - len(failed)} of {len(data['sheets'])} sheets from {server} "
        f"in {time.monotonic() - started:.1f}s")
    if len(failed) == len(data["sheets"]):
        raise SystemExit(f"every sheet failed; first error: {failed[0]['error']}")

    payload = json.dumps(data, default=str)
    if os.environ.get("VCO_OUTPUT_FILE"):
        with open(os.environ["VCO_OUTPUT_FILE"], "w") as fh:
            fh.write(payload)
    else:
        print(payload)


if __name__ == "__main__":
    sys.exit(main())
