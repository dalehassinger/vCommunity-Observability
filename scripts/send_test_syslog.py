"""Send test syslog messages to the Logs module (no lab needed).

    python scripts/send_test_syslog.py                       # 200 mixed messages to localhost:514
    python scripts/send_test_syslog.py --host 192.168.1.50 --count 5000 --rate 500
    python scripts/send_test_syslog.py --port 5514 --proto tcp

Messages look like ESXi (RFC 5424, sent over TCP with octet counting like ESXi does),
vCenter and classic BSD syslog (RFC 3164), with a mix of severities. Standard library only.
"""

from __future__ import annotations

import argparse
import random
import socket
import time
from datetime import datetime, timezone

HOSTS = ["esx9-01.lab.local", "esx9-02.lab.local", "esx9-03.lab.local", "esx9-04.lab.local"]
ESX = [
    (6, "Hostd", "Task Created : haTask-ha-host-vim.HostSystem.retrieveInternalCapability-{n}"),
    (6, "vpxa", "[Originator@6876 sub=vpxLro opID=lro-{n}] [VpxLRO] -- BEGIN task-internal-{n} -- vpxa"),
    (4, "vmkernel", "cpu{c}:{n})WARNING: NMP: nmp_DeviceRequestFastDeviceProbe:237: NMP device \"naa.6000c29{n}\" state in doubt; requested fast path state update..."),
    (3, "vobd", "[vob.storage.apd.start] Device or filesystem with identifier [naa.6000c29{n}] has entered the All Paths Down state."),
    (3, "Hostd", "Lost access to volume 6543e1f2-{n} (datastore-{c}) due to connectivity issues. Recovery attempt is in progress."),
    (5, "sshd", "Accepted keyboard-interactive/pam for root from 192.168.1.{c} port 5{n} ssh2"),
    (6, "vmkernel", "cpu{c}:{n})Vol3: 2128: Couldn't read volume header from : Timeout"),
    (2, "vmkernel", "cpu{c}:{n})ALERT: hpsa: CmdTimeout: Command timed out on adapter vmhba{c}"),
]
VC = [
    (6, "vpxd", "Session {n} logged in user VSPHERE.LOCAL\\Administrator from 192.168.1.{c}"),
    (4, "vpxd", "[Originator@6876 sub=InvtHostCnx] Host esx9-0{c}.lab.local is not responding"),
    (3, "vmon", "Service vsphere-ui exited with code 1, restarting"),
]


def rfc5424(pri: int, host: str, app: str, msg: str) -> bytes:
    ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return f"<{pri}>1 {ts} {host} {app} {random.randint(1000, 99999)} - [meta@6876 seq=\"{random.randint(1, 9999)}\"] {msg}".encode()


def rfc3164(pri: int, host: str, app: str, msg: str) -> bytes:
    ts = datetime.now().strftime("%b %e %H:%M:%S")
    return f"<{pri}>{ts} {host} {app}[{random.randint(100, 9999)}]: {msg}".encode()


def make() -> bytes:
    n, c = random.randint(1000, 99999), random.randint(1, 9)
    if random.random() < 0.8:
        sev, app, text = random.choice(ESX)
        return rfc5424(8 * 16 + sev, random.choice(HOSTS), app, text.format(n=n, c=c))     # facility local0
    sev, app, text = random.choice(VC)
    return rfc3164(8 * 3 + sev, "vcsa91", app, text.format(n=n, c=c % 4 + 1))           # facility daemon


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=514)
    ap.add_argument("--proto", choices=["udp", "tcp", "both"], default="both")
    ap.add_argument("--count", type=int, default=200)
    ap.add_argument("--rate", type=float, default=0, help="messages per second (0 = as fast as possible)")
    a = ap.parse_args()
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    tcp = None
    if a.proto in ("tcp", "both"):
        tcp = socket.create_connection((a.host, a.port), timeout=10)
    t0 = time.time()
    for i in range(a.count):
        msg = make()
        if tcp is not None and (a.proto == "tcp" or i % 2):
            tcp.sendall(str(len(msg)).encode() + b" " + msg)       # octet counting (RFC 6587), like ESXi
        else:
            udp.sendto(msg, (a.host, a.port))
        if a.rate:
            time.sleep(max(0.0, t0 + (i + 1) / a.rate - time.time()))
    if tcp is not None:
        tcp.close()
    print(f"sent {a.count} messages to {a.host}:{a.port} ({a.proto}) in {time.time() - t0:.1f} s")


if __name__ == "__main__":
    main()
