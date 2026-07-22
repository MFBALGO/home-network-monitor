#!/bin/bash
# Home Network Monitor - updater for macOS/Linux.
# Downloads the latest release from GitHub, keeps a backup of the old
# version in data/backup, and the services restart themselves.
cd "$(dirname "${BASH_SOURCE[0]}")"
PY="$(command -v python3 || command -v python)"
if [ -z "$PY" ]; then
  echo "ERROR: python not found"
  exit 1
fi
"$PY" update.py "$@"
