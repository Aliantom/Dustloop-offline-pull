#!/bin/bash
# Start the mirror server on port 8000 (if it isn't already running), then
# pull the newest mirror in the background. Runs on every codespace start and
# attach, so the page comes up right away instead of waiting for the download.
#
#   bash .devcontainer/start.sh        # by hand, if the page shows a 502
#
# Logs: /tmp/dustloop-server.log and /tmp/dustloop-refresh.log

cd "$(dirname "$0")/.." || exit 1
PORT=${PORT:-8000}

if curl -s -o /dev/null --max-time 3 "http://127.0.0.1:$PORT/_status"; then
    echo "[start] Server already running on port $PORT."
else
    echo "[start] Starting server on port $PORT..."
    (cd dustloop_mirror && setsid nohup python3 server.py "$PORT" \
        >> /tmp/dustloop-server.log 2>&1 < /dev/null &)
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        sleep 1
        curl -s -o /dev/null --max-time 3 "http://127.0.0.1:$PORT/_status" && break
    done
    if curl -s -o /dev/null --max-time 3 "http://127.0.0.1:$PORT/_status"; then
        echo "[start] Server is up."
    else
        echo "[start] Server didn't start; last lines of /tmp/dustloop-server.log:"
        tail -n 20 /tmp/dustloop-server.log
    fi
fi

# Pull the newest mirror without blocking the server (once per UTC day unless
# --force is passed through). Skip if a refresh is already running.
if [ "$1" != "--no-refresh" ] && ! pgrep -f "devcontainer/refresh.sh" >/dev/null; then
    setsid nohup bash .devcontainer/refresh.sh "$@" \
        >> /tmp/dustloop-refresh.log 2>&1 < /dev/null &
    echo "[start] Refreshing the mirror in the background (tail -f /tmp/dustloop-refresh.log)."
fi
