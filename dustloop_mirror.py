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
import logging
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# The wiki entry point. wget will follow links from here.
BASE_URL = "https://www.dustloop.com/w/Guilty_Gear_-Strive-"
DOMAIN = "www.dustloop.com"

# Where everything goes. Change this if you want a different location.
OUTPUT_DIR = Path.home() / "dustloop_mirror"
SITE_DIR = OUTPUT_DIR / "site"
LOG_FILE = OUTPUT_DIR / "mirror.log"

# Politeness settings. Don't lower these without good reason — Dustloop is a
# community-run wiki and aggressive scraping puts real strain on their servers.
WAIT_BETWEEN_REQUESTS = 1   # seconds (with --random-wait this becomes 0.5–1.5s)
USER_AGENT = "DustloopOfflineMirror/1.0 (personal offline reader)"

# Directories on the wiki that we DO want to descend into. Anything outside
# this list (forum, edit pages, user pages, etc.) is skipped.
INCLUDE_DIRS = "/w,/wiki,/images,/load.php,/skins,/extensions,/resources"

# URL patterns to reject — these are pages that are useless offline (edit
# forms, page histories, diffs, special print views, etc.) and would just
# bloat the mirror.
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


def build_wget_command() -> list[str]:
    """Construct the wget invocation."""
    return [
        "wget",
        "--mirror",                              # recursive + timestamping + infinite depth
        "--convert-links",                       # rewrite links to work offline
        "--adjust-extension",                    # add .html to HTML files
        "--page-requisites",                     # CSS, JS, images needed to render each page
        "--no-parent",                           # don't ascend above the start path
        "--continue",                            # resume partial downloads
        "--timestamping",                        # skip files that haven't changed
        "--no-host-directories",                 # cleaner folder layout
        f"--directory-prefix={SITE_DIR}",
        f"--wait={WAIT_BETWEEN_REQUESTS}",
        "--random-wait",                         # jitter the wait time (politeness)
        f"--user-agent={USER_AGENT}",
        f"--domains={DOMAIN}",
        f"--include-directories={INCLUDE_DIRS}",
        f"--reject-regex={REJECT_REGEX}",
        "--tries=3",                             # retry transient failures
        "--timeout=30",
        # wget's own log goes alongside ours. -nv = "not verbose" (one line per file).
        "--no-verbose",
        BASE_URL,
    ]


def mirror_site(dry_run: bool = False) -> bool:
    cmd = build_wget_command()
    logging.info("Command: %s", " ".join(cmd))

    if dry_run:
        logging.info("--dry-run set; not executing wget.")
        return True

    SITE_DIR.mkdir(parents=True, exist_ok=True)
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
    # wget exit codes: 0 = success, 4 = network failure, 8 = server gave error
    # response on at least one URL (very common with wikis — broken redlinks etc.).
    # Treat 0 and 8 as success; the mirror is still usable.
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

    # Find the actual landing page. wget's filename depends on URL encoding,
    # so search rather than guess.
    main_dir = SITE_DIR / "w"
    target = None
    if main_dir.exists():
        for candidate in main_dir.glob("Guilty_Gear_-Strive-*"):
            if candidate.is_file() and candidate.suffix in ("", ".html"):
                target = candidate.relative_to(OUTPUT_DIR).as_posix()
                break

    if target is None:
        target = "site/w/Guilty_Gear_-Strive-.html"  # best guess

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Dustloop Mirror</title>
  <meta http-equiv="refresh" content="0; url={target}">
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 40rem; margin: 4rem auto; padding: 0 1rem; }}
    code {{ background: #eee; padding: 0.1em 0.3em; border-radius: 3px; }}
  </style>
</head>
<body>
  <h1>Dustloop Offline Mirror</h1>
  <p>Redirecting to the <a href="{target}">Guilty Gear -Strive- wiki</a>...</p>
  <p>If the redirect doesn't work, click the link above.</p>
  <hr>
  <p><small>Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</small></p>
</body>
</html>
"""
    index_path.write_text(html, encoding="utf-8")
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
