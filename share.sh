#!/bin/bash
# Package this monitor for sharing: builds netmon-share.zip containing the
# code and setup scripts plus EXAMPLE config files — none of your personal
# data (your database, logs, router list, device names, or dashboard).
#
# Usage:  bash share.sh
# Then send netmon-share.zip to your friend. They unzip it anywhere,
# rename/edit the three .example.json files, and run the setup for their
# OS: `bash setup.sh` on macOS/Linux, or double-click setup-windows.bat
# on Windows.

set -e
NETMON_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STAGE="$(mktemp -d)/network-monitor"
mkdir -p "$STAGE"

for f in monitor.py dashboard.py scan_routers.py setup.sh uninstall.sh share.sh \
         setup.ps1 uninstall.ps1 setup-windows.bat uninstall-windows.bat \
         netmon.monitor.plist netmon.dashboard.plist README.md; do
  if [ -f "$NETMON_DIR/$f" ]; then
    cp "$NETMON_DIR/$f" "$STAGE/"
  else
    echo "warning: $f not found, skipping"
  fi
done

cat > "$STAGE/routers.example.json" << 'EOF'
[
  { "name": "Living Room AP", "ip": "192.168.1.5", "floor": "Ground Floor" },
  { "name": "Bedroom Mesh Node", "ip": "192.168.1.6", "floor": "First Floor" }
]
EOF

cat > "$STAGE/devices.example.json" << 'EOF'
{
  "aa:bb:cc:dd:ee:ff": "My iPhone"
}
EOF

cat > "$STAGE/config.example.json" << 'EOF'
{
  "title": "Home Network Monitor",
  "floors": ["First Floor", "Ground Floor"],
  "underground_floors": [],
  "main_router_floor": "Ground Floor"
}
EOF

cat > "$STAGE/START-HERE.txt" << 'EOF'
Quick start
===========
1. Rename the three example files (remove ".example"):
     routers.example.json -> routers.json   (your routers/APs, IPs, floors)
     devices.example.json -> devices.json   (friendly names, fill in over time)
     config.example.json  -> config.json    (your house: title, floors
                                             top-to-bottom, which floors are
                                             underground, where the main
                                             router is drawn)
   Edit them for your home. All three are optional — the monitor runs
   without them, you just get fewer labels on the dashboard.

2. Run the setup for your operating system:

   Mac:      open Terminal, cd into this folder, run:  bash setup.sh

   Windows:  install Python 3 first if you don't have it
             (https://www.python.org/downloads/ — tick "Add python.exe
             to PATH"), then double-click:  setup-windows.bat

   Linux:    the monitor code runs fine, but setup.sh is macOS-only
             (launchd) — see the README's Raspberry Pi/Linux section
             for a systemd/cron setup.

3. Wait a few minutes, then open dashboard.html in your browser.

Full documentation is in README.md (including a Windows section).
EOF

OUT="$NETMON_DIR/netmon-share.zip"
rm -f "$OUT"
(cd "$(dirname "$STAGE")" && zip -rq "$OUT" "$(basename "$STAGE")")
rm -rf "$(dirname "$STAGE")"
echo "Created $OUT"
echo "Send this file to your friend — it contains no personal data."
