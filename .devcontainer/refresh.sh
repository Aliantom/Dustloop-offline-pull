#!/bin/bash
# Pull the newest mirror of each game into this codespace.
# Runs automatically on codespace start (once per UTC day).
#
#   bash .devcontainer/refresh.sh            # skip if already refreshed today
#   bash .devcontainer/refresh.sh --force    # always pull the newest
#   MIRROR_SECTIONS="ggst" bash .devcontainer/refresh.sh --force   # just GGST
#
# Games: ggst (Guilty Gear -Strive-), gbvsr (Granblue Fantasy Versus: Rising),
# tokon (Marvel Tokon). A game that hasn't been mirrored yet is skipped.

MARKER="$HOME/.dustloop_last_refresh"
TODAY=$(date -u +%Y-%m-%d)
SECTIONS="${MIRROR_SECTIONS:-ggst gbvsr tokon}"
TAG="mirror-data"

# Work from the repo root no matter where this was started from.
cd "$(dirname "$0")/.." || exit 1

if [ "$1" != "--force" ] && [ -f "$MARKER" ] && [ "$(cat "$MARKER")" = "$TODAY" ]; then
    echo "[refresh] Mirror already updated today ($TODAY). Use --force to pull again."
    exit 0
fi

got_any=false
for s in $SECTIONS; do
    dir=$(mktemp -d)
    if gh release download "$TAG" --pattern "mirror-$s.tar.part*" --dir "$dir" 2>/dev/null \
       && ls "$dir"/mirror-$s.tar.part* >/dev/null 2>&1; then
        echo "[refresh] $s: downloaded $(du -sh "$dir" | cut -f1), unpacking..."
        if cat "$dir"/mirror-$s.tar.part* | tar -x; then
            got_any=true
        else
            echo "[refresh] $s: unpack failed (an upload may have been in progress); try again in a few minutes."
        fi
    else
        echo "[refresh] $s: not mirrored yet, skipping."
    fi
    rm -rf "$dir"
done

if $got_any; then
    echo "$TODAY" > "$MARKER"
    for f in dustloop_mirror/status-*.md; do [ -f "$f" ] && head -n 6 "$f" && echo; done
    echo "[refresh] Done. Run: cd dustloop_mirror && python3 server.py   (status at /_status)"
else
    echo "[refresh] Nothing downloaded. If this is a new setup, the first run may still be going."
fi
