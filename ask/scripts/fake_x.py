"""Stand-in for the X API v2 recent search, for tests of the x-posts source (no account needed).

    python ask/scripts/fake_x.py 11557        # then set the source's API address to http://127.0.0.1:11557

Token "good-token" is accepted, anything else gets 401. Each account (from:name in the query)
has three posts, newest first; since_id, max_results and next_token work like X's. Account
"ratelimited" answers 429. Standard library only."""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

NOW = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
POSTS: list[dict] = []
EXTRA: list[dict] = []        # tests append here to simulate new posts


def make(user: str, n: int, hours_ago: float) -> dict:
    uid = str(1000 + sum(map(ord, user)))
    pid = str(1_900_000_000_000_000_000 + int((NOW.timestamp() - hours_ago * 3600)) * 10 + n)
    return {"id": pid, "author_id": uid, "user": user,
            "created_at": (NOW - dt.timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "text": f"Post {n} from {user} about VCF 9 #{n}", "public_metrics": {"like_count": n, "retweet_count": 0,
                                                                            "reply_count": 1, "quote_count": 0, "impression_count": 100 * n}}


def all_posts() -> list[dict]:
    return sorted(POSTS + EXTRA, key=lambda p: int(p["id"]), reverse=True)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, headers=None):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path != "/2/tweets/search/recent":
            return self.send(404, {"title": "Not Found"})
        if self.headers.get("Authorization") != "Bearer good-token":
            return self.send(401, {"title": "Unauthorized", "detail": "Unauthorized", "status": 401})
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        users = {x.lower() for x in re.findall(r"from:(\w+)", q.get("query", ""))}
        if "ratelimited" in users:
            return self.send(429, {"title": "Too Many Requests"}, {"x-rate-limit-reset": str(int(NOW.timestamp()) + 900)})
        n = int(q.get("max_results", 10))
        if not 10 <= n <= 100:
            return self.send(400, {"errors": [{"message": "max_results must be between 10 and 100"}]})
        since = int(q.get("since_id", 0))
        rows = [p for p in all_posts() if p["user"].lower() in users and int(p["id"]) > since]
        start = int(q.get("next_token") or 0)
        page = rows[start:start + n]
        meta = {"result_count": len(page)}
        if start + n < len(rows):
            meta["next_token"] = str(start + n)
        data = [{k: v for k, v in p.items() if k != "user"} for p in page]
        inc = {"users": [{"id": p["author_id"], "username": p["user"], "name": p["user"].title()} for p in page]}
        self.send(200, {"data": data, "includes": inc, "meta": meta} if data else {"meta": meta})


for i, user in enumerate(["bpatvmware", "vcrocs", "lamw"]):
    for n in (1, 2, 3):
        POSTS.append(make(user, n, hours_ago=i * 5 + n * 10))


def serve(port: int) -> ThreadingHTTPServer:
    import threading
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 11557
    print(f"fake X API on http://127.0.0.1:{port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
