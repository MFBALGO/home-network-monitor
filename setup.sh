#!/bin/bash
# One-time setup: registers the monitor and dashboard-generator as macOS
# background services (launchd), so they keep running even after you log
# out and restart automatically at login / on crash.
#
# Usage: cd into this folder in Terminal, then run:  bash setup.sh

set -e
NETMON_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"

echo "Network monitor folder: $NETMON_DIR"
mkdir -p "$NETMON_DIR/data" "$NETMON_DIR/logs" "$NETMON_DIR/vendor" "$LAUNCH_AGENTS_DIR"

# Download the charting library used by dashboard.html, once, so the
# dashboard renders its charts from a local copy instead of a CDN. This
# matters specifically because the one time you most want to open this
# dashboard (your internet is down) is exactly when a CDN-hosted script
# would fail to load and silently blank out every chart on the page.
# Tries each mirror in turn — some networks/DNS filters block one CDN
# (cdnjs.cloudflare.com is a common one to get caught by ad/content
# blocklists) while leaving others reachable.
download_vendor_lib() {
  local dest="$1"; shift
  if [ -s "$dest" ] && [ "$(wc -c < "$dest")" -gt 10000 ]; then
    return 0  # already downloaded
  fi
  local url
  for url in "$@"; do
    echo "Downloading $(basename "$dest") from $(echo "$url" | awk -F/ '{print $3}')..."
    if curl -fsSL --connect-timeout 8 --max-time 30 -o "$dest" "$url" \
       && [ "$(wc -c < "$dest" 2>/dev/null || echo 0)" -gt 10000 ]; then
      echo "  OK ($(wc -c < "$dest") bytes)"
      return 0
    fi
    rm -f "$dest"
  done
  echo "  WARNING: all sources failed for $(basename "$dest") — charts may not render."
  echo "  You can retry later by re-running: bash setup.sh"
  return 1
}
download_vendor_lib "$NETMON_DIR/vendor/chart.umd.min.js" \
  "https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js" \
  "https://unpkg.com/chart.js@4.4.4/dist/chart.umd.min.js" \
  "https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.4/chart.umd.min.js"
download_vendor_lib "$NETMON_DIR/vendor/chartjs-adapter-date-fns.bundle.min.js" \
  "https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js" \
  "https://unpkg.com/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js" \
  "https://cdnjs.cloudflare.com/ajax/libs/chartjs-adapter-date-fns/3.0.0/chartjs-adapter-date-fns.bundle.min.js"

# Optional: install the official Ookla speed test CLI for speed test history.
# (Safe to skip — the dashboard just won't show a speed test chart without it.)
#
# IMPORTANT: the Homebrew formula named "speedtest-cli" is NOT this — it's an
# old, unmaintained community tool with a completely different (and
# incompatible) command-line interface. The official CLI comes from Ookla's
# own tap. If a "speedtest" command already exists but isn't the real one
# (e.g. left over from an earlier version of this script), replace it.
NEED_OOKLA_CLI=true
if command -v speedtest >/dev/null 2>&1; then
  if speedtest --version 2>&1 | grep -qi "ookla"; then
    NEED_OOKLA_CLI=false
  else
    echo "Found a 'speedtest' command that isn't the official Ookla CLI (likely the"
    echo "old community speedtest-cli tool) — replacing it with the real one."
    if command -v brew >/dev/null 2>&1; then
      brew uninstall speedtest-cli >/dev/null 2>&1 || true
    fi
  fi
fi
if [ "$NEED_OOKLA_CLI" = true ]; then
  if command -v brew >/dev/null 2>&1; then
    echo "Installing the official Ookla speedtest CLI via Homebrew (optional, for speed test charts)..."
    brew tap teamookla/speedtest >/dev/null 2>&1 || true
    if brew install speedtest; then
      echo "  OK"
    else
      echo "  (skipped — you can install later, see README)"
    fi
  else
    echo "Homebrew not found — skipping speed test CLI install."
    echo "  Speed test history will stay empty until you install one. See README.md."
  fi
fi

# Optional: nmap for richer, faster device discovery. The monitor works
# fine without it (it falls back to its built-in ping sweep), but when
# nmap is present it probes each host several ways (ICMP + TCP), which
# finds ping-blocking devices more reliably and finishes quicker.
if command -v nmap >/dev/null 2>&1 || [ -x /opt/homebrew/bin/nmap ] || [ -x /usr/local/bin/nmap ]; then
  echo "nmap found — the monitor will use it for device discovery."
elif command -v brew >/dev/null 2>&1; then
  echo "Installing nmap via Homebrew (optional, improves device discovery)..."
  if brew install nmap; then
    echo "  OK"
  else
    echo "  (skipped — the monitor will use its built-in ping sweep)"
  fi
else
  echo "Homebrew not found — skipping nmap (optional). The monitor will use its built-in ping sweep."
fi

# Service labels are derived from the current macOS username, so this same
# folder works for anyone who runs setup.sh — nothing is hardcoded to one
# person's machine.
LABEL_PREFIX="com.$(id -un).netmon"

# Clean up services installed by any previous version/prefix of this setup
# (e.g. an older hardcoded label, or a copy of the folder set up under a
# different username) so two monitors never run at once.
for old in "$LAUNCH_AGENTS_DIR"/*.netmon.monitor.plist "$LAUNCH_AGENTS_DIR"/*.netmon.dashboard.plist "$LAUNCH_AGENTS_DIR"/*.netmon.web.plist; do
  [ -f "$old" ] || continue
  case "$(basename "$old")" in
    "${LABEL_PREFIX}.monitor.plist"|"${LABEL_PREFIX}.dashboard.plist"|"${LABEL_PREFIX}.web.plist") ;; # current — reloaded below
    *)
      echo "Removing old service $(basename "$old")"
      launchctl unload "$old" 2>/dev/null || true
      rm -f "$old"
      ;;
  esac
done

INSTALLED_KINDS=""
for kind in monitor dashboard web; do
  src="$NETMON_DIR/netmon.${kind}.plist"
  # fall back to a legacy template name if the generic one isn't present
  [ -f "$src" ] || src="$(ls "$NETMON_DIR"/*.netmon.${kind}.plist 2>/dev/null | head -1)"
  if [ -z "$src" ] || [ ! -f "$src" ]; then
    if [ "$kind" = web ]; then
      # the LAN web server is optional (mirrors setup.ps1's Test-Path guard)
      echo "netmon.web.plist / serve.py not found — skipping the LAN web server service."
      continue
    fi
    echo "ERROR: no plist template found for '${kind}' — expected netmon.${kind}.plist in this folder."
    exit 1
  fi
  if [ "$kind" = web ] && [ ! -f "$NETMON_DIR/serve.py" ]; then
    echo "serve.py not found — skipping the LAN web server service."
    continue
  fi
  dst="$LAUNCH_AGENTS_DIR/${LABEL_PREFIX}.${kind}.plist"
  sed -e "s|__NETMON_DIR__|$NETMON_DIR|g" \
      -e "s|__LABEL__|${LABEL_PREFIX}.${kind}|g" \
      -e "s|<string>com\.[A-Za-z0-9_-]*\.netmon\.${kind}</string>|<string>${LABEL_PREFIX}.${kind}</string>|g" \
      "$src" > "$dst"
  echo "Installed $dst"
  INSTALLED_KINDS="$INSTALLED_KINDS $kind"
done

echo "Loading launchd services..."
for kind in $INSTALLED_KINDS; do
  launchctl unload "$LAUNCH_AGENTS_DIR/${LABEL_PREFIX}.${kind}.plist" 2>/dev/null || true
  launchctl load "$LAUNCH_AGENTS_DIR/${LABEL_PREFIX}.${kind}.plist"
done

echo ""
echo "Done. The monitor is now running continuously in the background and will"
echo "restart automatically (on crash, and at every login)."
echo ""
echo "Dashboard file: $NETMON_DIR/dashboard.html (regenerates every minute)"
echo "Open it once now (it'll be mostly empty until data accumulates):"
echo "  open \"$NETMON_DIR/dashboard.html\""
case " $INSTALLED_KINDS " in *" web "*)
  LAN_IP="$(ipconfig getifaddr en0 2>/dev/null || true)"
  [ -n "$LAN_IP" ] || LAN_IP="<this-machines-ip>"
  echo ""
  echo "House-wide dashboard: any device on your network can open"
  echo "  http://${LAN_IP}:8080/"
  echo "First time? Configure everything at http://localhost:8080/setup (this machine only)."
  ;;
esac
echo ""
echo "To check the monitor is alive:  launchctl list | grep netmon"
echo "To stop everything:             bash uninstall.sh"
