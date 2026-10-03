#!/usr/bin/env python3
"""
Supercombo Wiki Mirror — Avatar Legends
========================================
Downloads the Avatar Legends section of wiki.supercombo.gg for offline access.
Designed to be run daily; only re-downloads pages that have changed since last run.

Pages whose title contains "Zaheer" are seeded first, so an interrupted or
time-budgeted run still captures the character currently being reviewed.

Usage:
    python supercombo_mirror.py            # run a mirror cycle
    python supercombo_mirror.py --dry-run  # show what wget would do, but don't run it
    python supercombo_mirror.py --help     # show help

Output:
    ~/supercombo_mirror/site/...   the mirrored site
    ~/supercombo_mirror/index.html a redirect into the wiki's Avatar Legends page
    ~/supercombo_mirror/mirror.log run history
"""

import argparse
import html
import json
import logging
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import List, Optional

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL = "https://wiki.supercombo.gg/w/Avatar_Legends"
DOMAIN = "wiki.supercombo.gg"
API_URL = f"https://{DOMAIN}/w/api.php"

# Title prefix used to find every Avatar Legends page via the wiki's API, so
# pages that aren't well-linked from other pages still get discovered.
# Derived from BASE_URL; adjust if Supercombo's page-naming convention changes.
PAGE_TITLE_PREFIX = BASE_URL.rsplit("/w/", 1)[-1]

# Substring (case-insensitive) used to prioritize the character currently
# being reviewed. Seeded first so a partial/interrupted run still grabs it.
PRIORITY_SUBSTRING = "zaheer"

OUTPUT_DIR = Path.home() / "supercombo_mirror"
SITE_DIR = OUTPUT_DIR / "site"
LOG_FILE = OUTPUT_DIR / "mirror.log"
SEED_FILE = OUTPUT_DIR / "seed_urls.txt"

# Politeness settings. Don't lower these — Supercombo is a community-run wiki.
WAIT_BETWEEN_REQUESTS = 1   # seconds (with --random-wait this becomes 0.5–1.5s)
USER_AGENT = "SupercomboOfflineMirror/1.0 (personal offline reader)"

# Directories on the wiki to descend into.
INCLUDE_DIRS = "/w,/wiki,/images,/load.php,/skins,/extensions,/resources"

# URL patterns to reject — useless offline (edit pages, histories, etc.).
REJECT_REGEX = (
    r"action=edit|"
    r"action=history|"
    r"action=delete|"
    r"action=raw|"
    r"oldid=|"
    r"diff=|"
    r"printable=|"
    r"redlink=|"
    r"Special:|"
    r"User:|"
    r"User_talk:|"
    r"Talk:"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def setup_logging() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def check_wget() -> bool:
    """Verify wget is installed and reachable."""
    if shutil.which("wget") is None:
        return False
    try:
        subprocess.run(
            ["wget", "--version"],
            capture_output=True, check=True, timeout=5,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


def fetch_all_page_urls() -> List[str]:
    """
    Ask the wiki's API for every page whose title starts with the Avatar
    Legends prefix. wget's own recursive crawl only finds pages it can reach
    by following links from pages it has already visited, so anything not
    well-linked (an orphan subpage, a page added since the last run) never
    gets discovered no matter how many times the job reruns. Enumerating
    pages via the API guarantees full, growing coverage instead.

    Pages matching PRIORITY_SUBSTRING (currently "zaheer") are sorted first,
    so a time-budgeted or interrupted run still captures them.

    Returns an empty list (never raises) if the API is unreachable — the
    caller falls back to wget's plain recursive crawl in that case.
    """
    titles = set()
    params = {
        "action": "query",
        "list": "allpages",
        "apnamespace": "0",
        "apprefix": PAGE_TITLE_PREFIX,
        "aplimit": "500",
        "format": "json",
    }
    apcontinue = None
    while True:
        if apcontinue:
            params["apcontinue"] = apcontinue
        url = f"{API_URL}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.load(resp)
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            logging.warning(
                "Page-list API call failed (%s); falling back to link crawl only.",
                exc,
            )
            return []

        for page in data.get("query", {}).get("allpages", []):
            titles.add(page["title"])

        apcontinue = data.get("continue", {}).get("apcontinue")
        if not apcontinue:
            break

    safe_chars = "/:,()&'"
    sorted_titles = sorted(
        titles,
        key=lambda t: (0 if PRIORITY_SUBSTRING in t.lower() else 1, t),
    )
    urls = [
        f"https://{DOMAIN}/w/{urllib.parse.quote(t.replace(' ', '_'), safe=safe_chars)}"
        for t in sorted_titles
    ]
    priority_count = sum(1 for t in sorted_titles if PRIORITY_SUBSTRING in t.lower())
    logging.info(
        "Discovered %d pages via wiki API under title prefix %r (%d prioritized as %r)",
        len(urls), PAGE_TITLE_PREFIX, priority_count, PRIORITY_SUBSTRING,
    )
    return urls


def write_seed_file(urls: List[str]) -> Optional[Path]:
    """Write discovered page URLs to a file wget can consume with --input-file."""
    if not urls:
        return None
    SEED_FILE.write_text("\n".join(urls) + "\n", encoding="utf-8")
    return SEED_FILE


def build_wget_command(seed_file: Optional[Path] = None) -> list:
    """
    Construct the wget invocation.

    Scope is controlled by --domains and --include-directories rather than
    --no-parent, which is over-restrictive for MediaWiki's URL layout.
    """
    cmd = [
        "wget",
        "--mirror",                              # recursive + timestamping + infinite depth
        "--convert-links",                       # rewrite links to work offline
        "--adjust-extension",                    # add .html to HTML files
        "--page-requisites",                     # CSS, JS, images needed to render each page
        "--continue",                            # resume partial downloads
        "--timestamping",                        # skip files that haven't changed
        "--no-host-directories",                 # cleaner folder layout
        f"--directory-prefix={SITE_DIR}",
        f"--wait={WAIT_BETWEEN_REQUESTS}",
        "--random-wait",                         # jitter the wait time
        f"--user-agent={USER_AGENT}",
        f"--domains={DOMAIN}",
        f"--include-directories={INCLUDE_DIRS}",
        f"--reject-regex={REJECT_REGEX}",
        "--tries=3",                             # retry transient failures
        "--timeout=30",
        "--no-verbose",                          # one log line per file, not five
    ]
    if seed_file:
        # Seed the full known page list (Zaheer pages first) *in addition to*
        # the recursive crawl from BASE_URL below, so every page gets visited
        # regardless of whether anything currently links to it, and the
        # priority pages are queued before the rest.
        cmd.append(f"--input-file={seed_file}")
    cmd.append(BASE_URL)
    return cmd


def mirror_site(dry_run: bool = False) -> bool:
    SITE_DIR.mkdir(parents=True, exist_ok=True)

    seed_file = None
    if not dry_run:
        seed_file = write_seed_file(fetch_all_page_urls())

    cmd = build_wget_command(seed_file)
    logging.info("Command: %s", " ".join(cmd))

    if dry_run:
        logging.info("--dry-run set; not executing wget.")
        return True

    started = datetime.now()
    try:
        result = subprocess.run(cmd)
    except KeyboardInterrupt:
        logging.warning("Interrupted by user.")
        return False
    except Exception as exc:
        logging.error("wget failed to launch: %s", exc)
        return False

    duration = datetime.now() - started
    # wget exit codes: 0 = success, 4 = network failure, 8 = some URLs returned
    # error (very common for wikis with broken redlinks). Treat 0 and 8 as OK.
    if result.returncode in (0, 8):
        logging.info("Mirror finished in %s (exit %s)",
                     duration, result.returncode)
        return True

    logging.error("wget exited with code %s after %s",
                  result.returncode, duration)
    return False


def create_index() -> None:
    """Drop a redirect HTML at the root so the user has one obvious entry point."""
    index_path = OUTPUT_DIR / "index.html"

    main_dir = SITE_DIR / "w"
    target = None
    zaheer_target = None
    if main_dir.exists():
        for candidate in main_dir.glob("Avatar_Legends*"):
            if not (candidate.is_file() and candidate.suffix in ("", ".html")):
                continue
            if target is None and candidate.name.rstrip(".html") == "Avatar_Legends":
                target = candidate.relative_to(OUTPUT_DIR).as_posix()
            if zaheer_target is None and "zaheer" in candidate.name.lower():
                zaheer_target = candidate.relative_to(OUTPUT_DIR).as_posix()

    if target is None:
        target = "site/w/Avatar_Legends.html"

    safe_target = html.escape(target, quote=True)
    zaheer_link = ""
    if zaheer_target:
        safe_zaheer = html.escape(zaheer_target, quote=True)
        zaheer_link = f'<p>Jump straight to <a href="{safe_zaheer}">Zaheer</a>.</p>\n  '

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Supercombo Avatar Legends Mirror</title>
  <meta http-equiv="refresh" content="0; url={safe_target}">
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 40rem; margin: 4rem auto; padding: 0 1rem; }}
  </style>
</head>
<body>
  <h1>Supercombo Avatar Legends Offline Mirror</h1>
  <p>Redirecting to the <a href="{safe_target}">Avatar Legends wiki</a>...</p>
  {zaheer_link}<p>If the redirect doesn't work, click the link above.</p>
  <hr>
  <p><small>Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</small></p>
</body>
</html>
"""
    index_path.write_text(page, encoding="utf-8")
    logging.info("Wrote entry point: %s", index_path)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Mirror the Supercombo Avatar Legends wiki section.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the wget command but don't execute it.")
    args = parser.parse_args()

    setup_logging()
    logging.info("=" * 60)
    logging.info("Supercombo mirror run starting")
    logging.info("Output directory: %s", OUTPUT_DIR)

    if not check_wget():
        logging.error("wget is not installed or not on PATH.")
        print(
            "\nInstall wget first:\n"
            "  macOS:   brew install wget\n"
            "  Linux:   sudo apt install wget   (Debian/Ubuntu)\n"
            "           sudo dnf install wget   (Fedora)\n"
            "  Windows: choco install wget      (or use WSL / Git Bash)\n",
            file=sys.stderr,
        )
        return 1

    ok = mirror_site(dry_run=args.dry_run)
    if not ok:
        print(f"\nMirror failed. See log: {LOG_FILE}", file=sys.stderr)
        return 1

    if not args.dry_run:
        create_index()
        print(f"\nDone. Open this file in your browser:\n  {OUTPUT_DIR / 'index.html'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
