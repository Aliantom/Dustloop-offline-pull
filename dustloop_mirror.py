#!/usr/bin/env python3
"""
Dustloop Wiki Mirror
====================
Downloads the Guilty Gear -Strive- section of dustloop.com for offline access.

How it works (and why it isn't plain `wget --mirror` any more):

  1. Ask the wiki's API for every page titled "GGST/..." (plus the GGST main
     page), along with each page's latest revision id.
  2. Download each page that is new or has changed since the last run, plus
     every stylesheet, image and font that page needs. Nothing outside the
     GGST section is crawled, so a full pass fits in a single run.
  3. Rewrite links in each page *as it is saved*, so it renders correctly
     even if the run is cut off partway through. (wget only rewrites links
     after its whole crawl finishes, which never happened here.)
  4. Save progress after every page, so a run that hits its time budget
     picks up where it left off next time.

Usage:
    python dustloop_mirror.py                     # normal run
    python dustloop_mirror.py --budget-minutes 25 # stop starting pages after 25 min
    python dustloop_mirror.py --only "GGST/Venom/Combos"   # just these pages
    python dustloop_mirror.py --refetch-all       # ignore saved revision ids

Output:
    ~/dustloop_mirror/site/w/...          mirrored pages (.html)
    ~/dustloop_mirror/site/wiki/images/   images
    ~/dustloop_mirror/site/_assets/css/   stylesheets
    ~/dustloop_mirror/state.json          progress, so runs can resume
    ~/dustloop_mirror/status.md           human-readable summary of the last run
    ~/dustloop_mirror/mirror.log          run history
"""

import argparse
import hashlib
import html
import json
import logging
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Override with DUSTLOOP_BASE for local testing against a fake wiki.
BASE = os.environ.get("DUSTLOOP_BASE", "https://www.dustloop.com").rstrip("/")
HOSTS = {"www.dustloop.com", "dustloop.com", urllib.parse.urlsplit(BASE).netloc}

MAIN_TITLE = "Guilty Gear -Strive-"
TITLE_PREFIXES = ["GGST/"]

# Fetched first, in this order, before everything else.
PRIORITY_TITLES = [
    "GGST/Venom/Combos",
    "GGST/I-No/Combos",
    MAIN_TITLE,
]

API_CANDIDATES = [f"{BASE}/wiki/api.php", f"{BASE}/w/api.php"]

OUTPUT_DIR = Path(os.environ.get("DUSTLOOP_OUTPUT", Path.home() / "dustloop_mirror"))
SITE_DIR = OUTPUT_DIR / "site"
STATE_FILE = OUTPUT_DIR / "state.json"
STATUS_FILE = OUTPUT_DIR / "status.md"
LOG_FILE = OUTPUT_DIR / "mirror.log"

# Politeness: about one request per second. Don't lower this, Dustloop is
# a community-run wiki.
WAIT_SECONDS = float(os.environ.get("DUSTLOOP_WAIT", "1.0"))
USER_AGENT = "DustloopOfflineMirror/2.0 (personal offline reader)"

# Stylesheets change occasionally; images effectively never do.
CSS_MAX_AGE = timedelta(days=7)
# YouTube combo videos (Combos pages only), downloaded with yt-dlp after all
# pages are done. 360p keeps the whole set small; raise it if you want.
YOUTUBE_MAX_HEIGHT = int(os.environ.get("DUSTLOOP_YT_HEIGHT", "360"))
YOUTUBE_DIR_NAME = "_media/yt"
YOUTUBE_RETRY_AFTER = timedelta(days=2)
# Matches embeds, links and thumbnails; keep in sync with server.py.
YOUTUBE_ID_RE = re.compile(
    r"(?:youtube(?:-nocookie)?\.com/(?:embed/|watch\?v=|shorts/|v/)|youtu\.be/|ytimg\.com/vi/)"
    r"([A-Za-z0-9_-]{11})")

# Don't keep re-requesting URLs that 404'd for this long.
MISSING_RETRY_AFTER = timedelta(days=7)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class Fetcher:
    """Polite HTTP client: one request at a time, with a pause between."""

    def __init__(self, wait: float):
        self.wait = wait
        self.last = 0.0
        self.requests = 0

    def get(self, url: str) -> Tuple[Optional[int], bytes]:
        for attempt in range(3):
            pause = self.wait * random.uniform(0.5, 1.5) - (time.time() - self.last)
            if pause > 0:
                time.sleep(pause)
            self.last = time.time()
            self.requests += 1
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return resp.status, resp.read()
            except urllib.error.HTTPError as exc:
                if exc.code in (404, 410, 403):
                    return exc.code, b""
                if exc.code == 429 or exc.code >= 500:
                    time.sleep(10 * (attempt + 1))
                    continue
                return exc.code, b""
            except (urllib.error.URLError, OSError) as exc:
                logging.warning("Network error on %s (%s), retrying", url, exc)
                time.sleep(5 * (attempt + 1))
        return None, b""


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def now() -> datetime:
    return datetime.now(timezone.utc)


def load_state() -> dict:
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = {}
    state.setdefault("pages", {})    # title -> {"revid": int, "fetched": iso}
    state.setdefault("missing", {})  # url -> iso of last 404
    state.setdefault("css", {})      # load.php query -> iso fetched
    state.setdefault("yt_failed", {})  # youtube id -> {"when": iso, "why": str}
    state.setdefault("gone", {})     # local asset path -> iso of last 404
    return state


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(STATE_FILE)


def recently_missing(state: dict, url: str) -> bool:
    when = state["missing"].get(url)
    if not when:
        return False
    return now() - datetime.fromisoformat(when) < MISSING_RETRY_AFTER


# ---------------------------------------------------------------------------
# Page discovery
# ---------------------------------------------------------------------------

def api_query(fetcher: Fetcher, api: str, params: dict) -> Optional[dict]:
    url = f"{api}?{urllib.parse.urlencode(params)}"
    status, body = fetcher.get(url)
    if status != 200:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return None


def discover_pages(fetcher: Fetcher) -> Dict[str, Optional[int]]:
    """
    Return {title: latest revision id} for every GGST page. Returns {} if the
    API can't be reached; the caller then discovers pages by following
    GGST links from the pages it downloads.
    """
    for api in API_CANDIDATES:
        found: Dict[str, Optional[int]] = {}
        ok = True
        for prefix in TITLE_PREFIXES:
            params = {
                "action": "query", "format": "json",
                "generator": "allpages", "gapnamespace": "0",
                "gapprefix": prefix, "gaplimit": "500",
                "gapfilterredir": "nonredirects",
                "prop": "info",
            }
            while True:
                data = api_query(fetcher, api, params)
                if data is None:
                    ok = False
                    break
                for page in data.get("query", {}).get("pages", {}).values():
                    found[page["title"]] = page.get("lastrevid")
                if "continue" not in data:
                    break
                params = {**params, **data["continue"]}
            if not ok:
                break
        if not ok:
            logging.warning("Page-list API not usable at %s", api)
            continue
        data = api_query(fetcher, api, {
            "action": "query", "format": "json",
            "titles": MAIN_TITLE, "prop": "info",
        })
        for page in (data or {}).get("query", {}).get("pages", {}).values():
            if "missing" not in page:
                found[page["title"]] = page.get("lastrevid")
        logging.info("Discovered %d GGST pages via %s", len(found), api)
        return found
    logging.warning("Page-list API unreachable; discovering pages from links only.")
    return {}


def title_to_url(title: str) -> str:
    return f"{BASE}/w/{urllib.parse.quote(title.replace(' ', '_'), safe='/:,()!*~-_.')}"


def title_to_path(title: str) -> Path:
    return SITE_DIR / "w" / (title.replace(" ", "_") + ".html")


def is_ggst_title(title: str) -> bool:
    return title == MAIN_TITLE or any(title.startswith(p) for p in TITLE_PREFIXES)


# ---------------------------------------------------------------------------
# Rewriting pages and stylesheets
# ---------------------------------------------------------------------------

SCRIPT_RE = re.compile(r"<script\b[^>]*>.*?</script\s*>", re.I | re.S)
NOSCRIPT_RE = re.compile(r"</?noscript\b[^>]*>", re.I)
SRCSET_RE = re.compile(r"\s(?:data-)?srcset\s*=\s*(\"[^\"]*\"|'[^']*')", re.I)
LINK_TAG_RE = re.compile(r"<link\b[^>]*>", re.I)
IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.I)
MEDIA_TAG_RE = re.compile(r"<(?:video|source|audio|track)\b[^>]*>", re.I)
ATTR_RE = re.compile(r"(\s(src|href|data-src|poster)\s*=\s*)(\"[^\"]*\"|'[^']*')", re.I)
CSS_URL_RE = re.compile(r"url\(\s*(['\"]?)([^'\")]+)\1\s*\)", re.I)
CSS_IMPORT_RE = re.compile(r"@import\s+(['\"])([^'\"]+)\1", re.I)
PAGE_LINK_RE = re.compile(r"href=\"/w/([^\"#?]+)", re.I)


def to_site_path(url: str, base_url: str) -> Optional[str]:
    """Resolve a URL as it appears in a page; return its path (with query) if
    it lives on Dustloop, otherwise None."""
    url = html.unescape(url.strip())
    if not url or url.startswith(("data:", "javascript:", "mailto:", "#")):
        return None
    full = urllib.parse.urljoin(base_url, url)
    parts = urllib.parse.urlsplit(full)
    if parts.netloc not in HOSTS:
        return None
    return parts.path + (f"?{parts.query}" if parts.query else "")


def is_stylesheet_url(path: str) -> bool:
    return path.split("?", 1)[0].endswith("/load.php") and "only=styles" in path


def css_local_path(path: str) -> str:
    query = path.split("?", 1)[1] if "?" in path else ""
    digest = hashlib.sha1(query.encode("utf-8")).hexdigest()[:16]
    return f"/_assets/css/{digest}.css"


def asset_local_path(path: str) -> str:
    """Images, fonts, svgs: same path as on the site, query string dropped."""
    return path.split("?", 1)[0]


def local_file(site_path: str) -> Path:
    return SITE_DIR / urllib.parse.unquote(site_path.lstrip("/"))


class Mirror:
    def __init__(self, fetcher: Fetcher, state: dict):
        self.fetcher = fetcher
        self.state = state
        self.done_assets: Set[str] = set()   # handled during this run
        self.stats = {"pages": 0, "assets": 0, "css": 0, "failed": 0}
        self.failed_pages: List[str] = []

    # -- assets ------------------------------------------------------------

    def download(self, site_path: str, dest: Path) -> bool:
        url = BASE + site_path
        if recently_missing(self.state, url):
            return False
        status, body = self.fetcher.get(url)
        if status != 200:
            if status in (404, 410):
                self.state["missing"][url] = now().isoformat()
                self.state["gone"][asset_local_path(site_path)] = now().isoformat()
            self.stats["failed"] += 1
            logging.info("  %s -> %s", site_path, status)
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
        return True

    def ensure_asset(self, site_path: str) -> str:
        """Make sure an image/font is on disk; return the path to link to."""
        local = asset_local_path(site_path)
        if local in self.done_assets:
            return local
        self.done_assets.add(local)
        dest = local_file(local)
        if not dest.exists():
            if self.download(site_path, dest):
                self.stats["assets"] += 1
        return local

    def ensure_stylesheet(self, site_path: str) -> str:
        """Download a load.php stylesheet, rewrite the urls inside it, and
        return the local path to link to."""
        local = css_local_path(site_path)
        if local in self.done_assets:
            return local
        self.done_assets.add(local)
        dest = local_file(local)
        query = site_path.split("?", 1)[-1]
        fetched = self.state["css"].get(query)
        fresh = (dest.exists() and fetched
                 and now() - datetime.fromisoformat(fetched) < CSS_MAX_AGE)
        if fresh:
            return local
        url = BASE + site_path
        status, body = self.fetcher.get(url)
        if status != 200:
            self.stats["failed"] += 1
            logging.info("  stylesheet %s -> %s", site_path[:120], status)
            return local if dest.exists() else site_path
        css = body.decode("utf-8", errors="replace")
        css = self.rewrite_css(css, url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(css, encoding="utf-8")
        self.state["css"][query] = now().isoformat()
        self.stats["css"] += 1
        return local

    def rewrite_css(self, css: str, base_url: str) -> str:
        def fix_url(m):
            quote, target = m.group(1), m.group(2)
            path = to_site_path(target, base_url)
            if path is None:
                return m.group(0)
            if is_stylesheet_url(path):
                return f"url({quote}{self.ensure_stylesheet(path)}{quote})"
            return f"url({quote}{self.ensure_asset(path)}{quote})"

        def fix_import(m):
            path = to_site_path(m.group(2), base_url)
            if path is None:
                return m.group(0)
            return f'@import "{self.ensure_stylesheet(path)}"'

        css = CSS_IMPORT_RE.sub(fix_import, css)
        return CSS_URL_RE.sub(fix_url, css)

    # -- pages -------------------------------------------------------------

    def rewrite_page(self, page: str, page_url: str) -> str:
        page = SCRIPT_RE.sub("", page)
        page = NOSCRIPT_RE.sub("", page)
        page = SRCSET_RE.sub("", page)

        def fix_attr(m, tag_kind):
            prefix, name, quoted = m.group(1), m.group(2).lower(), m.group(3)
            q = quoted[0]
            path = to_site_path(quoted[1:-1], page_url)
            if path is None:
                return m.group(0)
            if tag_kind == "link":
                if name != "href":
                    return m.group(0)
                if is_stylesheet_url(path):
                    new = self.ensure_stylesheet(path)
                else:
                    return f"{prefix}{q}{html.escape(path, quote=True)}{q}"
            elif tag_kind == "img":
                new = self.ensure_asset(path)
                if name == "data-src":
                    # Lazy-loaded image: promote to a real src.
                    return f' src={q}{html.escape(new, quote=True)}{q}'
            else:
                # <a href>, etc.: keep the link on the mirror.
                new = path
            return f"{prefix}{q}{html.escape(new, quote=True)}{q}"

        def fix_link_tag(m):
            tag = m.group(0)
            rel = re.search(r"\srel\s*=\s*[\"']([^\"']*)", tag, re.I)
            rel = rel.group(1).lower() if rel else ""
            if "stylesheet" not in rel:
                # preload/modulepreload/alternate/etc. only cause requests
                # to the live site; drop them.
                if any(r in rel for r in ("preload", "modulepreload", "prefetch", "preconnect", "dns-prefetch")):
                    return ""
                return tag
            return ATTR_RE.sub(lambda a: fix_attr(a, "link"), tag)

        def fix_img_tag(m):
            tag = m.group(0)
            if re.search(r"\sdata-src\s*=", tag, re.I):
                # drop the placeholder src; data-src becomes the real one
                tag = re.sub(r"\ssrc\s*=\s*(\"[^\"]*\"|'[^']*')", "", tag, count=1, flags=re.I)
            return ATTR_RE.sub(lambda a: fix_attr(a, "img"), tag)

        page = LINK_TAG_RE.sub(fix_link_tag, page)
        page = IMG_TAG_RE.sub(fix_img_tag, page)
        # <video>/<source> files hosted on Dustloop: download them too.
        page = MEDIA_TAG_RE.sub(fix_img_tag, page)

        # Inline styles and <style> blocks: url(...) references.
        page = CSS_URL_RE.sub(
            lambda m: self.rewrite_css(m.group(0), page_url), page)

        # Absolute dustloop links in <a href> etc. -> keep them on the mirror.
        def fix_other(m):
            prefix, quoted = m.group(1), m.group(3)
            q = quoted[0]
            raw = html.unescape(quoted[1:-1])
            parts = urllib.parse.urlsplit(raw)
            if parts.scheme in ("http", "https") and parts.netloc in HOSTS:
                path = parts.path + (f"?{parts.query}" if parts.query else "")
                path += f"#{parts.fragment}" if parts.fragment else ""
                return f"{prefix}{q}{html.escape(path, quote=True)}{q}"
            return m.group(0)

        page = ATTR_RE.sub(fix_other, page)
        return page

    def fetch_page(self, title: str) -> Tuple[bool, List[str]]:
        url = title_to_url(title)
        status, body = self.fetcher.get(url)
        if status != 200:
            logging.warning("Page %s -> %s", title, status)
            self.stats["failed"] += 1
            self.failed_pages.append(f"{title} ({status})")
            return False, []
        page = body.decode("utf-8", errors="replace")
        linked = sorted({
            urllib.parse.unquote(m).replace("_", " ")
            for m in PAGE_LINK_RE.findall(page)
        })
        before = dict(self.stats)
        page = self.rewrite_page(page, url)
        dest = title_to_path(title)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(page, encoding="utf-8")
        self.stats["pages"] += 1
        logging.info(
            "Saved %s (+%d images/fonts, +%d stylesheets)", title,
            self.stats["assets"] - before["assets"], self.stats["css"] - before["css"],
        )
        return True, [t for t in linked if is_ggst_title(t)]


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

AUDIT_REF_RE = re.compile(r"\s(?:src|poster)\s*=\s*\"([^\"]+)\"", re.I)
AUDIT_CSS_RE = re.compile(r"<link\b[^>]*stylesheet[^>]*href=\"([^\"]+)\"", re.I)
IFRAME_RE = re.compile(r"<iframe\b[^>]*>", re.I)
VIDEO_TAG_RE = re.compile(r"<video\b", re.I)
EMBED_HOST_RE = re.compile(r"(?:data-)?src=\"(?:https?:)?//([^/\"]+)", re.I)


def page_titles_on_disk() -> List[str]:
    w = SITE_DIR / "w"
    titles = []
    for f in list((w / "GGST").rglob("*.html")) + list(w.glob("Guilty_Gear_-Strive-*.html")):
        titles.append(f.relative_to(w).as_posix()[:-5].replace("_", " "))
    return sorted(titles)


def audit(state: dict) -> dict:
    """
    Check every GGST page on disk for things that would make it render
    badly: still in the old (wget) format, or referencing a stylesheet,
    image, font or video that isn't on disk. Files the wiki itself says
    don't exist (404) are not counted against a page.
    """
    css_cache: Dict[str, List[str]] = {}
    result = {"pages": 0, "good": 0, "old": [], "broken": {}, "missing_files": 0,
              "embeds": {}, "video_tags": 0}

    def exists(ref: str) -> bool:
        local = urllib.parse.unquote(html.unescape(ref)).split("?", 1)[0]
        return local in state["gone"] or (SITE_DIR / local.lstrip("/")).exists()

    for title in page_titles_on_disk():
        result["pages"] += 1
        text = title_to_path(title).read_text(encoding="utf-8", errors="replace")
        if "/wiki/load.php?" in text or "<script" in text or title not in state["pages"]:
            result["old"].append(title)
            continue
        refs = [r for r in AUDIT_REF_RE.findall(text) if r.startswith("/") and not r.startswith("/w/")]
        for css in AUDIT_CSS_RE.findall(text):
            refs.append(css)
            if css not in css_cache:
                f = SITE_DIR / css.lstrip("/")
                body = f.read_text(encoding="utf-8", errors="replace") if f.exists() else ""
                css_cache[css] = [u for _, u in CSS_URL_RE.findall(body) if u.startswith("/")]
            refs += css_cache[css]
        refs += [u for _, u in CSS_URL_RE.findall(text) if u.startswith("/")]
        missing = sorted({r for r in refs if not exists(r)})
        if missing and len(result.setdefault("samples", [])) < 3:
            # Show the markup around a missing file, to see why it wasn't fetched.
            ref = missing[0]
            at = text.find(ref)
            start = text.rfind("<", 0, max(at, 0))
            start = max(0, start - 200)
            snippet = text[start:at + len(ref) + 150] if at >= 0 else ref
            result["samples"].append(f"{title}: " + " ".join(snippet.split()))
        if missing:
            result["broken"][title] = missing
            result["missing_files"] += len(missing)
        else:
            result["good"] += 1
        for tag in IFRAME_RE.findall(text):
            for host in EMBED_HOST_RE.findall(tag):
                result["embeds"][host] = result["embeds"].get(host, 0) + 1
        result["video_tags"] += len(VIDEO_TAG_RE.findall(text))
    return result


def plan(state: dict, discovered: Dict[str, Optional[int]], refetch_all: bool,
         broken: Optional[Set[str]] = None) -> List[str]:
    """Order: priority pages, then pages never downloaded, then pages still in
    the old format or with missing files, then changed pages."""
    broken = broken or set()
    known = set(discovered) | set(state["pages"]) | set(PRIORITY_TITLES) | set(page_titles_on_disk())

    def needs_fetch(title: str) -> bool:
        if refetch_all or not title_to_path(title).exists() or title in broken:
            return True
        saved = state["pages"].get(title)
        if not saved:
            return True
        latest = discovered.get(title)
        return latest is not None and latest != saved.get("revid")

    todo = [t for t in PRIORITY_TITLES if needs_fetch(t)]
    rest = sorted(t for t in known if t not in PRIORITY_TITLES and needs_fetch(t))
    missing_first = [t for t in rest if not title_to_path(t).exists()]
    repair = [t for t in rest if title_to_path(t).exists() and
              (t in broken or t not in state["pages"])]
    changed = [t for t in rest if t not in missing_first and t not in repair]
    return todo + missing_first + repair + changed


def combos_youtube_ids() -> Dict[str, List[str]]:
    """{video id: [Combos pages it appears on]} for every GGST Combos page."""
    ids: Dict[str, List[str]] = {}
    for title in page_titles_on_disk():
        if not title.endswith("/Combos"):
            continue
        text = title_to_path(title).read_text(encoding="utf-8", errors="replace")
        for vid in dict.fromkeys(YOUTUBE_ID_RE.findall(text)):
            ids.setdefault(vid, []).append(title)
    return ids


def youtube_file(vid: str) -> Path:
    return SITE_DIR / YOUTUBE_DIR_NAME / f"{vid}.mp4"


def download_youtube(state: dict, deadline: Optional[datetime]) -> dict:
    """Download combo videos with yt-dlp until done or out of time."""
    import shutil
    import subprocess
    ids = combos_youtube_ids()
    result = {"total": len(ids), "downloaded": 0, "failed_now": 0,
              "remaining": 0, "skipped": None}
    todo = []
    for vid in ids:
        if youtube_file(vid).exists():
            continue
        failed = state["yt_failed"].get(vid)
        if failed and now() - datetime.fromisoformat(failed["when"]) < YOUTUBE_RETRY_AFTER:
            continue
        todo.append(vid)
    result["remaining"] = len(todo)
    if not todo:
        return result
    ytdlp = shutil.which("yt-dlp")
    if not ytdlp:
        result["skipped"] = "yt-dlp not installed"
        logging.warning("yt-dlp not installed; skipping %d combo videos", len(todo))
        return result

    (SITE_DIR / YOUTUBE_DIR_NAME).mkdir(parents=True, exist_ok=True)
    h = YOUTUBE_MAX_HEIGHT
    fmt = (f"b[ext=mp4][height<={h}]/bv*[ext=mp4][height<={h}]+ba[ext=m4a]/"
           f"b[height<={h}]/bv*[height<={h}]+ba/b")
    streak = 0
    for n, vid in enumerate(todo):
        if deadline and now() > deadline:
            logging.info("Time budget reached during videos; continuing next run.")
            break
        out = youtube_file(vid)
        cmd = [ytdlp, "-q", "--no-warnings", "--no-playlist", "-f", fmt,
               "--merge-output-format", "mp4", "--max-filesize", "80M",
               "--sleep-requests", "1", "-o", str(out.with_suffix(".%(ext)s")),
               f"https://www.youtube.com/watch?v={vid}"]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            ok = proc.returncode == 0 and out.exists()
            why = (proc.stderr.strip().splitlines() or ["unknown error"])[-1][:200]
        except subprocess.TimeoutExpired:
            ok, why = False, "timed out"
        if ok:
            result["downloaded"] += 1
            state["yt_failed"].pop(vid, None)
            streak = 0
            logging.info("Video %s saved (%s)", vid, ids[vid][0])
        else:
            result["failed_now"] += 1
            state["yt_failed"][vid] = {"when": now().isoformat(), "why": why}
            streak += 1
            logging.warning("Video %s failed: %s", vid, why)
            if streak >= 8:
                logging.warning("8 videos failed in a row (YouTube may be blocking "
                                "this machine); stopping videos for this run.")
                save_state(state)
                break
        save_state(state)
        time.sleep(2)
    result["remaining"] = sum(1 for v in ids if not youtube_file(v).exists()
                              and v not in state["yt_failed"])
    return result


def write_status(state: dict, discovered: Dict[str, Optional[int]],
                 remaining: List[str], stats: dict, started: datetime,
                 fetcher: Fetcher, failed_pages: List[str], check: dict,
                 videos: Optional[dict] = None) -> None:
    total = len(set(discovered) | set(page_titles_on_disk()))
    embeds = ", ".join(f"{h} x{n}" for h, n in sorted(check["embeds"].items(), key=lambda x: -x[1])[:6])
    lines = [
        "# Dustloop GGST mirror status",
        "",
        f"- Run finished: {now().strftime('%Y-%m-%d %H:%M UTC')} "
        f"(took {str(now() - started).split('.')[0]}, {fetcher.requests} requests)",
        f"- Pages fully rendered (new format, every file present): "
        f"**{check['good']} / {total}**",
        f"- Still in the old unstyled format: {len(check['old'])}",
        f"- Pages with missing files: {len(check['broken'])} ({check['missing_files']} files)"
        + (" (re-queued)" if check["broken"] else ""),
        f"- Videos: {check['video_tags']} <video> tags; embeds: {embeds or 'none'}",
        *([f"- Combo videos (YouTube, Combos pages): "
           f"**{sum(1 for v in combos_youtube_ids() if youtube_file(v).exists())} / {videos['total']}** "
           f"downloaded; this run +{videos['downloaded']}, {videos['failed_now']} failed"
           + (f" ({videos['skipped']})" if videos.get('skipped') else "")]
          if videos else []),
        f"- This run: {stats['pages']} pages, {stats['assets']} images/fonts, "
        f"{stats['css']} stylesheets, {stats['failed']} failed requests",
        f"- Still to do: {len(remaining)} pages"
        + (" (next run continues from here)" if remaining else " — mirror is complete"),
    ]
    if failed_pages:
        lines += ["", f"Pages that failed ({len(failed_pages)}):", ""] + [f"- {t}" for t in failed_pages[:15]]
    yt_fail = state.get("yt_failed", {})
    if videos and yt_fail:
        reasons: Dict[str, int] = {}
        for f in yt_fail.values():
            reasons[f["why"]] = reasons.get(f["why"], 0) + 1
        lines += ["", f"Videos that failed ({len(yt_fail)}), by reason:", ""] + [
            f"- {n}x {why}" for why, n in sorted(reasons.items(), key=lambda x: -x[1])[:5]]
    for sample in check.get("samples", []):
        lines += ["", "Markup around a missing file: " + sample.replace("%", "%25")[:900]]
    if check["broken"]:
        lines += ["", "Pages with missing files:", ""] + [
            f"- {t}: {', '.join(m[:3])}" for t, m in list(check["broken"].items())[:10]]
    if remaining:
        lines += ["", "Next up:", ""] + [f"- {t}" for t in remaining[:15]]
    text = "\n".join(lines) + "\n"
    STATUS_FILE.write_text(text, encoding="utf-8")
    if os.environ.get("GITHUB_ACTIONS"):
        # Shows up as a public annotation on the run page.
        notice = "%0A".join(l for l in lines if l and not l.startswith("#"))
        print(f"::notice title=Mirror status::{notice}")
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as fh:
            fh.write(f"remaining={len(remaining)}\nfetched={stats['pages']}\n")
            if videos:
                fh.write(f"videos_remaining={videos['remaining']}\n"
                         f"videos_downloaded={videos['downloaded']}\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(text)
    print(text)


def prune_other_games() -> None:
    """The old wget crawl wandered into every game on Dustloop. Remove those
    pages so the download only carries GGST."""
    import shutil
    w = SITE_DIR / "w"
    if not w.is_dir():
        return
    removed = 0
    for entry in w.iterdir():
        name = entry.name
        if name == "GGST" or name.startswith("Guilty_Gear_-Strive-"):
            continue
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink(missing_ok=True)
        removed += 1
    if removed:
        logging.info("Removed %d non-GGST entries left by the old crawler", removed)


def create_index() -> None:
    target = "site/w/Guilty_Gear_-Strive-.html"
    (OUTPUT_DIR / "index.html").write_text(
        f'<!DOCTYPE html><meta charset="utf-8"><title>Dustloop Mirror</title>'
        f'<meta http-equiv="refresh" content="0; url={target}">'
        f'<p><a href="{target}">Guilty Gear -Strive- wiki</a></p>\n',
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Mirror the Dustloop GGST wiki.")
    parser.add_argument("--budget-minutes", type=float, default=None,
                        help="Stop starting new pages after this many minutes.")
    parser.add_argument("--only", nargs="+", metavar="TITLE",
                        help="Only fetch these page titles.")
    parser.add_argument("--no-videos", action="store_true",
                        help="Skip downloading YouTube combo videos.")
    parser.add_argument("--refetch-all", action="store_true",
                        help="Re-download every page even if unchanged.")
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    SITE_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"),
                  logging.StreamHandler(sys.stdout)],
    )
    logging.info("=" * 60)
    logging.info("Dustloop mirror run starting -> %s", OUTPUT_DIR)

    prune_other_games()
    started = now()
    deadline = started + timedelta(minutes=args.budget_minutes) if args.budget_minutes else None
    state = load_state()
    fetcher = Fetcher(WAIT_SECONDS)
    mirror = Mirror(fetcher, state)

    if args.only:
        discovered = {}
        queue = list(args.only)
    else:
        discovered = discover_pages(fetcher)
        before = audit(state)
        broken = set(before["broken"])
        queue = plan(state, discovered, args.refetch_all, broken)
    logging.info("%d pages to fetch this run", len(queue))

    seen = set(queue)
    i = 0
    while i < len(queue):
        if deadline and now() > deadline:
            logging.info("Time budget reached; stopping here. Progress is saved.")
            break
        title = queue[i]
        i += 1
        ok, linked = mirror.fetch_page(title)
        if ok:
            state["pages"][title] = {"revid": discovered.get(title),
                                     "fetched": now().isoformat()}
            save_state(state)
        if not args.only:
            # Pick up GGST pages the API didn't list (or all of them, if the
            # API was unreachable).
            for t in linked:
                if t not in seen and not title_to_path(t).exists():
                    seen.add(t)
                    queue.append(t)

    save_state(state)
    videos = None
    if not args.only and not args.no_videos:
        if i < len(queue):
            # Pages come first; videos wait until every page is done.
            ids = combos_youtube_ids()
            videos = {"total": len(ids), "downloaded": 0, "failed_now": 0,
                      "remaining": 0, "skipped": "waiting for pages to finish"}
        else:
            videos = download_youtube(state, deadline)
    save_state(state)
    create_index()
    write_status(state, discovered, queue[i:], mirror.stats, started, fetcher,
                 mirror.failed_pages, audit(state), videos)
    return 0


if __name__ == "__main__":
    sys.exit(main())
