"""Stand-in SMTP server for tests: accepts every message and saves it as a .eml file.

    python scripts/fake_smtp.py 2525 /tmp/mail      # then set the Logs mail server to 127.0.0.1:2525

The smoke test imports serve() and runs it in a thread. No TLS and no login (use security
"none" and no user). Standard library only.
"""
from __future__ import annotations

import socketserver
import sys
import threading
from pathlib import Path


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        out: Path = self.server.outdir  # type: ignore[attr-defined]
        w = self.wfile.write
        w(b"220 fake-smtp ready\r\n")
        while True:
            line = self.rfile.readline()
            if not line:
                return
            cmd = line.decode("utf-8", "replace").strip().upper()
            if cmd.startswith(("EHLO", "HELO")):
                w(b"250 fake-smtp\r\n")
            elif cmd.startswith(("MAIL", "RCPT", "RSET", "NOOP")):
                w(b"250 OK\r\n")
            elif cmd == "DATA":
                w(b"354 end with .\r\n")
                data = []
                while True:
                    part = self.rfile.readline()
                    if not part or part in (b".\r\n", b".\n"):
                        break
                    data.append(part[1:] if part.startswith(b"..") else part)
                with self.server.lock:  # type: ignore[attr-defined]
                    n = len(list(out.glob("*.eml"))) + 1
                    (out / f"{n:04d}.eml").write_bytes(b"".join(data))
                w(b"250 queued\r\n")
            elif cmd == "QUIT":
                w(b"221 bye\r\n")
                return
            else:
                w(b"502 not implemented\r\n")


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def serve(port: int, outdir: Path) -> _Server:
    """Start in a background thread; returns the server (call .shutdown() to stop)."""
    outdir.mkdir(parents=True, exist_ok=True)
    srv = _Server(("127.0.0.1", port), _Handler)
    srv.outdir, srv.lock = outdir, threading.Lock()  # type: ignore[attr-defined]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 2525
    folder = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("fake-mail")
    serve(port, folder)
    print(f"fake SMTP on 127.0.0.1:{port}, messages saved in {folder}", flush=True)
    threading.Event().wait()
