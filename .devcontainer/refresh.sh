#!/bin/bash
# Pull the newest mirror from GitHub Actions into this codespace.
# Runs automatically on codespace start (once per UTC day).
#
#   bash .devcontainer/refresh.sh          # skip if already refreshed today
#   bash .devcontainer/refresh.sh --force  # always pull the newest run

set -e

MARKER="$HOME/.dustloop_last_refresh"
TODAY=$(date -u +%Y-%m-%d)

# Work from the repo root no matter where this was started from.
cd "$(dirname "$0")/.."

if [ "$1" != "--force" ] && [ -f "$MARKER" ] && [ "$(cat "$MARKER")" = "$TODAY" ]; then
    echo "[refresh] Mirror already updated today ($TODAY). Use --force to pull again."
    exit 0
fi

RUN_ID=$(gh run list --workflow mirror.yml --status success --limit 1 \
    --json databaseId --jq '.[0].databaseId' || true)
if [ -z "$RUN_ID" ]; then
    echo "[refresh] No finished mirror run found yet. Try again later."
    exit 0
fi

echo "[refresh] Downloading mirror from run $RUN_ID..."
rm -f dustloop_mirror.tar.gz
if gh run download "$RUN_ID" --name dustloop-mirror; then
    tar -xzf dustloop_mirror.tar.gz
    rm -f dustloop_mirror.tar.gz
    echo "$TODAY" > "$MARKER"
    [ -f dustloop_mirror/status.md ] && cat dustloop_mirror/status.md
    echo "[refresh] Done. Run: cd dustloop_mirror && python3 server.py"
else
    echo "[refresh] Could not download the artifact from run $RUN_ID (see error above)."
fi
