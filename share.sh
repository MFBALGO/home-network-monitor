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

for f in monitor.py dashboard.py serve.py scan_routers.py version.py diagnose.py \
         settings_api.py settings_page.py update.py \
         setup.sh uninstall.sh share.sh update.sh update-windows.bat \
         setup.ps1 uninstall.ps1 setup-windows.bat uninstall-windows.bat \
         netmon.monitor.plist netmon.dashboard.plist netmon.web.plist README.md \
         routers.example.json devices.example.json config.example.json; do
  if [ -f "$NETMON_DIR/$f" ]; then
    cp "$NETMON_DIR/$f" "$STAGE/"
  else
    echo "warning: $f not found, skipping"
  fi
done

# Ship the vendored chart library when present, so the dashboard renders
# charts immediately (and offline) without the setup script needing a CDN.
if [ -d "$NETMON_DIR/vendor" ]; then
  mkdir -p "$STAGE/vendor"
  cp "$NETMON_DIR"/vendor/*.js "$STAGE/vendor/" 2>/dev/null || true
fi

cat > "$STAGE/START-HERE.txt" << 'EOF'
Quick start
===========
1. Run the setup for your operating system:

   Mac:      open Terminal, cd into this folder, run:  bash setup.sh

   Windows:  install Python 3 first if you don't have it
             (https://www.python.org/downloads/ — tick "Add python.exe
             to PATH"), then double-click:  setup-windows.bat

   Linux:    the monitor code runs fine, but setup.sh is macOS-only
             (launchd) — see the README's Raspberry Pi/Linux section
             for a systemd/cron setup.

2. On the same computer, open  http://localhost:8080/  in a browser.
   A setup wizard appears the first time: it scans your network, finds
   your routers/access points, and writes the config for you.

   (Prefer editing files by hand? Rename the three .example.json files
   — remove ".example" — and edit them; the format is in README.md.
   All three are optional.)

3. Wait a few minutes for data to accumulate. Every device on your
   Wi-Fi can open the dashboard at  http://<that-computer's-ip>:8080/
   — the setup prints the exact address.

Full documentation is in README.md (including a Windows section).
EOF

OUT="$NETMON_DIR/netmon-share.zip"
rm -f "$OUT"
if command -v zip >/dev/null 2>&1; then
  (cd "$(dirname "$STAGE")" && zip -rq "$OUT" "$(basename "$STAGE")")
else
  # Git Bash on Windows usually has no zip binary — use Python's instead.
  PY="$(command -v python3 || command -v python)"
  if [ -z "$PY" ]; then
    echo "ERROR: neither 'zip' nor python found — can't build the archive."
    exit 1
  fi
  "$PY" -c "import shutil, sys; shutil.make_archive(sys.argv[1][:-4], 'zip', sys.argv[2], sys.argv[3])" \
    "$OUT" "$(dirname "$STAGE")" "$(basename "$STAGE")"
fi
rm -rf "$(dirname "$STAGE")"
echo "Created $OUT"
echo "Send this file to your friend — it contains no personal data."
