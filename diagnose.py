#!/usr/bin/env python3
"""
Home Network Monitor - diagnostics bundle for remote troubleshooting.

When an install misbehaves ("charts are empty", "the task won't start"),
run this and send the resulting zip to whoever is helping you:

    python diagnose.py      (or:  py diagnose.py  on Windows)

It writes netmon-diagnostics-YYYYMMDD-HHMMSS.zip next to this script,
containing a status report (versions, service state, database health),
the logs/ folder, and your three config files. It only READS — nothing
about the running monitor is touched.

NOTE: the bundle contains your device names, router IPs/MACs, and file
paths. Review report.txt and configs/ before sending it to anyone you
wouldn't show your network to.

Stdlib only; works on Windows, macOS, and Linux even when the monitor or
web server is broken — every section degrades to an error note instead of
crashing, so a half-dead install still produces a useful report.
"""

import io
import json
import os
import platform
import sqlite3
import subprocess
import sys
import zipfile
from datetime import datetime, timezone

try:
    from version import __version__
except ImportError:  # partially-copied install
    __version__ = "0.0.0"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data", "network_monitor.db")
LOG_TAIL_BYTES = 200 * 1024  # cap each shipped log at ~200KB (the tail matters)

IS_MACOS = sys.platform == "darwin"
IS_WINDOWS = sys.platform == "win32"
SUBPROCESS_EXTRA = {"creationflags": subprocess.CREATE_NO_WINDOW} if IS_WINDOWS else {}

CONFIG_FILES = ("routers.json", "devices.json", "config.json")
KEY_FILES = (
    os.path.join("data", "network_monitor.db"),
    "dashboard.html",
    os.path.join("vendor", "chart.umd.min.js"),
    os.path.join("vendor", "chartjs-adapter-date-fns.bundle.min.js"),
) + CONFIG_FILES


def _section(title, fn):
    """Run one report section, never letting it break the rest."""
    lines = ["", "=" * 60, title, "=" * 60]
    try:
        lines.extend(fn())
    except Exception as e:
        lines.append(f"!! section failed: {type(e).__name__}: {e}")
    return lines


def _sec_environment():
    return [
        f"netmon version:  {__version__}",
        f"python:          {sys.version.split()[0]} ({sys.executable})",
        f"platform:        {platform.platform()} ({platform.machine()})",
        f"install path:    {BASE_DIR}",
        f"local time:      {datetime.now().astimezone().isoformat()}",
        f"utc time:        {datetime.now(timezone.utc).isoformat()}",
    ]


def _sec_services():
    out = []
    if IS_WINDOWS:
        # Raw schtasks output on purpose: it's locale-dependent, and parsing
        # it would break on non-English Windows. Humans can read it fine.
        for task in ("NetMon Monitor", "NetMon Dashboard", "NetMon Web"):
            out.append(f"--- Task Scheduler: {task}")
            r = subprocess.run(["schtasks", "/query", "/tn", task, "/v", "/fo", "LIST"],
                               capture_output=True, text=True, timeout=15, **SUBPROCESS_EXTRA)
            out.extend((r.stdout or r.stderr or "(no output)").strip().splitlines()[:25])
    elif IS_MACOS:
        out.append("--- launchctl list | grep netmon")
        r = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=15)
        hits = [ln for ln in (r.stdout or "").splitlines() if "netmon" in ln.lower()]
        out.extend(hits or ["(no netmon services loaded)"])
    else:
        out.append("(Linux: no standard service here — check your systemd unit / cron entry)")
    return out


def _sec_files():
    out = []
    for rel in KEY_FILES:
        path = os.path.join(BASE_DIR, rel)
        if os.path.exists(path):
            st = os.stat(path)
            mtime = datetime.fromtimestamp(st.st_mtime).isoformat(sep=" ", timespec="seconds")
            out.append(f"{rel:45s} {st.st_size:>12,} bytes   modified {mtime}")
        else:
            out.append(f"{rel:45s} MISSING")
    return out


def _sec_database():
    if not os.path.exists(DB_PATH):
        return ["database not found - has the monitor ever run here?"]
    out = []
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA busy_timeout=5000")  # monitor writes constantly
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        out.append("--- tables (rows, time range where applicable)")
        for t in tables:
            count = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            span = ""
            cols = [c[1] for c in conn.execute(f"PRAGMA table_info({t})")]
            if "ts" in cols and count:
                lo, hi = conn.execute(f"SELECT MIN(ts), MAX(ts) FROM {t}").fetchone()
                span = f"   {lo} .. {hi}"
            out.append(f"{t:20s} {count:>10,}{span}")
        out.append("")
        out.append("--- last 50 events")
        for row in conn.execute(
                "SELECT start_ts, end_ts, kind, scope, router_name, note FROM events ORDER BY id DESC LIMIT 50"):
            out.append(" | ".join("" if v is None else str(v) for v in row))
    finally:
        conn.close()
    return out


def _sec_update_state():
    path = os.path.join(BASE_DIR, "data", "update_check.json")
    if not os.path.exists(path):
        return ["(no update-check state file)"]
    with open(path, encoding="utf-8") as f:
        return f.read().splitlines()


def build_bundle():
    """Assemble the report + logs + configs into a zip; returns its path.
    Kept importable so a future serve.py endpoint can reuse it."""
    report = ["Home Network Monitor - diagnostics report",
              f"generated {datetime.now(timezone.utc).isoformat()}"]
    report += _section("ENVIRONMENT", _sec_environment)
    report += _section("BACKGROUND SERVICES", _sec_services)
    report += _section("KEY FILES", _sec_files)
    report += _section("DATABASE", _sec_database)
    report += _section("UPDATE-CHECK STATE", _sec_update_state)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = os.path.join(BASE_DIR, f"netmon-diagnostics-{stamp}.zip")
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("report.txt", "\n".join(report) + "\n")

        log_dir = os.path.join(BASE_DIR, "logs")
        if os.path.isdir(log_dir):
            for name in sorted(os.listdir(log_dir)):
                if not name.endswith(".log"):
                    continue
                try:
                    with open(os.path.join(log_dir, name), "rb") as f:
                        f.seek(0, io.SEEK_END)
                        f.seek(max(0, f.tell() - LOG_TAIL_BYTES))
                        zf.writestr(f"logs/{name}", f.read())
                except OSError as e:
                    zf.writestr(f"logs/{name}.error.txt", f"could not read: {e}")

        for name in CONFIG_FILES:
            path = os.path.join(BASE_DIR, name)
            if os.path.exists(path):
                try:
                    zf.write(path, f"configs/{name}")
                except OSError as e:
                    zf.writestr(f"configs/{name}.error.txt", f"could not read: {e}")
    return out_path


def main():
    out_path = build_bundle()
    print(f"Wrote {out_path}")
    print()
    print("NOTE: this zip contains your device names, IPs/MACs, and file paths.")
    print("Review report.txt and configs/ before sending it to anyone.")


if __name__ == "__main__":
    if "--version" in sys.argv:
        print(__version__)
        sys.exit(0)
    main()
