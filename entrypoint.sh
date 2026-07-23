#!/bin/bash
# network-monitor container entrypoint.
#
# Seed-on-start: copy the image's code (/opt/netmon-dist) over the state
# volume (/app) on EVERY boot - the container always runs the image's code,
# and any in-container self-update is deliberately reverted on restart
# (update via image rebuild instead; update.py's restart watcher baselines
# the status file's mtime at start, so a persisted state=done file can't
# bounce a fresh container). User state (config.json, routers.json,
# devices.json, data/, logs/, dashboard.html, report.html) is never touched;
# stale data/commands.json is inert too (monitor.py baselines it + 120s TTL).
#
# Then supervise: monitor.py + serve.py + a 60s dashboard.py render loop.
# If monitor.py or serve.py dies (including update.py's restart-watcher
# calling os._exit(1)), exit 1 so `restart: unless-stopped` relaunches us.
# A failed dashboard render only logs - serve.py keeps serving the last
# good dashboard.html, so one bad render costs 60s of staleness, not uptime.
set -u

DIST=/opt/netmon-dist
APP=/app

mkdir -p "$APP/data" "$APP/logs" "$APP/vendor"

cp -f "$DIST"/*.py "$APP"/
cp -f "$DIST"/*.example.json "$APP"/ 2>/dev/null || true
cp -f "$DIST"/vendor/* "$APP/vendor/"

cd "$APP"

shutdown() {
    echo "[entrypoint] caught stop signal - terminating services" >&2
    kill $(jobs -p) 2>/dev/null
    wait
    exit 0
}
trap shutdown TERM INT

python monitor.py &
python serve.py &
(
    while true; do
        python dashboard.py \
            || echo "[entrypoint] dashboard.py failed - next attempt in 60s" >&2
        sleep 60
    done
) &

wait -n
echo "[entrypoint] a core service exited - exiting so Docker restarts the container" >&2
exit 1
