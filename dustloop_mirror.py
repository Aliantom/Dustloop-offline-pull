#!/usr/bin/env python3
"""
Dustloop Wiki Mirror
====================
Downloads the Guilty Gear -Strive- section of dustloop.com for offline access.
Designed to be run daily; only re-downloads pages that have changed since last run.

Usage:
    python dustloop_mirror.py            # run a mirror cycle
    python dustloop_mirror.py --dry-run  # show what wget would do, but don't run it
    python dustloop_mirror.py --help     # show help

Output:
    ~/dustloop_mirror/site/...   the mirrored site
    ~/dustloop_mirror/index.html a redirect into the wiki's main page
    ~/dustloop_mirror/mirror.log run history
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

BASE_URL = "https://www.dustloop.com/w/Guilty_Gear_-Strive-"
DOMAIN = "www.dustloop.com"
API_URL = f"https://{DOMAIN}/wiki/api.php"

# Title prefix used to find every GGST page via the wiki's API, so pages
# that aren't well-linked from other pages still get discovered. Character
# and system subpages live under "GGST/..." (e.g. "GGST/Sol_Badguy"), which
# is a different title than the BASE_URL landing page ("Guilty_Gear_-Strive-"),
# so this is intentionally not derived from BASE_URL. Confirmed against a
# live crawl log — adjust if Dustloop's naming convention changes again.
PAGE_TITLE_PREFIX = "GGST"

OUTPUT_DIR = Path.home() / "dustloop_mirror"
SITE_DIR = OUTPUT_DIR / "site"
LOG_FILE = OUTPUT_DIR / "mirror.log"
SEED_FILE = OUTPUT_DIR / "seed_urls.txt"

# Politeness settings. Don't lower these — Dustloop is a community-run wiki.
WAIT_BETWEEN_REQUESTS = 1   # seconds (with --random-wait this becomes 0.5–1.5s)
USER_AGENT = "DustloopOfflineMirror/1.0 (personal offline reader)"

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
    Ask the wiki's API for every page whose title starts with the GGST
    prefix. wget's own recursive crawl only finds pages it can reach by
    following links from pages it has already visited, so anything not
    well-linked (an orphan subpage, a page added since the last run) never
    gets discovered no matter how many times the job reruns. Enumerating
    pages via the API guarantees full, growing coverage instead.

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
    urls = [
        f"https://{DOMAIN}/w/{urllib.parse.quote(t.replace(' ', '_'), safe=safe_chars)}"
        for t in sorted(titles)
    ]
    logging.info(
        "Discovered %d pages via wiki API under title prefix %r",
        len(urls), PAGE_TITLE_PREFIX,
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

    NOTE: --no-parent was removed in this version. It was over-restrictive for
    MediaWiki's URL layout and may have been blocking pages we wanted.
    Scope is now controlled exclusively by --domains and --include-directories.
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
        # Seed the full known page list *in addition to* the recursive crawl
        # from BASE_URL below, so every page gets visited regardless of
        # whether anything currently links to it.
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
    if main_dir.exists():
        for candidate in main_dir.glob("Guilty_Gear_-Strive-*"):
            if candidate.is_file() and candidate.suffix in ("", ".html"):
                target = candidate.relative_to(OUTPUT_DIR).as_posix()
                break

    if target is None:
        target = "site/w/Guilty_Gear_-Strive-.html"

    safe_target = html.escape(target, quote=True)
    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Dustloop Mirror</title>
  <meta http-equiv="refresh" content="0; url={safe_target}">
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 40rem; margin: 4rem auto; padding: 0 1rem; }}
  </style>
</head>
<body>
  <h1>Dustloop Offline Mirror</h1>
  <p>Redirecting to the <a href="{safe_target}">Guilty Gear -Strive- wiki</a>...</p>
  <p>If the redirect doesn't work, click the link above.</p>
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
    parser = argparse.ArgumentParser(description="Mirror the Dustloop GGST wiki.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the wget command but don't execute it.")
    args = parser.parse_args()

    setup_logging()
    logging.info("=" * 60)
    logging.info("Dustloop mirror run starting")
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
