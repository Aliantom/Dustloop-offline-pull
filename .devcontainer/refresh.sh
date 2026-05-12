#!/bin/bash
# Auto-refresh the dustloop mirror once per UTC day on codespace startup.

set -e

MARKER="$HOME/.dustloop_last_refresh"
TODAY=$(date -u +%Y-%m-%d)

# Skip if we've already refreshed today
if [ -f "$MARKER" ] && [ "$(cat "$MARKER")" = "$TODAY" ]; then
    echo "[refresh] Mirror already updated today ($TODAY). Skipping."
    exit 0
fi

echo "[refresh] Pulling fresh mirror for $TODAY..."
cd "$(dirname "$0")/.."
rm -f dustloop_mirror.tar.gz

if gh run download --name dustloop-mirror 2>/dev/null; then
    tar -xzf dustloop_mirror.tar.gz
    echo "$TODAY" > "$MARKER"
    echo "[refresh] Done. Run: cd dustloop_mirror && python3 server.py"
else
    echo "[refresh] Could not fetch artifact (workflow may not have run yet)."
    echo "[refresh] Will retry on next codespace start."
fi