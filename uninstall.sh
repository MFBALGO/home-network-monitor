#!/bin/bash
# Stops and removes the launchd background services. Your collected data
# (data/network_monitor.db) and dashboard.html are left untouched.
# Matches any username prefix, so it also cleans up services installed by
# older versions of setup.sh.

LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"

FOUND=false
for plist in "$LAUNCH_AGENTS_DIR"/*.netmon.monitor.plist "$LAUNCH_AGENTS_DIR"/*.netmon.dashboard.plist; do
  [ -f "$plist" ] || continue
  FOUND=true
  launchctl unload "$plist" 2>/dev/null || true
  rm "$plist"
  echo "Removed $plist"
done

if [ "$FOUND" = false ]; then
  echo "No netmon services were installed."
fi
echo "Stopped. Your data and dashboard.html are still in this folder."
