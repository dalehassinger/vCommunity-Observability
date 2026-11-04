"""web-site source plug-in: find the posts of a web site (sitemap, RSS/Atom feed or
link crawl), keep the ones inside the date range, pull out the article text as
markdown and print them as JSON documents for the vCommunity Ask indexer.

Polite by default: robots.txt is respected (including Crawl-delay), requests are
spaced by delay_seconds, only the start URL's host is fetched, and max_pages caps
the network fetches per sync. Pages are cached in the work folder with their
ETag / Last-Modified, so a re-sync only downloads what changed."""

from __future__ import annotations

import datetime as dt
import email.utils
import gzip
import hashlib
import json
import os
import re
import sys
import time
import urllib.robotparser
import xml.etree.ElementTree as ET
from collections import deque
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse, urlencode, parse_qsl

import requests

MAX_BYTES = 5 * 1024 * 1024
TIMEOUT = 20
MIN_TEXT = 200          # shorter extractions are list/tag pages or empty shells
FEED_PATHS = ("/index.xml", "/feed", "/feed/", "/rss.xml", "/atom.xml", "/feed.xml", "/rss")
SITEMAP_PATHS = ("/sitemap.xml", "/sitemap_index.xml", "/wp-sitemap.xml")
SKIP_EXT = re.compile(r"\.(png|jpe?g|gif|svg|webp|ico|pdf|zip|gz|tgz|mp4|mp3|mov|css|js|json|xml|txt|woff2?|ttf|exe|dmg|iso|ova)$", re.I)


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def as_list(v) -> list[str]:
    if isinstance(v, str):
        v = v.splitlines()
    return [x.strip() for x in (v or []) if str(x).strip()]


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """`**` matches across folders, `*` inside one; a trailing `/**` also matches the folder itself."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("/**", i) and i + 3 == len(pattern):
            out += "(?:/.*)?"
            i += 3
            continue
        if pattern.startswith("**", i):
            out += ".*"
            i += 2
            continue
        c = pattern[i]
        out += "[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c)
        i += 1
    return re.compile(f"^{out}$")


def to_date(value) -> dt.date | None:
    """ISO dates/datetimes and RFC 822 (RSS) dates -> date."""
    if not value:
        return None
    s = str(value).strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return dt.date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            return None
    try:
        return email.utils.parsedate_to_datetime(s).date()
    except (TypeError, ValueError):
        return None


# -- HTTP --------------------------------------------------------------------
class Fetcher:
    def __init__(self, user_agent: str, delay: float, max_pages: int):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": user_agent, "Accept-Language": "en;q=0.9, *;q=0.5"})
        self.ua = user_agent
        self.delay = delay
        self.max_pages = max_pages
        self.fetched = 0
        self.net_errors = 0
        self._last = 0.0
        self.robots: urllib.robotparser.RobotFileParser | None = None

    def _wait(self) -> None:
        gap = self.delay - (time.time() - self._last)
        if gap > 0:
            time.sleep(gap)
        self._last = time.time()

    def get(self, url: str, headers: dict | None = None, count: bool = True) -> requests.Response | None:
        """GET with size cap; None on network errors. `count` = counts toward max_pages."""
        if count:
            if self.fetched >= self.max_pages:
                return None
            self.fetched += 1
        self._wait()
        try:
            r = self.s.get(url, headers=headers or {}, timeout=TIMEOUT, stream=True, allow_redirects=True)
            body = b""
            for part in r.iter_content(65536):
                body += part
                if len(body) > MAX_BYTES:
                    log(f"  {url}: larger than {MAX_BYTES // 1048576} MB, skipped")
                    r.close()
                    return None
            r._content = body
            return r
        except requests.RequestException as e:
            self.net_errors += 1
            log(f"  {url}: {e.__class__.__name__}: {str(e)[:200]}")
            return None

    def allowed(self, url: str) -> bool:
        return self.robots is None or self.robots.can_fetch(self.ua, url)

    def load_robots(self, base: str) -> list[str]:
        """Read robots.txt; returns its Sitemap: URLs. Adopts a larger Crawl-delay."""
        rp = urllib.robotparser.RobotFileParser()
        r = self.get(urljoin(base, "/robots.txt"), count=False)
        if r is not None and r.status_code == 200:
            rp.parse(r.text.splitlines())
            cd = rp.crawl_delay(self.ua)
            if cd and float(cd) > self.delay:
                log(f"robots.txt asks for a {cd}s crawl delay")
                self.delay = min(float(cd), 30.0)
        else:
            rp.parse([])  # no robots.txt: everything allowed
        self.robots = rp
        return list(rp.site_maps() or [])


# -- discovery ---------------------------------------------------------------
def _xml(body: bytes, url: str) -> ET.Element | None:
    if url.endswith(".gz") or body[:2] == b"\x1f\x8b":
        try:
            body = gzip.decompress(body)[:MAX_BYTES * 4]
        except OSError:
            return None
    try:
        return ET.fromstring(body)
    except ET.ParseError:
        return None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def read_sitemaps(f: Fetcher, urls: list[str]) -> dict[str, str]:
    """{page_url: lastmod} from sitemaps and sitemap indexes (up to 50 sitemap files)."""
    pages: dict[str, str] = {}
    seen, queue = set(), deque(urls)
    while queue and len(seen) < 50:
        sm = queue.popleft()
        if sm in seen:
            continue
        seen.add(sm)
        r = f.get(sm, count=False)
        if r is None or r.status_code != 200:
            continue
        root = _xml(r.content, sm)
        if root is None:
            continue
        kind = _local(root.tag)
        for node in root:
            loc = lastmod = ""
            for child in node:
                name = _local(child.tag)
                if name == "loc":
                    loc = (child.text or "").strip()
                elif name == "lastmod":
                    lastmod = (child.text or "").strip()
            if not loc:
                continue
            if kind == "sitemapindex":
                queue.append(loc)
            else:
                pages[loc] = lastmod
    if pages:
        log(f"sitemap: {len(pages)} URLs from {len(seen)} file(s)")
    return pages


def read_feed(f: Fetcher, urls: list[str]) -> dict[str, str]:
    """{post_url: published} from the first RSS/Atom feed that answers."""
    for url in urls:
        r = f.get(url, count=False)
        if r is None or r.status_code != 200:
            continue
        root = _xml(r.content, url)
        if root is None or _local(root.tag) not in ("rss", "feed", "RDF"):
            continue
        posts: dict[str, str] = {}
        for item in root.iter():
            if _local(item.tag) not in ("item", "entry"):
                continue
            link = date = ""
            for child in item:
                name = _local(child.tag)
                if name == "link":
                    link = (child.get("href") if child.get("href") and child.get("rel", "alternate") == "alternate"
                            else (child.text or "").strip()) or link
                elif name in ("pubDate", "published", "date") or (name == "updated" and not date):
                    date = (child.text or "").strip()
            if link:
                posts[link] = date
        log(f"feed {url}: {len(posts)} posts")
        return posts
    return {}


# -- page handling -------------------------------------------------------------
def normalize(url: str) -> str:
    url, _ = urldefrag(url)
    p = urlparse(url)
    query = urlencode([(k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith(("utm_", "fbclid", "gclid"))])
    path = p.path or "/"
    return p._replace(scheme=p.scheme.lower(), netloc=p.netloc.lower(), path=path, query=query, params="").geturl()


def same_site(url: str, host: str) -> bool:
    h = urlparse(url).netloc.lower()
    return urlparse(url).scheme in ("http", "https") and h.removeprefix("www.") == host.removeprefix("www.")


def doc_id_for(url: str) -> str:
    p = urlparse(url)
    path = p.path.strip("/") or "index"
    if path.endswith((".html", ".htm")):
        path = path.rsplit(".", 1)[0]
    return path + (f"?{p.query}" if p.query else "")


def page_links(html: str, base: str) -> list[str]:
    try:
        import lxml.html
        tree = lxml.html.fromstring(html)
        hrefs = tree.xpath("//a/@href")
    except Exception:
        hrefs = re.findall(r"""<a\s[^>]*href=["']([^"']+)""", html, re.I)
    return [urljoin(base, h) for h in hrefs if h and not h.startswith(("mailto:", "javascript:", "tel:", "#"))]


def extract(html: str, url: str) -> tuple[str, dict]:
    """(markdown text, metadata) for one HTML page."""
    import trafilatura
    text = trafilatura.extract(html, url=url, output_format="markdown", include_tables=True,
                               include_formatting=True, include_links=False, include_images=False,
                               favor_recall=True) or ""
    meta: dict = {}
    try:
        md = trafilatura.extract_metadata(html, default_url=url)
    except Exception:
        md = None
    if md is not None:
        meta = {"title": md.title or "", "date": md.date or "", "author": md.author or "",
                "sitename": md.sitename or "", "description": md.description or "",
                "tags": list(md.tags or []), "categories": list(md.categories or [])}
    return text.strip(), meta


def build_document(url: str, text: str, meta: dict, published: dt.date | None) -> dict:
    title = meta.get("title") or next((ln[2:].strip() for ln in text.splitlines() if ln.startswith("# ")), "") or doc_id_for(url)
    lines = text.splitlines()
    if not (lines and lines[0].startswith("# ")):
        lines = [f"# {title}", ""] + lines
    if published:
        # Put the date in the text too, so "posts from 2024" style questions match it.
        lines = lines[:1] + ["", f"Published: {published.isoformat()}"] + lines[1:]
    body = "\n".join(lines).strip()
    keep = {k: v for k, v in {
        "published": published.isoformat() if published else "",
        "author": meta.get("author"), "site": meta.get("sitename"),
        "tags": meta.get("tags"), "categories": meta.get("categories"),
        "description": meta.get("description"),
    }.items() if v}
    digest = hashlib.sha256(json.dumps([body, keep], sort_keys=True).encode()).hexdigest()
    return {"id": doc_id_for(url), "title": title, "path": urlparse(url).path or "/", "url": url, "text": body,
            "status": "", "meta": keep, "links": {}, "images": [], "hash": digest}


# -- main ----------------------------------------------------------------------
def main() -> int:
    opts = json.loads(os.environ.get("ASK_SOURCE_OPTIONS") or "{}")
    work = Path(os.environ.get("ASK_SOURCE_WORKDIR") or "work")
    state = json.loads(os.environ.get("ASK_SOURCE_STATE") or "{}")
    full = bool(os.environ.get("ASK_SOURCE_FULL"))

    site = (opts.get("site_url") or "").strip()
    if not re.match(r"^https?://[^/]+", site):
        raise SystemExit("Site URL must start with http:// or https://")
    site = normalize(site)
    host = urlparse(site).netloc
    discovery = (opts.get("discovery") or "auto").strip()
    inc = [glob_to_regex(p) for p in as_list(opts.get("url_include"))]
    exc = [glob_to_regex(p) for p in as_list(opts.get("url_exclude"))]
    undated = (opts.get("undated") or "skip").strip()
    max_pages = int(float(opts.get("max_pages") or 500))
    delay = float(opts.get("delay_seconds") if opts.get("delay_seconds") not in (None, "") else 1)

    date_from, date_to = to_date(opts.get("date_from")), to_date(opts.get("date_to"))
    last_days = int(float(opts.get("last_days") or 0))
    today = dt.datetime.now(dt.timezone.utc).date()
    if last_days > 0:
        date_from = today - dt.timedelta(days=last_days)
    ranged = bool(date_from or date_to)
    if ranged:
        log(f"date range: {date_from or 'any'} .. {date_to or 'any'} (undated pages: {undated})")

    def wanted_path(url: str) -> bool:
        path = urlparse(url).path or "/"
        if inc and not any(r.match(path) for r in inc):
            return False
        return not any(r.match(path) for r in exc)

    def in_range(d: dt.date | None) -> bool:
        if not ranged:
            return True
        if d is None:
            return undated == "keep"
        return (not date_from or d >= date_from) and (not date_to or d <= date_to)

    f = Fetcher(opts.get("user_agent") or "vCommunity-Ask/0.1 (local knowledge indexer)", delay, max_pages)
    robot_maps = f.load_robots(site)

    sitemap: dict[str, str] = {}
    feed: dict[str, str] = {}
    if discovery in ("auto", "sitemap"):
        sitemap = read_sitemaps(f, robot_maps or [urljoin(site, p) for p in SITEMAP_PATHS[:1]])
        if not sitemap and not robot_maps:
            sitemap = read_sitemaps(f, [urljoin(site, p) for p in SITEMAP_PATHS[1:]])
    if discovery in ("auto", "feed"):
        feed_urls = [opts["feed_url"].strip()] if (opts.get("feed_url") or "").strip() else [urljoin(site, p) for p in FEED_PATHS]
        feed = read_feed(f, feed_urls)
    crawl = discovery == "crawl" or (discovery == "auto" and not sitemap and not feed)

    opts_hash = hashlib.sha256(json.dumps(opts, sort_keys=True).encode()).hexdigest()[:16]
    feed_n = {normalize(u): d for u, d in feed.items()}
    candidates: dict[str, dict] = {}
    for u, lm in sitemap.items():
        candidates.setdefault(normalize(u), {})["lastmod"] = lm
    for u, d in feed_n.items():
        candidates.setdefault(u, {})["feed_date"] = d
    candidates = {u: c for u, c in candidates.items() if same_site(u, host) and wanted_path(u) and not SKIP_EXT.search(urlparse(u).path)}

    # Cheap "nothing changed" answer: same URL list + lastmods + options (+ same day for rolling windows).
    fingerprint = hashlib.sha256(json.dumps([sorted(candidates.items()), opts_hash,
                                             today.isoformat() if last_days else ""], sort_keys=True).encode()).hexdigest()[:24]
    if not crawl and candidates and not full and state.get("fingerprint") == fingerprint:
        log(f"no change since last sync ({len(candidates)} URLs)")
        print(json.dumps({"version": state.get("version", ""), "root": str(work), "unchanged": True,
                          "state": state, "documents": []}))
        return 0

    cache_dir = work / "pages"
    cache_dir.mkdir(parents=True, exist_ok=True)

    def cache_path(url: str) -> Path:
        return cache_dir / (hashlib.sha1(url.encode()).hexdigest() + ".json")

    def load_cache(url: str) -> dict:
        if full:
            return {}
        try:
            return json.loads(cache_path(url).read_text())
        except (OSError, ValueError):
            return {}

    stats = {"fetched": 0, "cached": 0, "not_modified": 0, "out_of_range": 0, "too_short": 0, "blocked": 0, "errors": 0}

    def process(url: str, hint: dict) -> tuple[dict | None, list[str]]:
        """Returns (document or None, links found on the page)."""
        feed_date = to_date(hint.get("feed_date"))
        lastmod = hint.get("lastmod", "")
        # A page last modified before the range started was also published before it.
        if date_from and to_date(lastmod) and to_date(lastmod) < date_from and not crawl:
            stats["out_of_range"] += 1
            return None, []
        if feed_date and not in_range(feed_date):
            stats["out_of_range"] += 1
            return None, []
        cached = load_cache(url)
        if cached and lastmod and cached.get("lastmod") == lastmod:
            stats["cached"] += 1
            entry = cached
        else:
            if not f.allowed(url):
                stats["blocked"] += 1
                return None, []
            headers = {}
            if cached.get("etag"):
                headers["If-None-Match"] = cached["etag"]
            if cached.get("last_modified"):
                headers["If-Modified-Since"] = cached["last_modified"]
            r = f.get(url, headers)
            if r is None:
                if cached:
                    stats["cached"] += 1
                    entry = cached
                else:
                    stats["errors"] += 1
                    return None, []
            elif r.status_code == 304 and cached:
                stats["not_modified"] += 1
                entry = cached
            elif r.status_code != 200 or "html" not in r.headers.get("Content-Type", "html"):
                stats["errors"] += 1
                return None, []
            else:
                stats["fetched"] += 1
                final = normalize(r.url)
                if not same_site(final, host):
                    return None, []
                html = r.text
                text, meta = extract(html, final)
                entry = {"url": url, "final": final, "etag": r.headers.get("ETag", ""),
                         "last_modified": r.headers.get("Last-Modified", ""), "lastmod": lastmod,
                         "text": text, "meta": meta, "links": page_links(html, final) if crawl else []}
                cache_path(url).write_text(json.dumps(entry))
        links = entry.get("links") or []
        if not wanted_path(url):
            return None, links
        published = feed_date or to_date((entry.get("meta") or {}).get("date"))
        if not in_range(published):
            stats["out_of_range"] += 1
            return None, links
        if len(entry.get("text") or "") < MIN_TEXT:
            stats["too_short"] += 1
            return None, links
        return build_document(entry.get("final") or url, entry["text"], entry.get("meta") or {}, published), links

    docs: dict[str, dict] = {}
    if crawl:
        log(f"crawling from {site} (max {max_pages} pages)")
        queue, seen = deque([site]), {site}
        while queue and f.fetched < max_pages:
            url = queue.popleft()
            doc, links = process(url, candidates.get(url, {}))
            if doc:
                docs.setdefault(doc["id"], doc)
            for link in links:
                n = normalize(link)
                if n not in seen and same_site(n, host) and not SKIP_EXT.search(urlparse(n).path):
                    seen.add(n)
                    queue.append(n)
    else:
        # Newest first, so max_pages keeps the most recent posts.
        order = sorted(candidates, key=lambda u: (candidates[u].get("feed_date") and to_date(candidates[u]["feed_date"]) or
                                                 to_date(candidates[u].get("lastmod")) or dt.date.min), reverse=True)
        warned = False
        for url in order:
            if f.fetched >= max_pages and not warned:
                # Keep going: pages already in the cache are still indexed, new ones wait for the next sync.
                log(f"max pages ({max_pages}) reached; the rest come from the cache or the next sync")
                warned = True
            doc, _ = process(url, candidates[url])
            if doc:
                docs.setdefault(doc["id"], doc)

    # Site unreachable (DNS, proxy, firewall): fail the sync instead of reporting
    # zero pages, which would remove everything indexed before.
    if not docs and f.net_errors and not (stats["fetched"] or stats["cached"] or stats["not_modified"]):
        raise RuntimeError(f"could not reach {site} ({f.net_errors} network errors, see the log above)")
    dates = sorted(d["meta"]["published"] for d in docs.values() if d["meta"].get("published"))
    version = f"{len(docs)} pages" + (f", newest {dates[-1]}" if dates else "")
    log(f"{version}; " + ", ".join(f"{k} {v}" for k, v in stats.items() if v))
    new_state = {"fingerprint": fingerprint if not crawl else "", "version": version, "options": opts_hash}
    print(json.dumps({"version": version, "root": str(work), "unchanged": False,
                      "state": new_state, "documents": list(docs.values())}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:
        log(f"ERROR: {e}")
        sys.exit(1)
