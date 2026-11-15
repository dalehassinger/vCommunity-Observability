"""x-posts source plug-in: recent posts of a list of X accounts (X API v2 recent search),
kept for `keep_days` in the work folder and printed as one document per post for the
vCommunity Ask indexer. Standard library + requests only.

Cost-aware: all accounts go into as few search queries as fit (512 characters each), later
syncs ask only for posts newer than the newest one already kept (since_id), and
`max_per_sync` caps how many posts one sync reads. When X answers 429 (rate limit) the
posts already kept are still indexed and the rest come on the next sync."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

TIMEOUT = 20
QUERY_MAX = 512           # X recent search limit for one query (self-serve tiers)
HANDLE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
FIELDS = "created_at,public_metrics,text,author_id,note_tweet,lang"


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def as_list(v) -> list[str]:
    if isinstance(v, str):
        v = re.split(r"[\s,;]+", v)
    return [str(x).strip() for x in (v or []) if str(x).strip()]


def as_int(v, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(float(v))))
    except (TypeError, ValueError):
        return default


def handles(raw) -> list[str]:
    out = []
    for h in as_list(raw):
        h = re.sub(r"^(https?://)?(www\.)?(x|twitter)\.com/", "", h, flags=re.I).strip("/").lstrip("@")
        if not HANDLE.match(h):
            raise RuntimeError(f"'{h}' is not an X account name (letters, digits and _, at most 15)")
        if h.lower() not in (x.lower() for x in out):
            out.append(h)
    return out


def queries(accounts: list[str], suffix: str) -> list[str]:
    """As few `(from:a OR from:b) suffix` queries as fit in QUERY_MAX characters."""
    out, cur = [], []
    def build(group):
        inner = " OR ".join(f"from:{a}" for a in group)
        return (f"({inner})" if len(group) > 1 else inner) + suffix
    for a in accounts:
        if cur and len(build(cur + [a])) > QUERY_MAX:
            out.append(build(cur))
            cur = []
        cur.append(a)
    if cur:
        out.append(build(cur))
    return out


def explain(r: requests.Response) -> str:
    try:
        body = r.json()
    except ValueError:
        body = {}
    detail = body.get("detail") or body.get("title") or (body.get("errors") or [{}])[0].get("message") or r.text[:200]
    if r.status_code == 401:
        return "X refused the Bearer token (401). Check the token in .env and restart vco-ask."
    if r.status_code == 402 or "credits" in str(detail).lower():
        return f"X says the app has no API credits or plan for this ({r.status_code}): {detail}"
    if r.status_code == 403:
        return f"X refused the request (403): {detail}. The app may need a plan that includes recent search."
    return f"X API error {r.status_code}: {detail}"


def doc_for(p: dict) -> dict:
    when = p["created_at"]
    date = when[:10]
    stamp = when[:16].replace("T", " ") + " UTC"
    m = p.get("metrics") or {}
    first = re.sub(r"\s+", " ", p["text"]).strip()
    title = f"@{p['username']} · {date}: " + (first[:70] + "…" if len(first) > 70 else first)
    url = f"https://x.com/{p['username']}/status/{p['id']}"
    stats = " · ".join(f"{label} {m[k]}" for k, label in (("like_count", "Likes"), ("retweet_count", "Reposts"),
                       ("reply_count", "Replies"), ("quote_count", "Quotes"), ("impression_count", "Views")) if k in m)
    who = f"@{p['username']}" + (f" ({p['name']})" if p.get("name") else "")
    body = "\n".join([f"# {who} on X, {stamp}", "", f"Published: {date}", f"Account: @{p['username']}", "",
                      p["text"].strip(), "", stats, "", f"Link: {url}"]).strip()
    meta = {"published": date, "created_at": when, "author": f"@{p['username']}", "author_name": p.get("name") or "",
            "metrics": m, "lang": p.get("lang") or ""}
    digest = hashlib.sha256(json.dumps([body, meta], sort_keys=True).encode()).hexdigest()
    return {"id": f"{p['username'].lower()}/{p['id']}", "title": title, "path": f"/{p['username']}/status/{p['id']}",
            "url": url, "text": body, "status": "", "meta": meta, "links": {}, "images": [], "hash": digest}


def main() -> int:
    opts = json.loads(os.environ.get("ASK_SOURCE_OPTIONS") or "{}")
    state = json.loads(os.environ.get("ASK_SOURCE_STATE") or "{}")
    work = Path(os.environ.get("ASK_SOURCE_WORKDIR") or ".")
    work.mkdir(parents=True, exist_ok=True)
    accounts = handles(opts.get("accounts"))
    if not accounts:
        raise RuntimeError("no accounts: add one X account per line under Accounts")
    env_name = str(opts.get("token_env") or "X_BEARER_TOKEN").strip()
    token = os.environ.get(env_name, "").strip()
    if not token:
        raise RuntimeError(f"no X API token: put {env_name}=<your Bearer token> in .env and restart vco-ask "
                           f"(docker compose up -d vco-ask)")
    api = str(opts.get("api_url") or "https://api.x.com").rstrip("/")
    keep_days = as_int(opts.get("keep_days"), 30, 1, 3650)
    budget = as_int(opts.get("max_per_sync"), 100, 10, 10000)
    suffix = ("" if opts.get("retweets") == "include" else " -is:retweet") + ("" if opts.get("replies") == "include" else " -is:reply")
    full = os.environ.get("ASK_SOURCE_FULL") == "1"

    store_path = work / "posts.json"
    try:
        posts: dict[str, dict] = {} if full else json.loads(store_path.read_text())
    except (OSError, ValueError):
        posts = {}
    since: dict[str, str] = {} if full else dict(state.get("since") or {})
    sess = requests.Session()
    sess.headers.update({"Authorization": f"Bearer {token}", "User-Agent": "vcommunity-ask-x-posts/0.1"})

    read = new = requests_made = 0
    stopped = ""
    for q in queries(accounts, suffix):
        key = hashlib.sha1(q.encode()).hexdigest()[:12]
        params = {"query": q, "tweet.fields": FIELDS, "expansions": "author_id", "user.fields": "username,name"}
        if since.get(key):
            params["since_id"] = since[key]
        newest = since.get(key, "")
        next_token = ""
        while read < budget:
            params["max_results"] = max(10, min(100, budget - read))
            if next_token:
                params["next_token"] = next_token
            try:
                r = sess.get(f"{api}/2/tweets/search/recent", params=params, timeout=TIMEOUT)
            except requests.RequestException as e:
                raise RuntimeError(f"could not reach {api}: {e}") from None
            requests_made += 1
            if r.status_code == 429:
                reset = r.headers.get("x-rate-limit-reset")
                wait = f", try again after {time.strftime('%H:%M UTC', time.gmtime(int(reset)))}" if reset and reset.isdigit() else ""
                stopped = f"X rate limit reached{wait}"
                log(stopped + "; posts read so far are kept")
                break
            if r.status_code >= 400:
                if posts and r.status_code >= 500:
                    stopped = explain(r)
                    log(stopped + "; keeping the posts from earlier syncs")
                    break
                raise RuntimeError(explain(r))
            body = r.json()
            users = {u["id"]: u for u in (body.get("includes") or {}).get("users") or []}
            for t in body.get("data") or []:
                read += 1
                u = users.get(t.get("author_id"), {})
                text = ((t.get("note_tweet") or {}).get("text")) or t.get("text") or ""
                pid = str(t["id"])
                if pid not in posts:
                    new += 1
                posts[pid] = {"id": pid, "created_at": t.get("created_at") or "", "text": text,
                              "username": u.get("username") or "unknown", "name": u.get("name") or "",
                              "metrics": t.get("public_metrics") or {}, "lang": t.get("lang") or ""}
                if not newest or int(pid) > int(newest):
                    newest = pid
            next_token = (body.get("meta") or {}).get("next_token") or ""
            if not next_token:
                break
        if stopped:
            break
        if newest and not next_token:
            since[key] = newest        # only when this query was read to the end
        if read >= budget and next_token:
            log(f"max posts per sync ({budget}) reached; older posts in this window are skipped")
            since[key] = newest

    # Keep only the listed accounts and the last keep_days.
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=keep_days)).strftime("%Y-%m-%dT%H:%M:%S")
    wanted = {a.lower() for a in accounts}
    before = len(posts)
    posts = {k: p for k, p in posts.items() if p["created_at"][:19] >= cutoff and p["username"].lower() in wanted}
    dropped = before - len(posts)
    store_path.write_text(json.dumps(posts))
    since = {k: v for k, v in since.items() if k in {hashlib.sha1(q.encode()).hexdigest()[:12] for q in queries(accounts, suffix)}}

    docs = [doc_for(p) for p in sorted(posts.values(), key=lambda p: p["created_at"], reverse=True)]
    newest_date = docs[0]["meta"]["published"] if docs else ""
    version = f"{len(docs)} posts from {len({d['meta']['author'] for d in docs})} of {len(accounts)} accounts" + \
              (f", newest {newest_date}" if newest_date else "")
    log(f"{version}; {new} new, {read} read in {requests_made} request(s), {dropped} dropped (older than {keep_days} days or account removed)")
    if stopped:
        version += f" ({stopped})"
    print(json.dumps({"version": version, "root": str(work), "unchanged": False,
                      "state": {"since": since, "version": version}, "documents": docs}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:
        log(f"ERROR: {e}")
        sys.exit(1)
