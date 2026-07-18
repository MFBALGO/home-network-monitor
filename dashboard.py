#!/usr/bin/env python3
"""
Generate a self-contained HTML dashboard (dashboard.html) from the
network_monitor.db collected by monitor.py.

Run this on a schedule (see setup.sh / launchd plist) to keep the dashboard
fresh — e.g. every minute. It always writes a full standalone HTML file
you can open directly in a browser (no server needed) or leave open and
manually refresh.
"""

import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from html import escape as html_escape

try:
    from version import __version__
except ImportError:  # partially-copied install
    __version__ = "0.0.0"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Under pythonw.exe (how the Windows scheduled task runs this script) there
# is no console: sys.stdout/sys.stderr are None and print() would crash.
# Send output to a log file instead.
if os.name == "nt" and (sys.stdout is None or sys.stderr is None):
    _log_dir = os.path.join(BASE_DIR, "logs")
    os.makedirs(_log_dir, exist_ok=True)
    _log_file = open(os.path.join(_log_dir, "dashboard.out.log"), "a", buffering=1, encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = _log_file
    if sys.stderr is None:
        sys.stderr = _log_file
DB_PATH = os.path.join(BASE_DIR, "data", "network_monitor.db")
OUT_PATH = os.path.join(BASE_DIR, "dashboard.html")
ROUTERS_CONFIG_PATH = os.path.join(BASE_DIR, "routers.json")
DEVICE_NAMES_PATH = os.path.join(BASE_DIR, "devices.json")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

LOOKBACK_HOURS = 24 * 7  # pull a week of data; the page itself lets you toggle 24h vs 7d

# A hole in the ping timeline longer than this means the monitor wasn't
# running (Mac asleep, service stopped) — shown as "monitoring paused"
# rather than being silently invisible. Pings normally land every 15s.
MONITOR_GAP_MIN = 5


def load_json_config(path, default):
    """routers.json / devices.json are optional user-edited files — a
    malformed one should degrade the dashboard, not crash it."""
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


# Update check: ask GitHub (at most once a day, cached in data/) whether a
# newer release exists, so the dashboard can show a small "update available"
# pill. This is the ONLY thing in the toolkit that talks to anything other
# than the ping/speed-test targets; set "update_check": false in config.json
# to disable it entirely.
UPDATE_CHECK_STATE_PATH = os.path.join(BASE_DIR, "data", "update_check.json")
UPDATE_CHECK_URL = "https://api.github.com/repos/MFBALGO/home-network-monitor/releases/latest"
UPDATE_CHECK_INTERVAL_HOURS = 24


def _parse_version(tag):
    """'v0.2.0' / '0.2.0' -> (0, 2, 0); None if it doesn't look like one."""
    m = re.fullmatch(r"v?(\d+(?:\.\d+)*)", str(tag).strip())
    if not m:
        return None
    return tuple(int(p) for p in m.group(1).split("."))


def check_for_update(site_config):
    """Returns {"latest": "0.2.0", "url": ...} if a newer release exists,
    else None. Never raises and never blocks for more than ~5s: the whole
    point of this dashboard is to keep working while the internet is down,
    so a failed check is cached like a successful one (one attempt per day,
    not one per minute)."""
    try:
        if site_config.get("update_check") is False:
            return None

        state = load_json_config(UPDATE_CHECK_STATE_PATH, {})
        checked_at = state.get("checked_at")
        fresh = False
        if checked_at:
            try:
                age = datetime.now(timezone.utc) - datetime.fromisoformat(checked_at)
                fresh = age < timedelta(hours=UPDATE_CHECK_INTERVAL_HOURS)
            except ValueError:
                pass

        if not fresh:
            # Record the attempt FIRST (keeping any previously-known result),
            # so a hard failure below still counts as today's attempt.
            state["checked_at"] = datetime.now(timezone.utc).isoformat()
            try:
                import urllib.request
                req = urllib.request.Request(
                    UPDATE_CHECK_URL,
                    headers={"User-Agent": f"home-network-monitor/{__version__}"})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    release = json.loads(resp.read().decode("utf-8", errors="ignore"))
                state["latest_tag"] = release.get("tag_name")
                state["html_url"] = release.get("html_url")
            except Exception:
                pass  # offline, rate-limited, or no releases yet (404) — keep old info
            try:
                os.makedirs(os.path.dirname(UPDATE_CHECK_STATE_PATH), exist_ok=True)
                with open(UPDATE_CHECK_STATE_PATH, "w", encoding="utf-8") as f:
                    json.dump(state, f, indent=2)
            except OSError:
                pass

        latest = _parse_version(state.get("latest_tag") or "")
        current = _parse_version(__version__)
        if latest and current and latest > current:
            return {"latest": ".".join(str(p) for p in latest),
                    "url": state.get("html_url") or ""}
        return None
    except Exception:
        return None

PLACEHOLDER_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><title>Home Network Monitor</title>
<style>
  body { margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
    background:#f9f9f7; font-family: system-ui, -apple-system, "Segoe UI", sans-serif; color:#0b0b0b; }
  .card { background:#fcfcfb; border:1px solid rgba(11,11,11,0.10); border-radius:12px; padding:32px 40px; text-align:center; }
  h1 { font-size:18px; margin:0 0 8px 0; }
  p { color:#52514e; font-size:14px; margin:0; }
</style></head>
<body><div class="card"><h1>No data yet</h1><p>Start monitor.py first — this page refreshes automatically once data arrives.<br>
First time here? On the monitor machine, open <a href="http://localhost:8080/setup">the setup wizard</a>.</p></div></body></html>
"""


def q(conn, sql, params=()):
    cur = conn.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def iso(dt):
    return dt.isoformat()


def main():
    if not os.path.exists(DB_PATH):
        with open(OUT_PATH, "w", encoding="utf-8") as f:
            f.write(PLACEHOLDER_HTML)
        print("no db found, wrote placeholder")
        return

    conn = sqlite3.connect(DB_PATH)
    # monitor.py runs the DB in WAL mode and writes constantly; a busy
    # timeout stops a rare unlucky overlap from crashing this generator.
    conn.execute("PRAGMA busy_timeout=5000")
    since = iso(datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS))

    # Loaded early because the device queries below use it. "hide_ip_prefixes"
    # (optional, in config.json) drops devices whose IP starts with any listed
    # prefix from the device table and the online counts — handy for excluding
    # a leftover subnet (e.g. an old 192.168.100.x segment) that isn't really
    # "on your network" anymore.
    site_config = load_json_config(CONFIG_PATH, {})
    hide_prefixes = [p for p in (site_config.get("hide_ip_prefixes") or []) if isinstance(p, str) and p]

    def ip_hidden(ip):
        return any(str(ip).startswith(p) for p in hide_prefixes)

    # Defensive schema catch-up: dashboard.py can run (via its own launchd
    # timer) before monitor.py has been restarted to apply a newer schema,
    # so make sure the columns/tables this version expects actually exist.
    for migration in (
        "ALTER TABLE events ADD COLUMN router_name TEXT",
        "ALTER TABLE router_pings ADD COLUMN method TEXT",
    ):
        try:
            conn.execute(migration)
            conn.commit()
        except sqlite3.OperationalError:
            pass
    conn.execute(
        """CREATE TABLE IF NOT EXISTS router_pings (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, name TEXT NOT NULL,
            ip TEXT NOT NULL, success INTEGER NOT NULL, latency_ms REAL, method TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS dns_checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, domain TEXT NOT NULL,
            success INTEGER NOT NULL, latency_ms REAL
        )"""
    )
    conn.commit()

    pings = q(conn, "SELECT ts, target, target_type, success, latency_ms FROM pings WHERE ts >= ? ORDER BY ts", (since,))
    events = q(conn, "SELECT start_ts, end_ts, kind, scope, note, router_name FROM events WHERE start_ts >= ? ORDER BY start_ts DESC", (since,))
    speedtests = q(conn, "SELECT ts, download_mbps, upload_mbps, ping_ms, error FROM speedtests WHERE ts >= ? ORDER BY ts", (since,))
    wifi = q(conn, "SELECT ts, ssid, rssi_dbm, noise_dbm, channel, tx_rate_mbps FROM wifi WHERE ts >= ? ORDER BY ts", (since,))
    router_pings = q(conn, "SELECT ts, name, ip, success, latency_ms, method FROM router_pings WHERE ts >= ? ORDER BY ts", (since,))
    dns_checks = q(conn, "SELECT ts, domain, success, latency_ms FROM dns_checks WHERE ts >= ? ORDER BY ts", (since,))

    def ip_key(d):
        # numeric sort, not lexical ("...100.2" should come before "...100.153")
        try:
            return tuple(int(part) for part in d["ip"].split("."))
        except (ValueError, AttributeError):
            return (999, 999, 999, 999)

    latest_devices_ts = q(conn, "SELECT MAX(ts) as ts FROM devices")
    devices = []
    last_scan_ts = None
    device_count_series = []
    if latest_devices_ts and latest_devices_ts[0]["ts"]:
        last_scan_ts = latest_devices_ts[0]["ts"]
        # Presence history: one row per device seen in the lookback window,
        # carrying first/last-seen — not just the latest snapshot.
        devices = q(conn, """
            SELECT d.mac, d.ip, d.hostname, p.first_seen, p.last_seen, p.scans
            FROM devices d
            JOIN (SELECT mac, MIN(ts) AS first_seen, MAX(ts) AS last_seen,
                         COUNT(DISTINCT ts) AS scans
                  FROM devices WHERE ts >= ? GROUP BY mac) p
              ON d.mac = p.mac AND d.ts = p.last_seen
            GROUP BY d.mac
        """, (since,))
        # drop devices on any hidden IP prefix (e.g. a leftover subnet)
        if hide_prefixes:
            devices = [d for d in devices if not ip_hidden(d["ip"])]
        # "online" = present in (or within 10 min of) the latest scan
        online_cutoff = (datetime.fromisoformat(last_scan_ts) - timedelta(minutes=10)).isoformat()
        for dv in devices:
            dv["online"] = dv["last_seen"] >= online_cutoff

        def last_seen_key(d):
            # most-recently-seen first, so negate the timestamp; unknown/bad
            # timestamps sort to the bottom of their status group
            try:
                return -datetime.fromisoformat(d["last_seen"]).timestamp()
            except (ValueError, TypeError):
                return 0.0

        # Sort by status (online first), then last seen (most recent first),
        # with IP as a stable tiebreaker for rows sharing a timestamp (e.g. all
        # the "online now" devices from the same scan).
        devices.sort(key=lambda d: (not d["online"], last_seen_key(d), ip_key(d)))
        # devices-online-over-time, one point per scan (same hidden-prefix
        # exclusion so the chart's counts match the table below)
        count_where = "ts >= ?"
        count_params = [since]
        for p in hide_prefixes:
            count_where += " AND ip NOT LIKE ?"
            count_params.append(p + "%")
        device_count_series = q(conn,
            f"SELECT ts AS t, COUNT(DISTINCT mac) AS v FROM devices WHERE {count_where} GROUP BY ts ORDER BY ts",
            tuple(count_params))

    # Friendly device names from devices.json ({"mac": "name"}).
    def norm_mac(mac):
        parts = str(mac).strip().lower().split(":")
        if len(parts) == 6:
            return ":".join(p.zfill(2) for p in parts)
        return str(mac).strip().lower()
    device_names = {norm_mac(k): str(v).strip() for k, v in load_json_config(DEVICE_NAMES_PATH, {}).items() if str(v).strip()}
    for d in devices:
        d["name"] = device_names.get(norm_mac(d["mac"]))

    public_ip_rows = q(conn, "SELECT ts, ip FROM public_ip WHERE ts >= ? AND ip IS NOT NULL ORDER BY ts", (since,))

    conn.close()

    # ---- derived stats ----
    external = [p for p in pings if p["target_type"] == "external"]
    now = datetime.now(timezone.utc)

    # latest gateway (main router) state, for the house map's center node
    gateway_pings = [p for p in pings if p["target_type"] == "gateway"]
    gateway_info = None
    if gateway_pings:
        g = gateway_pings[-1]
        g24 = [p for p in gateway_pings if p["ts"] >= iso(now - timedelta(hours=24))]
        g24_ok = [p for p in g24 if p["success"]]
        g24_lat = [p["latency_ms"] for p in g24_ok if p["latency_ms"] is not None]
        gateway_info = {
            "ip": g["target"],
            "status": "up" if g["success"] else "down",
            "latency": g["latency_ms"],
            "uptime_pct": round(100.0 * len(g24_ok) / len(g24), 2) if g24 else None,
            "avg_latency": round(sum(g24_lat) / len(g24_lat), 1) if g24_lat else None,
        }

    def window_stats(hours, offset_hours=0):
        end = now - timedelta(hours=offset_hours)
        start = end - timedelta(hours=hours)
        w = [p for p in external if start.isoformat() <= p["ts"] <= end.isoformat()]
        if not w:
            return {"uptime_pct": None, "avg_latency": None, "loss_pct": None, "count": 0}
        successes = [p for p in w if p["success"]]
        latencies = [p["latency_ms"] for p in successes if p["latency_ms"] is not None]
        return {
            "uptime_pct": round(100.0 * len(successes) / len(w), 2),
            "avg_latency": round(sum(latencies) / len(latencies), 1) if latencies else None,
            "loss_pct": round(100.0 * (1 - len(successes) / len(w)), 2),
            "count": len(w),
        }

    stats_24h = window_stats(24)
    stats_24h_prev = window_stats(24, offset_hours=24)
    stats_7d = window_stats(24 * 7)

    def delta(a, b):
        if a is None or b is None:
            return None
        return round(a - b, 2)

    deltas_24h = {
        "uptime_pct": delta(stats_24h["uptime_pct"], stats_24h_prev["uptime_pct"]),
        "avg_latency": delta(stats_24h["avg_latency"], stats_24h_prev["avg_latency"]),
    }

    # ---- jitter: mean |Δlatency| between consecutive pings, per target ----
    # (computed per target so the different baselines of 1.1.1.1 vs 9.9.9.9
    # don't masquerade as jitter)
    def jitter_over(hours):
        cutoff = iso(now - timedelta(hours=hours))
        by_target = {}
        for p in external:
            if p["ts"] >= cutoff and p["success"] and p["latency_ms"] is not None:
                by_target.setdefault(p["target"], []).append(p["latency_ms"])
        diffs = []
        for lats in by_target.values():
            diffs += [abs(b - a) for a, b in zip(lats, lats[1:])]
        return round(sum(diffs) / len(diffs), 1) if diffs else None

    jitter_24h = jitter_over(24)

    # ---- DNS 24h summary ----
    dns_cutoff = iso(now - timedelta(hours=24))
    dns_24 = [c for c in dns_checks if c["ts"] >= dns_cutoff]
    dns_24_ok = [c["latency_ms"] for c in dns_24 if c["success"] and c["latency_ms"] is not None]
    dns_24h = {
        "avg": round(sum(dns_24_ok) / len(dns_24_ok), 1) if dns_24_ok else None,
        "failures": sum(1 for c in dns_24 if not c["success"]),
        "checks": len(dns_24),
    }

    latest = external[-1] if external else None
    current_status = "unknown"
    if latest:
        current_status = "up" if latest["success"] else "down"

    outage_events = [e for e in events if e["kind"] == "outage"]
    degraded_events = [e for e in events if e["kind"] == "degraded"]
    ip_change_events = [e for e in events if e["kind"] == "ip_change"]
    new_device_events = [e for e in events if e["kind"] == "new_device"]

    # ---- monitoring gaps (Mac asleep / monitor stopped) ----
    # A hole in the ping timeline isn't an outage and isn't uptime either —
    # nothing was being measured. Surface these explicitly so a sleeping
    # Mac can't masquerade as a healthy week.
    monitoring_gaps = []
    ping_ts_sorted = sorted({p["ts"] for p in pings})
    parsed_ts = []
    for t in ping_ts_sorted:
        try:
            parsed_ts.append(datetime.fromisoformat(t))
        except ValueError:
            pass
    for prev_t, next_t in zip(parsed_ts, parsed_ts[1:]):
        if (next_t - prev_t).total_seconds() > MONITOR_GAP_MIN * 60:
            monitoring_gaps.append({"start": iso(prev_t), "end": iso(next_t)})
    # gap still open right now (monitor not running)?
    if parsed_ts and (now - parsed_ts[-1]).total_seconds() > MONITOR_GAP_MIN * 60:
        monitoring_gaps.append({"start": iso(parsed_ts[-1]), "end": None})

    # ---- per-router summary (routers.json targets) ----
    router_names = sorted(set(p["name"] for p in router_pings))

    def router_window_stats(name, hours):
        cutoff = iso(now - timedelta(hours=hours))
        w = [p for p in router_pings if p["name"] == name and p["ts"] >= cutoff]
        if not w:
            return {"uptime_pct": None, "avg_latency": None}
        successes = [p for p in w if p["success"]]
        latencies = [p["latency_ms"] for p in successes if p["latency_ms"] is not None]
        return {
            "uptime_pct": round(100.0 * len(successes) / len(w), 2),
            "avg_latency": round(sum(latencies) / len(latencies), 1) if latencies else None,
        }

    # routers.json is the source of truth for display order and the
    # optional per-router "floor"/location label.
    router_config = load_json_config(ROUTERS_CONFIG_PATH, [])
    router_meta = {}  # name -> {"floor": ..., "order": ...}
    for idx, item in enumerate(router_config):
        rname = (item.get("name") or "").strip()
        if rname:
            router_meta[rname] = {"floor": (item.get("floor") or "").strip() or None, "order": idx}

    router_summary = []
    for name in router_names:
        rows_for_router = [p for p in router_pings if p["name"] == name]
        latest_r = rows_for_router[-1]
        stats24 = router_window_stats(name, 24)
        meta = router_meta.get(name, {})
        router_summary.append({
            "name": name,
            "floor": meta.get("floor"),
            "ip": latest_r["ip"],
            "status": "up" if latest_r["success"] else "down",
            "latency": latest_r["latency_ms"],
            "method": latest_r.get("method"),
            "uptime_pct": stats24["uptime_pct"],
            "avg_latency": stats24["avg_latency"],
        })
    # Sort by routers.json order (user-controlled, naturally groups floors
    # if the file is arranged that way); routers no longer in the config
    # (old data) sink to the bottom alphabetically.
    router_summary.sort(key=lambda r: (router_meta.get(r["name"], {}).get("order", 9999), r["name"]))

    # ---- house/site configuration (config.json — all optional) ----
    # Everything personal about the house map lives in config.json so this
    # whole folder can be copied to someone else's Mac as-is: their floors,
    # their title, their main-router placement. (site_config was already
    # loaded near the top for the device-hiding option.)
    title = (site_config.get("title") or "").strip() or "Home Network Monitor"
    floors = [f for f in (site_config.get("floors") or []) if isinstance(f, str) and f.strip()]
    if not floors:
        # derive from the routers' floor labels, in routers.json order
        seen = set()
        for r in router_summary:
            fl = r.get("floor")
            if fl and fl not in seen:
                seen.add(fl)
                floors.append(fl)
    underground = site_config.get("underground_floors")
    if underground is None:
        underground = [f for f in floors if "basement" in f.lower() or "cellar" in f.lower()]
    main_floor = site_config.get("main_router_floor")
    if main_floor not in floors:
        above_ground = [f for f in floors if f not in underground]
        # default: the lowest above-ground floor (where the ISP line usually enters)
        main_floor = above_ground[-1] if above_ground else (floors[-1] if floors else None)
    house = {"floors": floors, "underground": underground, "main_floor": main_floor}

    current_public_ip = public_ip_rows[-1]["ip"] if public_ip_rows else None
    ip_stable_since = None
    ip_stable_at_least = False
    if public_ip_rows:
        # walk backwards from the latest reading while the IP matches
        stable_since_ts = public_ip_rows[-1]["ts"]
        for row in reversed(public_ip_rows):
            if row["ip"] != current_public_ip:
                break
            stable_since_ts = row["ts"]
        ip_stable_since = stable_since_ts
        # if the streak runs all the way to the start of our lookback window,
        # we don't actually know when it first became this IP
        ip_stable_at_least = stable_since_ts == public_ip_rows[0]["ts"]

    def fmt_duration(start, end):
        try:
            s = datetime.fromisoformat(start)
            e = datetime.fromisoformat(end) if end else now.replace(tzinfo=s.tzinfo) if s.tzinfo else now
            secs = (e - s).total_seconds()
        except Exception:
            return "?"
        if secs < 60:
            return f"{int(secs)}s"
        if secs < 3600:
            return f"{int(secs//60)}m {int(secs%60)}s"
        return f"{secs/3600:.1f}h"

    # bucket external pings into 5-min buckets for the latency/loss charts
    def bucket_key(ts):
        dt = datetime.fromisoformat(ts)
        dt = dt.replace(minute=(dt.minute // 5) * 5, second=0, microsecond=0)
        return dt.isoformat()

    buckets = {}
    for p in external:
        k = bucket_key(p["ts"])
        b = buckets.setdefault(k, {"latencies": [], "total": 0, "success": 0, "by_target": {}})
        b["total"] += 1
        if p["success"]:
            b["success"] += 1
            if p["latency_ms"] is not None:
                b["latencies"].append(p["latency_ms"])
                b["by_target"].setdefault(p["target"], []).append(p["latency_ms"])

    dns_buckets = {}
    for c in dns_checks:
        if c["success"] and c["latency_ms"] is not None:
            dns_buckets.setdefault(bucket_key(c["ts"]), []).append(c["latency_ms"])

    # per-router latency buckets (same 5-min buckets as the main charts)
    router_buckets = {}  # (name, bucket_key) -> [latencies]
    for p in router_pings:
        if not p["success"] or p["latency_ms"] is None:
            continue
        k = bucket_key(p["ts"])
        router_buckets.setdefault((p["name"], k), []).append(p["latency_ms"])

    # Build a *complete* 5-min bucket axis over the whole span, including
    # empty buckets as None — so monitoring gaps show as visible breaks in
    # the charts instead of the line quietly connecting across them.
    all_keys = set(buckets.keys()) | {k for (_n, k) in router_buckets} | set(dns_buckets.keys())
    full_keys = []
    if all_keys:
        cur = datetime.fromisoformat(min(all_keys))
        end_dt = datetime.fromisoformat(max(all_keys))
        while cur <= end_dt:
            full_keys.append(iso(cur))
            cur += timedelta(minutes=5)

    latency_series = []
    loss_series = []
    jitter_series = []
    dns_series = []
    for k in full_keys:
        b = buckets.get(k)
        avg_lat = round(sum(b["latencies"]) / len(b["latencies"]), 1) if b and b["latencies"] else None
        loss = round(100.0 * (1 - b["success"] / b["total"]), 1) if b and b["total"] else None
        jit = None
        if b:
            diffs = []
            for lats in b["by_target"].values():
                diffs += [abs(y - x) for x, y in zip(lats, lats[1:])]
            jit = round(sum(diffs) / len(diffs), 1) if diffs else None
        dl = dns_buckets.get(k)
        dns_v = round(sum(dl) / len(dl), 1) if dl else None
        latency_series.append({"t": k, "v": avg_lat})
        loss_series.append({"t": k, "v": loss})
        jitter_series.append({"t": k, "v": jit})
        dns_series.append({"t": k, "v": dns_v})

    routers_chart = {
        "buckets": full_keys,
        "series": {
            r["name"]: [
                (round(sum(router_buckets[(r["name"], k)]) / len(router_buckets[(r["name"], k)]), 1)
                 if (r["name"], k) in router_buckets else None)
                for k in full_keys
            ]
            for r in router_summary
        },
    }

    sparkline = [p["v"] for p in latency_series[-12:] if p["v"] is not None]

    speed_series = [s for s in speedtests if s.get("download_mbps") is not None]
    wifi_series = [w for w in wifi if w.get("rssi_dbm") not in (None, "")]

    data = {
        "generated_at": now.isoformat(),
        "version": __version__,
        "update": check_for_update(site_config),
        "current_status": current_status,
        "current_latency": latest["latency_ms"] if latest else None,
        "stats_24h": stats_24h,
        "stats_7d": stats_7d,
        "deltas_24h": deltas_24h,
        "sparkline": sparkline,
        "outage_events": [
            {"start": e["start_ts"], "end": e["end_ts"], "scope": e["scope"], "note": e["note"],
             "router_name": e.get("router_name"),
             "duration": fmt_duration(e["start_ts"], e["end_ts"]), "ongoing": e["end_ts"] is None}
            for e in outage_events
        ],
        "degraded_events": [
            {"start": e["start_ts"], "end": e["end_ts"], "note": e["note"],
             "duration": fmt_duration(e["start_ts"], e["end_ts"]), "ongoing": e["end_ts"] is None}
            for e in degraded_events
        ],
        "ip_change_events": [
            {"start": e["start_ts"], "note": e["note"]}
            for e in ip_change_events
        ],
        "new_device_events": [
            {"start": e["start_ts"], "note": e["note"]}
            for e in new_device_events
        ],
        "monitoring_gaps": [
            {"start": g["start"], "end": g["end"],
             "duration": fmt_duration(g["start"], g["end"]), "ongoing": g["end"] is None}
            for g in monitoring_gaps
        ],
        "current_public_ip": current_public_ip,
        "ip_stable_since": ip_stable_since,
        "ip_stable_at_least": ip_stable_at_least,
        "router_summary": router_summary,
        "gateway": gateway_info,
        "house": house,
        "title": title,
        # Optional per-home overrides for the "what's normal" helpers. All
        # optional: config.json may set a "thresholds" object (per metric:
        # {"good": x, "fair": y}) and "plan_down_mbps"/"plan_up_mbps" so the
        # ratings reflect this household's internet plan. Defaults live in the
        # dashboard JS; anything omitted here just uses those.
        "thresholds": site_config.get("thresholds") if isinstance(site_config.get("thresholds"), dict) else {},
        "plan": {"down_mbps": site_config.get("plan_down_mbps"), "up_mbps": site_config.get("plan_up_mbps")},
        "latency_series": latency_series,
        "loss_series": loss_series,
        "jitter_series": jitter_series,
        "dns_series": dns_series,
        "jitter_24h": jitter_24h,
        "dns_24h": dns_24h,
        "device_count_series": device_count_series,
        "routers_chart": routers_chart,
        "speed_series": [{"t": s["ts"], "down": s["download_mbps"], "up": s["upload_mbps"], "ping": s["ping_ms"]} for s in speed_series],
        "wifi_series": [{"t": w["ts"], "rssi": w["rssi_dbm"], "channel": w["channel"]} for w in wifi_series],
        "devices": devices,
        "last_device_scan_ts": last_scan_ts,
    }

    html = build_html(data)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"wrote {OUT_PATH}")


def build_html(data):
    data_json = json.dumps(data)
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>__TITLE__</title>
<script src="vendor/chart.umd.min.js" onerror="window.__chartLibMissing = true;"></script>
<script src="vendor/chartjs-adapter-date-fns.bundle.min.js" onerror="window.__chartLibMissing = true;"></script>
<style>
  :root {
    --page: #eef2f7;
    --surface-1: #fcfdfe;
    --surface-2: #f3f6fa;
    --text-primary: #0c1424;
    --text-secondary: #42506b;
    --muted: #7b89a1;
    --grid: #dfe5ee;
    --baseline: #bfc9d9;
    --border: rgba(15,35,70,0.13);
    --border-soft: rgba(15,35,70,0.07);
    --shadow: 0 1px 2px rgba(15,35,70,0.05), 0 10px 28px -14px rgba(15,35,70,0.14);
    --accent: #0d6fb8;
    --accent-soft: rgba(13,111,184,0.09);
    --accent-glow: rgba(13,111,184,0.22);
    --gridline-bg: rgba(20,70,140,0.045);
    --font-mono: ui-monospace, "SF Mono", SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
    --cat-1: #2a78d6; --cat-2: #008300; --cat-3: #e87ba4; --cat-4: #eda100;
    --cat-5: #1baf7a; --cat-6: #eb6834; --cat-7: #4a3aa7; --cat-8: #e34948;
    --series-blue: #2a78d6;
    --series-green: #008300;
    --series-orange: #eb6834;
    --status-good: #0ca30c;
    --status-good-bg: rgba(12,163,12,0.10);
    --status-warning: #fab219;
    --status-warning-bg: rgba(250,178,25,0.15);
    --status-serious: #ec835a;
    --status-serious-bg: rgba(236,131,90,0.13);
    --status-critical: #d03b3b;
    --status-critical-bg: rgba(208,59,59,0.10);
    --success-text: #006300;
    --glow-good: rgba(12,163,12,0.20);
    --glow-bad: rgba(208,59,59,0.28);
    --glow-accent: rgba(13,111,184,0.22);
    --scene-sky-top: #b9dcf8;
    --scene-sky-bottom: #e9f4fd;
    --scene-earth-top: #e6decb;
    --scene-earth-bottom: #d8cdb4;
    --scene-grass: #79b768;
    --scene-wall: rgba(255,255,255,0.74);
    --scene-roof: #c9d3e0;
    --scene-trunk: #997a51;
    --scene-leaf: #63a856;
    --scene-sun: #f5c542;
    --scene-moon: #e8ecf4;
    --scene-smoke: rgba(110,110,110,0.35);
    color-scheme: light;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --page: #05080f;
      --surface-1: #0c121d;
      --surface-2: #101a2b;
      --text-primary: #e8eef8;
      --text-secondary: #a2b3cb;
      --muted: #5e7290;
      --grid: #172133;
      --baseline: #273650;
      --border: rgba(130,170,230,0.15);
      --border-soft: rgba(130,170,230,0.07);
      --shadow: 0 1px 2px rgba(0,0,0,0.4), 0 12px 30px -14px rgba(0,0,0,0.6);
      --accent: #3fc6ff;
      --accent-soft: rgba(63,198,255,0.09);
      --accent-glow: rgba(63,198,255,0.30);
      --gridline-bg: rgba(90,140,230,0.05);
      --cat-1: #3987e5; --cat-2: #008300; --cat-3: #d55181; --cat-4: #c98500;
      --cat-5: #199e70; --cat-6: #d95926; --cat-7: #9085e9; --cat-8: #e66767;
      --series-blue: #3987e5;
      --series-green: #008300;
      --series-orange: #d95926;
      --status-good: #0ca30c;
      --status-good-bg: rgba(12,163,12,0.14);
      --status-warning: #fab219;
      --status-warning-bg: rgba(250,178,25,0.12);
      --status-serious: #ec835a;
      --status-serious-bg: rgba(236,131,90,0.14);
      --status-critical: #e66767;
      --status-critical-bg: rgba(230,103,103,0.14);
      --success-text: #0ca30c;
      --glow-good: rgba(20,200,90,0.30);
      --glow-bad: rgba(230,103,103,0.35);
      --glow-accent: rgba(63,198,255,0.35);
      --scene-sky-top: #030510;
      --scene-sky-bottom: #0a1830;
      --scene-earth-top: #0c0f16;
      --scene-earth-bottom: #07090e;
      --scene-grass: #24422a;
      --scene-wall: rgba(10,16,28,0.78);
      --scene-roof: #1a2436;
      --scene-trunk: #4a3b29;
      --scene-leaf: #2c5433;
      --scene-sun: #f5c542;
      --scene-moon: #dfe7f5;
      --scene-smoke: rgba(190,200,220,0.16);
      color-scheme: dark;
    }
  }
  :root[data-theme="dark"] {
    --page: #05080f;
    --surface-1: #0c121d;
    --surface-2: #101a2b;
    --text-primary: #e8eef8;
    --text-secondary: #a2b3cb;
    --muted: #5e7290;
    --grid: #172133;
    --baseline: #273650;
    --border: rgba(130,170,230,0.15);
    --border-soft: rgba(130,170,230,0.07);
    --shadow: 0 1px 2px rgba(0,0,0,0.4), 0 12px 30px -14px rgba(0,0,0,0.6);
    --accent: #3fc6ff;
    --accent-soft: rgba(63,198,255,0.09);
    --accent-glow: rgba(63,198,255,0.30);
    --gridline-bg: rgba(90,140,230,0.05);
    --cat-1: #3987e5; --cat-2: #008300; --cat-3: #d55181; --cat-4: #c98500;
    --cat-5: #199e70; --cat-6: #d95926; --cat-7: #9085e9; --cat-8: #e66767;
    --series-blue: #3987e5;
    --series-green: #008300;
    --series-orange: #d95926;
    --status-good: #0ca30c;
    --status-good-bg: rgba(12,163,12,0.14);
    --status-warning: #fab219;
    --status-warning-bg: rgba(250,178,25,0.12);
    --status-serious: #ec835a;
    --status-serious-bg: rgba(236,131,90,0.14);
    --status-critical: #e66767;
    --status-critical-bg: rgba(230,103,103,0.14);
    --success-text: #0ca30c;
    --glow-good: rgba(20,200,90,0.30);
    --glow-bad: rgba(230,103,103,0.35);
    --glow-accent: rgba(63,198,255,0.35);
    --scene-sky-top: #030510;
    --scene-sky-bottom: #0a1830;
    --scene-earth-top: #0c0f16;
    --scene-earth-bottom: #07090e;
    --scene-grass: #24422a;
    --scene-wall: rgba(10,16,28,0.78);
    --scene-roof: #1a2436;
    --scene-trunk: #4a3b29;
    --scene-leaf: #2c5433;
    --scene-sun: #f5c542;
    --scene-moon: #dfe7f5;
    --scene-smoke: rgba(190,200,220,0.16);
    color-scheme: dark;
  }

  * { box-sizing: border-box; }
  body { margin:0; background: var(--page); color: var(--text-primary);
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
    transition: background .2s ease, color .2s ease; min-height: 100vh; }
  body::before { content:""; position:fixed; inset:0; pointer-events:none; z-index:0;
    background-image: linear-gradient(var(--gridline-bg) 1px, transparent 1px),
                      linear-gradient(90deg, var(--gridline-bg) 1px, transparent 1px);
    background-size: 44px 44px; }
  body::after { content:""; position:fixed; left:0; right:0; top:0; height:360px; pointer-events:none; z-index:0;
    background: radial-gradient(60% 100% at 50% 0%, var(--accent-soft), transparent 72%); }
  .topline { position:fixed; top:0; left:0; right:0; height:2px; z-index:5;
    background: linear-gradient(90deg, transparent, var(--accent) 25%, var(--accent) 75%, transparent); opacity:.7; }
  .wrap { padding: 34px 24px 60px; max-width: 1220px; margin: 0 auto; position:relative; z-index:1; }

  .topbar { display:flex; align-items:center; justify-content:space-between; margin-bottom: 26px; gap: 16px; flex-wrap: wrap; }
  .brand { display:flex; align-items:center; gap:14px; }
  .brand-mark { position:relative; width:46px; height:46px; border-radius:50%; border:1px solid var(--border);
    background: radial-gradient(circle at 50% 50%, var(--accent-soft), transparent 72%); overflow:hidden; flex-shrink:0;
    box-shadow: var(--shadow); }
  .brand-mark svg { position:absolute; inset:0; }
  .brand-mark .sweep { position:absolute; inset:0; border-radius:50%;
    background: conic-gradient(from 0deg, transparent 0deg 290deg, var(--accent-glow) 340deg, var(--accent) 360deg);
    animation: sweep 4.5s linear infinite; opacity:.55; }
  @keyframes sweep { to { transform: rotate(360deg); } }
  .brand-mark .core { position:absolute; left:50%; top:50%; width:6px; height:6px; margin:-3px 0 0 -3px; border-radius:50%;
    background: var(--accent); box-shadow: 0 0 8px 2px var(--accent-glow); }
  h1 { font-size: 16px; margin: 0 0 5px 0; letter-spacing: .14em; text-transform: uppercase; font-weight: 800; }
  .subtitle { color: var(--text-secondary); font-size: 12px; display:flex; align-items:center; gap:7px; font-family: var(--font-mono); }
  .live-dot { width:7px; height:7px; border-radius:50%; background: var(--status-good); display:inline-block;
    box-shadow: 0 0 6px var(--glow-good); animation: pulse 2.2s infinite; }
  @keyframes pulse {
    0% { box-shadow: 0 0 0 0 rgba(12,163,12,0.45); }
    70% { box-shadow: 0 0 0 6px rgba(12,163,12,0); }
    100% { box-shadow: 0 0 0 0 rgba(12,163,12,0); }
  }
  .theme-toggle { display:flex; align-items:center; gap:4px; background: var(--surface-1); border:1px solid var(--border);
    border-radius: 10px; padding: 4px; box-shadow: var(--shadow); }
  .theme-toggle button { border:none; background:transparent; color: var(--muted); font-size:10.5px; font-weight:700;
    letter-spacing:.07em; text-transform:uppercase; padding: 6px 11px; border-radius: 7px; cursor:pointer;
    font-family: var(--font-mono); transition: color .12s ease, background .12s ease; }
  .theme-toggle button:hover { color: var(--text-primary); }
  .theme-toggle button.active { background: var(--accent-soft); color: var(--accent); box-shadow: inset 0 0 0 1px var(--accent-glow); }
  #refreshCtl button { display:inline-flex; align-items:center; gap:5px; }
  #refreshCtl button.active { background: var(--status-good-bg); color: var(--status-good); box-shadow: inset 0 0 0 1px var(--glow-good); }
  #settingsLink { background: var(--surface-1); border:1px solid var(--border); border-radius:10px;
    box-shadow: var(--shadow); color: var(--muted); font-size:10.5px; font-weight:700;
    letter-spacing:.07em; text-transform:uppercase; padding: 10px 14px; cursor:pointer;
    font-family: var(--font-mono); text-decoration:none; transition: color .12s ease; }
  #settingsLink:hover { color: var(--accent); }

  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(215px, 1fr)); gap: 14px; margin-bottom: 32px; }
  .card { position:relative; background: linear-gradient(180deg, var(--surface-2), var(--surface-1) 58%);
    border: 1px solid var(--border); border-radius: 12px; padding: 16px 18px 15px; box-shadow: var(--shadow); }
  .card::before, .card::after { content:""; position:absolute; width:13px; height:13px; pointer-events:none; opacity:.55; }
  .card::before { top:-1px; left:-1px; border-top:2px solid var(--accent); border-left:2px solid var(--accent); border-top-left-radius:12px; }
  .card::after { bottom:-1px; right:-1px; border-bottom:2px solid var(--accent); border-right:2px solid var(--accent); border-bottom-right-radius:12px; }
  .card h3 { margin: 0 0 10px 0; font-size: 10px; text-transform: uppercase; letter-spacing: .15em; color: var(--muted);
    font-weight: 700; font-family: var(--font-mono); }
  .card-head { display:flex; align-items:center; justify-content:space-between; gap:8px; margin-bottom:10px; }
  .card-head h3 { margin:0; }
  @media (min-width: 940px) { .card-hero { grid-column: span 2; } }
  .stat-row { display:flex; align-items:flex-end; justify-content:space-between; gap: 10px; }
  .stat-value { font-size: 29px; font-weight: 600; letter-spacing: -.01em; line-height:1;
    font-family: var(--font-mono); font-variant-numeric: tabular-nums; }
  .stat-value .unit { font-size: 14px; color: var(--muted); font-weight: 600; margin-left: 2px; }
  .stat-sub { font-size: 11.5px; color: var(--text-secondary); margin-top: 8px; }
  /* rating pill + "what's normal" helper on each metric card */
  .rating { font-family: var(--font-mono); font-size: 9px; font-weight: 800; letter-spacing: .1em;
    padding: 2px 7px; border-radius: 5px; display: none; white-space: nowrap; flex-shrink: 0; }
  .rating.show { display: inline-block; }
  .rating.good { color: var(--status-good); background: var(--status-good-bg); box-shadow: inset 0 0 0 1px var(--glow-good); }
  .rating.fair { color: color-mix(in srgb, var(--status-warning) 80%, black); background: var(--status-warning-bg); }
  .rating.poor { color: var(--status-critical); background: var(--status-critical-bg); box-shadow: inset 0 0 0 1px var(--glow-bad); }
  .stat-hint { font-size: 10px; color: var(--muted); font-family: var(--font-mono); margin-top: 9px;
    padding-top: 8px; border-top: 1px solid var(--border-soft); line-height: 1.4; }
  .delta { font-size: 12px; font-weight: 700; display:inline-flex; align-items:center; gap:2px; font-family: var(--font-mono); }
  .delta.good { color: var(--success-text); }
  .delta.bad { color: var(--status-critical); }
  .delta.flat { color: var(--muted); }
  .sparkline { flex-shrink:0; }

  .status-pill { display: inline-flex; align-items: center; gap: 8px; padding: 6px 14px 6px 11px; border-radius: 8px;
    font-size: 13.5px; font-weight: 800; font-family: var(--font-mono); letter-spacing: .12em; text-transform: uppercase; }
  .status-up { background: var(--status-good-bg); color: var(--status-good);
    box-shadow: 0 0 16px var(--glow-good), inset 0 0 0 1px var(--glow-good); }
  .status-down { background: var(--status-critical-bg); color: var(--status-critical);
    box-shadow: 0 0 16px var(--glow-bad), inset 0 0 0 1px var(--glow-bad); }
  .status-dot { width: 8px; height: 8px; border-radius: 50%; background: currentColor; box-shadow: 0 0 6px currentColor; }
  .status-pill.small { font-size: 10.5px; padding: 3px 9px 3px 8px; letter-spacing: .08em; border-radius: 6px; box-shadow:none; }
  .status-pill.small .status-dot { box-shadow:none; }

  section { margin-bottom: 34px; }
  section .section-head { display:flex; align-items:baseline; justify-content:space-between; margin-bottom: 12px;
    flex-wrap:wrap; gap:8px; padding-bottom: 9px; border-bottom: 1px solid var(--border-soft); }
  section h2 { font-size: 12px; margin: 0; font-weight: 800; letter-spacing: .16em; text-transform: uppercase;
    font-family: var(--font-mono); display:flex; align-items:center; gap:9px; }
  section h2::before { content:""; width:7px; height:9px; background: var(--accent);
    box-shadow: 0 0 8px var(--accent-glow); clip-path: polygon(0 0, 100% 50%, 0 100%); flex-shrink:0; }
  section .section-note { font-size: 11.5px; color: var(--muted); font-family: var(--font-mono); }

  .range-toggle { display:flex; gap:4px; background: var(--surface-1); border:1px solid var(--border); border-radius: 9px; padding: 3px; }
  .range-toggle button { border:none; background:transparent; color: var(--muted); font-size:11px; font-weight:700;
    font-family: var(--font-mono); letter-spacing:.05em; padding: 5px 11px; border-radius: 6px; cursor:pointer; }
  .range-toggle button.active { background: var(--accent-soft); color: var(--accent); box-shadow: inset 0 0 0 1px var(--accent-glow); }

  .chart-card { background: var(--surface-1); border: 1px solid var(--border); border-radius: 12px; padding: 18px;
    box-shadow: var(--shadow); overflow-x:auto; position:relative; }
  .chart-card + .chart-card { margin-top: 12px; }
  /* two-up responsive grid: two charts per row on wide screens, one on phones */
  .chart-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 12px; }
  .chart-grid > .chart-card { margin-top: 0; }
  /* fixed-height wrapper so maintainAspectRatio:false charts stay compact */
  .chart-box { position: relative; width: 100%; height: 168px; }
  .chart-box.sm { height: 140px; }
  .chart-box.lg { height: 188px; }
  .chart-box > canvas { position: absolute; inset: 0; }
  /* keep charts compact on phones too (single-column there) */
  @media (max-width: 640px) {
    .chart-box { height: 130px; }
    .chart-box.sm { height: 116px; }
    .chart-box.lg { height: 150px; }
  }
  .chart-label { font-size: 10.5px; font-weight: 700; color: var(--muted); margin-bottom: 10px;
    font-family: var(--font-mono); text-transform: uppercase; letter-spacing: .13em; }
  .panel-hud::before, .panel-hud::after { content:""; position:absolute; width:15px; height:15px; pointer-events:none; opacity:.55; }
  .panel-hud::before { top:-1px; left:-1px; border-top:2px solid var(--accent); border-left:2px solid var(--accent); border-top-left-radius:12px; }
  .panel-hud::after { bottom:-1px; right:-1px; border-bottom:2px solid var(--accent); border-right:2px solid var(--accent); border-bottom-right-radius:12px; }

  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; color: var(--muted); font-weight: 700; font-size: 9.5px; text-transform: uppercase;
    letter-spacing: .13em; font-family: var(--font-mono); border-bottom: 1px solid var(--grid); padding: 8px 10px; }
  td { padding: 9px 10px; border-bottom: 1px solid var(--border-soft); font-variant-numeric: tabular-nums; vertical-align: middle; }
  tbody tr { transition: background .12s ease; }
  tbody tr:hover { background: var(--surface-2); }
  tr:last-child td { border-bottom: none; }
  tr.event-row td:first-child { border-left: 3px solid transparent; }
  tr.event-gateway td:first-child { border-left-color: var(--status-serious); }
  tr.event-internet td:first-child { border-left-color: var(--status-critical); }
  tr.event-degraded td:first-child { border-left-color: var(--status-warning); }
  tr.event-ipchange td:first-child { border-left-color: var(--series-blue); }

  .badge { display:inline-flex; align-items:center; gap:6px; font-weight:700; font-size:12.5px; }
  .badge .dot { width:7px; height:7px; border-radius:50%; flex-shrink:0; box-shadow: 0 0 6px currentColor; }
  .badge-gateway { color: var(--status-serious); } .badge-gateway .dot { background: var(--status-serious); }
  .badge-internet { color: var(--status-critical); } .badge-internet .dot { background: var(--status-critical); }
  .badge-degraded { color: var(--status-warning); } .badge-degraded .dot { background: color-mix(in srgb, var(--status-warning) 75%, black); }
  .badge-ipchange { color: var(--series-blue); } .badge-ipchange .dot { background: var(--series-blue); }
  .badge-newdevice { color: var(--series-green); } .badge-newdevice .dot { background: var(--series-green); }
  .badge-dns { color: var(--status-serious); } .badge-dns .dot { background: var(--status-serious); }
  tr.event-dns td:first-child { border-left-color: var(--status-serious); }
  .badge-gap { color: var(--muted); } .badge-gap .dot { background: var(--muted); box-shadow:none; }
  tr.event-newdevice td:first-child { border-left-color: var(--series-green); }
  tr.event-gap td:first-child { border-left-color: var(--baseline); }
  tr.event-gap td { color: var(--text-secondary); }
  .ongoing-tag { background: var(--status-critical-bg); color: var(--status-critical); font-size: 10px; font-weight:800;
    font-family: var(--font-mono); text-transform:uppercase; letter-spacing:.08em; padding: 2px 7px; border-radius: 5px;
    box-shadow: inset 0 0 0 1px var(--glow-bad); animation: blinkSoft 1.6s ease-in-out infinite; }
  @keyframes blinkSoft { 50% { opacity: .55; } }

  .ghost-btn { border:1px solid var(--border); background: var(--surface-2); color: var(--text-secondary);
    font-family: var(--font-mono); font-size:11px; font-weight:700; letter-spacing:.08em; text-transform:uppercase;
    padding:7px 16px; border-radius:8px; cursor:pointer; transition: color .12s ease, border-color .12s ease; }
  .ghost-btn:hover { color: var(--accent); border-color: var(--accent-glow); }

  /* ---------- outages: summary chips + incident timeline + filters ---------- */
  .outage-summary { display:grid; grid-template-columns: repeat(auto-fit, minmax(148px,1fr)); gap:10px; margin-bottom:18px; }
  .osum { background: var(--surface-2); border:1px solid var(--border-soft); border-radius:10px; padding:11px 13px 12px; position:relative; overflow:hidden; }
  .osum::before { content:""; position:absolute; left:0; top:0; bottom:0; width:3px; background: var(--muted); opacity:.85; }
  .osum.good::before { background: var(--status-good); } .osum.warn::before { background: var(--status-warning); } .osum.bad::before { background: var(--status-critical); }
  .osum .k { font-size:9px; text-transform:uppercase; letter-spacing:.13em; color:var(--muted); font-family:var(--font-mono); font-weight:700; }
  .osum .v { font-size:21px; font-weight:600; font-family:var(--font-mono); font-variant-numeric:tabular-nums; margin-top:6px; letter-spacing:-.01em; line-height:1; }
  .osum .s { font-size:10.5px; color:var(--text-secondary); margin-top:5px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .osum.good .v { color: var(--status-good); } .osum.warn .v { color: var(--status-warning); } .osum.bad .v { color: var(--status-critical); }

  .timeline-head { display:flex; align-items:baseline; justify-content:space-between; gap:10px; margin-bottom:9px; flex-wrap:wrap; }
  .timeline-label { font-size:10.5px; font-weight:700; color:var(--muted); font-family:var(--font-mono); text-transform:uppercase; letter-spacing:.13em; }
  .timeline-legend { display:flex; gap:12px; flex-wrap:wrap; font-family:var(--font-mono); font-size:10px; color:var(--text-secondary); }
  .timeline-legend .tlk { display:inline-flex; align-items:center; gap:5px; }
  .timeline-legend .tlk i { width:9px; height:9px; border-radius:2px; display:inline-block; }
  .timeline-svg { display:block; width:100%; height:auto; }
  .timeline-svg text { font-family: var(--font-mono); }
  .timeline-svg .tl-grid { stroke: var(--grid); stroke-width:1; }
  .timeline-svg .tl-day { fill: var(--muted); font-size:10px; }
  .timeline-svg .tl-track { fill: var(--surface-2); stroke: var(--border-soft); }
  .timeline-svg .tl-now { stroke: var(--accent); stroke-width:1.5; stroke-dasharray:2 3; }
  .timeline-svg .tl-nowlab { fill: var(--accent); font-size:9px; font-weight:700; }
  .timeline-svg .tl-ev { filter: drop-shadow(0 0 3px currentColor); }
  .timeline-empty { padding:14px 2px; }

  .outage-filters { display:flex; gap:6px; flex-wrap:wrap; margin:18px 0 12px; }
  .ofilter { border:1px solid var(--border); background:var(--surface-1); color:var(--muted); font-family:var(--font-mono);
    font-size:10.5px; font-weight:700; letter-spacing:.04em; text-transform:uppercase; padding:5px 11px; border-radius:7px;
    cursor:pointer; display:inline-flex; align-items:center; gap:6px; transition: color .12s ease, background .12s ease; }
  .ofilter:hover { color:var(--text-primary); }
  .ofilter.active { background: var(--accent-soft); color: var(--accent); box-shadow: inset 0 0 0 1px var(--accent-glow); }
  .ofilter .cnt { font-size:9.5px; opacity:.75; font-variant-numeric:tabular-nums; }
  .outage-clear { display:flex; align-items:center; gap:8px; color:var(--status-good); font-family:var(--font-mono);
    font-size:12px; font-weight:700; padding:16px 2px; }
  .outage-clear .status-dot { box-shadow: 0 0 6px var(--glow-good); }

  .device-name { display:flex; align-items:center; gap:8px; }
  .device-icon { width:26px; height:26px; border-radius:7px; background: var(--surface-2); border:1px solid var(--border);
    display:flex; align-items:center; justify-content:center; flex-shrink:0; color: var(--muted); }
  .mono { font-family: var(--font-mono); font-size: 12px; color: var(--text-secondary); }

  .empty { color: var(--muted); font-size: 13px; padding: 20px 4px; text-align:center; }
  .legend-note { display: grid; grid-template-columns: repeat(auto-fit, minmax(340px, 1fr)); gap: 8px 28px;
    font-size: 12px; color: var(--text-secondary); margin-top: 12px; padding-top: 12px; border-top: 1px solid var(--border-soft); }
  .legend-item { display: flex; align-items: baseline; gap: 8px; line-height: 1.45; }
  .legend-item .badge { flex-shrink: 0; white-space: nowrap; min-width: 118px; }
  .footer-note { text-align:center; color: var(--muted); font-size: 11.5px; margin-top: 44px; font-family: var(--font-mono); }
  #updatePill { margin-left: 10px; padding: 2px 9px; border-radius: 999px; font-size: 10.5px;
    font-family: var(--font-mono); text-decoration: none; letter-spacing: 0.4px;
    color: var(--accent); border: 1px solid var(--accent); opacity: 0.9; }
  #updatePill:hover { opacity: 1; }
  .warning-banner { display:none; background: var(--status-warning-bg); border: 1px solid var(--border);
    border-left: 3px solid var(--status-warning); border-radius: 8px; padding: 12px 16px; margin-bottom: 20px;
    font-size: 13px; color: var(--text-primary); }
  .warning-banner b { display:block; margin-bottom: 2px; }

  /* ---------- house map ---------- */
  .house-svg { display:block; width:100%; max-width: 860px; margin: 0 auto; height:auto; }
  .house-svg .wall { fill: var(--scene-wall); stroke: var(--baseline); stroke-width: 1.5; }
  .house-svg .roof { fill: var(--scene-roof); stroke: var(--baseline); stroke-width: 1.5; stroke-linejoin: round; }
  .house-svg .basement-band { fill: var(--grid); opacity: 0.32; }
  .house-svg .floor-sep { stroke: var(--grid); stroke-width: 1.2; stroke-dasharray: 2 6; }
  .house-svg .floor-label { fill: var(--muted); font-size: 9.5px; font-weight: 700; letter-spacing: .12em;
    text-transform: uppercase; font-family: var(--font-mono); }
  .house-svg .floor-chip { fill: var(--surface-1); stroke: var(--border-soft); }
  .house-svg .scene-grass { fill: var(--scene-grass); }
  .house-svg .street-label { fill: var(--muted); font-size: 9px; font-weight: 700; letter-spacing: .16em;
    text-transform: uppercase; font-family: var(--font-mono); opacity: 0.75; }
  .house-svg .scene-trunk { fill: var(--scene-trunk); }
  .house-svg .scene-leaf { fill: var(--scene-leaf); }
  .house-svg .scene-smoke { fill: var(--scene-smoke); animation: smokeRise 4s ease-out infinite; }
  @keyframes smokeRise { 0% { transform: translateY(0); opacity: 0; } 15% { opacity: 1; } 100% { transform: translateY(-30px); opacity: 0; } }
  .house-svg .scene-cloud { fill: var(--surface-1); opacity: .9; animation: cloudDrift 26s ease-in-out infinite alternate; }
  @keyframes cloudDrift { from { transform: translateX(0); } to { transform: translateX(26px); } }
  .house-svg .scene-star { fill: var(--scene-moon); animation: twinkle 2.6s ease-in-out infinite alternate; }
  @keyframes twinkle { from { opacity: .2; } to { opacity: 1; } }
  .house-svg .scene-moon, .house-svg .scene-star { display: none; }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) .house-svg .scene-sun,
    :root:not([data-theme="light"]) .house-svg .scene-cloud { display: none; }
    :root:not([data-theme="light"]) .house-svg .scene-moon,
    :root:not([data-theme="light"]) .house-svg .scene-star { display: inline; }
  }
  :root[data-theme="dark"] .house-svg .scene-sun,
  :root[data-theme="dark"] .house-svg .scene-cloud { display: none; }
  :root[data-theme="dark"] .house-svg .scene-moon,
  :root[data-theme="dark"] .house-svg .scene-star { display: inline; }
  .house-svg .node-up { color: var(--status-good); }
  .house-svg .node-down { color: var(--status-critical); }
  .house-svg .node-main { color: var(--accent); }
  .house-svg g.node-up { filter: drop-shadow(0 0 7px var(--glow-good)); }
  .house-svg g.node-down { filter: drop-shadow(0 0 9px var(--glow-bad)); }
  .house-svg g.node-main { filter: drop-shadow(0 0 9px var(--glow-accent)); }
  .house-svg .card-box { fill: var(--surface-1); stroke: currentColor; stroke-width: 1.5; }
  .house-svg .card-accent { fill: currentColor; }
  .house-svg .card-name { fill: var(--text-primary); font-size: 12.5px; font-weight: 700; }
  .house-svg .card-mono { fill: var(--text-secondary); font-size: 10.5px; font-family: var(--font-mono); }
  .house-svg .card-stats { fill: var(--text-secondary); font-size: 10px; font-family: var(--font-mono); font-variant-numeric: tabular-nums; }
  .house-svg .card-status { fill: currentColor; font-size: 9.5px; font-weight: 800; letter-spacing: .09em;
    text-transform: uppercase; font-family: var(--font-mono); }
  .house-svg .status-dot-svg { fill: currentColor; }
  .house-svg .node-down .status-dot-svg { animation: hardBlink 1.1s steps(2, start) infinite; }
  @keyframes hardBlink { 50% { opacity: 0.1; } }
  .house-svg .bar-track { fill: var(--grid); }
  .house-svg .bar-fill { fill: currentColor; opacity: .85; }
  .house-svg .linkgrp.up { color: var(--status-good); }
  .house-svg .linkgrp.down { color: var(--status-critical); }
  .house-svg .link-glow { fill: none; stroke: currentColor; stroke-width: 6; opacity: .10; stroke-linecap: round; }
  .house-svg .link-core { fill: none; stroke: currentColor; stroke-width: 1.8; stroke-linecap: round; opacity: .6; }
  .house-svg .linkgrp.up .link-core { stroke-dasharray: 4 9; animation: dashflow 1.2s linear infinite; }
  .house-svg .linkgrp.down .link-core { stroke-dasharray: 3 6; opacity: .8; }
  @keyframes dashflow { to { stroke-dashoffset: -26; } }
  .house-svg .packet { fill: currentColor; }
  .house-svg .ripple { fill: none; stroke: var(--accent); }
  @media (prefers-reduced-motion: reduce) {
    .house-svg .scene-smoke, .house-svg .scene-cloud, .house-svg .scene-star,
    .house-svg .linkgrp.up .link-core, .house-svg .node-down .status-dot-svg,
    .brand-mark .sweep, .live-dot, .ongoing-tag { animation: none; }
  }
</style>
</head>
<body>
<div class="topline"></div>
<div class="wrap">
  <div class="warning-banner" id="chartLibWarning">
    <b>Charts couldn't load.</b>
    <span>The chart library (vendor/chart.umd.min.js) is missing or failed to load, so the stats above still work but the charts below can't render. Run <span class="mono">bash setup.sh</span> again from Terminal to download it — that's a one-time fix.</span>
  </div>
  <div class="topbar">
    <div class="brand">
      <div class="brand-mark">
        <svg viewBox="0 0 46 46" fill="none">
          <circle cx="23" cy="23" r="8" stroke="var(--accent)" stroke-opacity="0.5" stroke-width="1"/>
          <circle cx="23" cy="23" r="14" stroke="var(--accent)" stroke-opacity="0.3" stroke-width="1"/>
          <circle cx="23" cy="23" r="20" stroke="var(--accent)" stroke-opacity="0.15" stroke-width="1"/>
        </svg>
        <div class="sweep"></div>
        <div class="core"></div>
      </div>
      <div>
        <h1>__TITLE__</h1>
        <div class="subtitle"><span class="live-dot"></span><span id="generatedAt"></span><a id="updatePill" target="_blank" rel="noopener" style="display:none"></a></div>
      </div>
    </div>
    <div style="display:flex; gap:10px; align-items:center; flex-wrap:wrap;">
      <div class="theme-toggle" id="refreshCtl">
        <button id="refreshBtn" title="Reload the page now">
          <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-2.64-6.36"/><polyline points="21 3 21 9 15 9"/></svg>
          Refresh
        </button>
        <button id="autoRefreshBtn" title="Reload automatically every minute — the page regenerates every 60s">Auto: off</button>
      </div>
      <div class="theme-toggle" id="themeToggle">
        <button data-theme="light">Light</button>
        <button data-theme="dark">Dark</button>
        <button data-theme="auto">Auto</button>
      </div>
      <a id="settingsLink" style="display:none" title="Configure routers, floors, and device names (only available on the monitor PC)">Settings</a>
    </div>
  </div>

  <div class="grid">
    <div class="card card-hero">
      <h3>Current status</h3>
      <div class="stat-row">
        <div>
          <div id="statusPill"></div>
          <div class="stat-sub" id="currentLatency"></div>
        </div>
        <canvas class="sparkline" id="sparkline" width="150" height="44"></canvas>
      </div>
    </div>
    <div class="card">
      <div class="card-head"><h3>Uptime · 24h</h3><span class="rating" id="rateUptime24"></span></div>
      <div class="stat-row">
        <div class="stat-value" id="uptime24h">—</div>
        <div class="delta" id="uptimeDelta"></div>
      </div>
      <div class="stat-sub" id="loss24h"></div>
      <div class="stat-hint" id="hintUptime24"></div>
    </div>
    <div class="card">
      <div class="card-head"><h3>Uptime · 7d</h3><span class="rating" id="rateUptime7"></span></div>
      <div class="stat-value" id="uptime7d">—</div>
      <div class="stat-sub" id="loss7d"></div>
      <div class="stat-hint" id="hintUptime7"></div>
    </div>
    <div class="card">
      <div class="card-head"><h3>Avg latency · 24h</h3><span class="rating" id="rateLatency"></span></div>
      <div class="stat-row">
        <div class="stat-value" id="avgLatency24h">—</div>
        <div class="delta" id="latencyDelta"></div>
      </div>
      <div class="stat-sub">to 1.1.1.1 / 8.8.8.8 / 9.9.9.9</div>
      <div class="stat-hint" id="hintLatency"></div>
    </div>
    <div class="card">
      <div class="card-head"><h3>DNS · 24h</h3><span class="rating" id="rateDns"></span></div>
      <div class="stat-value" id="dnsAvg">—</div>
      <div class="stat-sub" id="dnsSub">name-lookup speed</div>
      <div class="stat-hint" id="hintDns"></div>
    </div>
    <div class="card">
      <div class="card-head"><h3>Jitter · 24h</h3><span class="rating" id="rateJitter"></span></div>
      <div class="stat-value" id="jitter24h">—</div>
      <div class="stat-sub">latency stability — lower is steadier (calls/gaming)</div>
      <div class="stat-hint" id="hintJitter"></div>
    </div>
    <div class="card">
      <h3>Public IP</h3>
      <div class="stat-value" id="publicIp" style="font-size:19px;">—</div>
      <div class="stat-sub" id="publicIpStable"></div>
    </div>
  </div>

  <section>
    <div class="section-head">
      <h2>Routers &amp; access points</h2>
      <span class="section-note">from routers.json</span>
    </div>
    <div class="chart-card panel-hud">
      <div id="houseMapWrap"></div>
      <div id="houseMapNote" class="section-note" style="display:none; text-align:center; margin-top:6px;"></div>
    </div>
    <div class="chart-card">
      <div class="chart-label">Per-router latency (ms)</div>
      <div class="chart-box lg"><canvas id="routersChart"></canvas></div>
      <div id="routersChartEmpty" class="empty" style="display:none">No router ping history yet.</div>
    </div>
  </section>

  <section>
    <div class="section-head">
      <h2>Outages &amp; degradation log</h2>
      <span class="section-note">last 7 days, most recent first</span>
    </div>
    <div class="chart-card">
      <div id="outageSummary" class="outage-summary"></div>
      <div class="timeline-head">
        <span class="timeline-label">Incident timeline · last 7 days</span>
        <span class="timeline-legend" id="timelineLegend"></span>
      </div>
      <div id="outageTimeline"></div>
      <div id="outageFilters" class="outage-filters"></div>
      <div id="outagesTableWrap"></div>
      <div class="legend-note">
        <div class="legend-item"><span class="badge badge-gateway"><span class="dot"></span>Gateway down</span><span>Your router/Wi-Fi (or a router from the list above) was unreachable — local issue.</span></div>
        <div class="legend-item"><span class="badge badge-internet"><span class="dot"></span>Internet down</span><span>Gateway was fine but the internet wasn't — likely your ISP.</span></div>
        <div class="legend-item"><span class="badge badge-degraded"><span class="dot"></span>Slow / degraded</span><span>Nothing fully down, but latency or packet loss was elevated.</span></div>
        <div class="legend-item"><span class="badge badge-dns"><span class="dot"></span>DNS failure</span><span>Name lookups were failing — sites won't load by name even though pings still work.</span></div>
        <div class="legend-item"><span class="badge badge-ipchange"><span class="dot"></span>Public IP changed</span><span>Informational — your ISP reassigned your address (common around brief reconnects).</span></div>
        <div class="legend-item"><span class="badge badge-newdevice"><span class="dot"></span>New device</span><span>A never-seen-before device joined the network — name it in <span class="mono">devices.json</span>.</span></div>
        <div class="legend-item"><span class="badge badge-gap"><span class="dot"></span>Monitoring paused</span><span>No data was collected (Mac asleep or monitor stopped) — not an outage, but not measured uptime either.</span></div>
      </div>
    </div>
  </section>

  <section>
    <div class="section-head">
      <h2>Latency &amp; packet loss</h2>
      <div class="range-toggle" data-rangetoggle>
        <button data-range="24">24h</button>
        <button data-range="168" class="active">7d</button>
      </div>
    </div>
    <div class="chart-grid">
      <div class="chart-card">
        <div class="chart-label">Average latency (ms)</div>
        <div class="chart-box"><canvas id="latencyChart"></canvas></div>
      </div>
      <div class="chart-card">
        <div class="chart-label">Packet loss (%)</div>
        <div class="chart-box sm"><canvas id="lossChart"></canvas></div>
      </div>
    </div>
  </section>

  <section>
    <div class="section-head">
      <h2>Speed test &amp; Wi-Fi</h2>
      <div class="range-toggle" data-rangetoggle>
        <button data-range="24">24h</button>
        <button data-range="168" class="active">7d</button>
      </div>
    </div>
    <div class="chart-grid">
      <div class="chart-card">
        <div class="chart-label">Speed test (Mbps)</div>
        <div class="chart-box"><canvas id="speedChart"></canvas></div>
        <div id="speedEmpty" class="empty" style="display:none">No speed test data yet — install a speed test tool (see README) and it will appear here automatically.</div>
      </div>
      <div class="chart-card">
        <div class="chart-label">Wi-Fi signal (dBm, higher is better)</div>
        <div class="chart-box sm"><canvas id="wifiChart"></canvas></div>
        <div id="wifiEmpty" class="empty" style="display:none">No Wi-Fi signal data yet.</div>
      </div>
    </div>
  </section>

  <section>
    <div class="section-head">
      <h2>Devices on your network</h2>
      <div style="display:flex; align-items:center; gap:12px; flex-wrap:wrap;">
        <span class="section-note" id="devicesNote">most recent scan</span>
        <div class="range-toggle" data-rangetoggle title="Applies to the Devices-online chart; the table below always shows the latest scan">
          <button data-range="24">24h</button>
          <button data-range="168" class="active">7d</button>
        </div>
      </div>
    </div>
    <div class="chart-card">
      <div class="chart-label">Devices online over time</div>
      <div class="chart-box sm"><canvas id="devCountChart"></canvas></div>
      <div id="devCountEmpty" class="empty" style="display:none">No scan history yet.</div>
    </div>
    <div class="chart-card">
      <div id="devicesTableWrap"></div>
    </div>
  </section>

  <div class="footer-note" id="footerNote">Everything on this page stays local — regenerated every minute by dashboard.py. Reload to see the latest.</div>
</div>

<script>
const DATA = __DATA_JSON__;

function escapeHtml(s) {
  if (s == null) return '';
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

if (typeof Chart === 'undefined' || window.__chartLibMissing) {
  document.getElementById('chartLibWarning').style.display = 'block';
}

// Guard each block independently so one failure can't blank the rest of
// the page — an uncaught error stops the rest of a script block cold.
function safely(label, fn) {
  try { fn(); } catch (e) { console.error('[dashboard] ' + label + ' failed:', e); }
}

const REDUCE_MOTION = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

// ---------- theme ----------
const root = document.documentElement;
let currentTheme = 'auto';
function applyTheme(t) {
  currentTheme = t;
  if (t === 'auto') { root.removeAttribute('data-theme'); }
  else { root.setAttribute('data-theme', t); }
  document.querySelectorAll('#themeToggle button').forEach(b => b.classList.toggle('active', b.dataset.theme === t));
  // charts bake resolved colors in at build time, so a theme change means
  // a full re-render, not just an update()
  if (window.__rerenderCharts) window.__rerenderCharts();
  try { localStorage.setItem('netmon-theme', t); } catch (e) {}
}
document.getElementById('themeToggle').addEventListener('click', (e) => {
  const btn = e.target.closest('button');
  if (btn) applyTheme(btn.dataset.theme);
});

function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

// ---------- header ----------
document.getElementById('generatedAt').textContent =
  'Updated ' + new Date(DATA.generated_at).toLocaleString();

if (DATA.update && DATA.update.latest) {
  const up = document.getElementById('updatePill');
  up.textContent = 'Update available: v' + DATA.update.latest;
  if (DATA.update.url) up.href = DATA.update.url;
  up.style.display = '';
}

// Settings only work from the machine running the monitor (serve.py rejects
// everyone else), so only surface the link where it will actually work:
// viewed via http://localhost:8080 or opened as a local file on that machine.
(() => {
  const link = document.getElementById('settingsLink');
  if (['localhost', '127.0.0.1', '[::1]'].includes(location.hostname)) {
    link.href = '/settings';
    link.style.display = '';
  } else if (location.protocol === 'file:') {
    link.href = 'http://localhost:8080/settings';
    link.style.display = '';
  }
})();
if (DATA.version) {
  document.getElementById('footerNote').textContent =
    'netmon v' + DATA.version + ' — everything on this page stays local, regenerated every minute by dashboard.py. Reload to see the latest.';
}

const pill = document.getElementById('statusPill');
if (DATA.current_status === 'up') {
  pill.innerHTML = '<span class="status-pill status-up"><span class="status-dot"></span>Online</span>';
} else if (DATA.current_status === 'down') {
  pill.innerHTML = '<span class="status-pill status-down"><span class="status-dot"></span>Offline</span>';
} else {
  pill.innerHTML = '<span class="status-pill">Unknown</span>';
}
document.getElementById('currentLatency').textContent =
  DATA.current_latency != null ? (Math.round(DATA.current_latency * 10) / 10) + ' ms right now' : 'no recent ping';

function setStat(id, value, unit) {
  const el = document.getElementById(id);
  if (value == null) { el.textContent = '—'; return; }
  el.innerHTML = escapeHtml(value) + (unit ? '<span class="unit">' + unit + '</span>' : '');
}
setStat('uptime24h', DATA.stats_24h.uptime_pct, '%');
document.getElementById('loss24h').textContent = DATA.stats_24h.loss_pct != null ? DATA.stats_24h.loss_pct + '% packet loss' : '';
setStat('uptime7d', DATA.stats_7d.uptime_pct, '%');
document.getElementById('loss7d').textContent = DATA.stats_7d.loss_pct != null ? DATA.stats_7d.loss_pct + '% packet loss' : '';
setStat('avgLatency24h', DATA.stats_24h.avg_latency, 'ms');
setStat('dnsAvg', DATA.dns_24h ? DATA.dns_24h.avg : null, 'ms');
if (DATA.dns_24h && DATA.dns_24h.checks) {
  document.getElementById('dnsSub').textContent = DATA.dns_24h.failures
    ? DATA.dns_24h.failures + ' failed lookups' : 'all lookups succeeded';
}
setStat('jitter24h', DATA.jitter_24h, 'ms');

function timeSince(ts) {
  const secs = (Date.now() - new Date(ts).getTime()) / 1000;
  if (secs < 3600) return Math.max(1, Math.round(secs / 60)) + 'm';
  if (secs < 86400) return Math.round(secs / 3600) + 'h';
  return Math.round(secs / 86400) + 'd';
}
document.getElementById('publicIp').textContent = DATA.current_public_ip || '—';
if (DATA.current_public_ip && DATA.ip_stable_since) {
  const prefix = DATA.ip_stable_at_least ? 'stable for at least ' : 'stable for ';
  document.getElementById('publicIpStable').textContent = prefix + timeSince(DATA.ip_stable_since);
} else {
  document.getElementById('publicIpStable').textContent = '';
}

function renderDelta(elId, value, opts) {
  const el = document.getElementById(elId);
  if (value == null || value === 0) { el.textContent = ''; return; }
  const invert = opts && opts.invertGood; // true when a smaller number is the good direction
  const isUp = value > 0;
  const isGood = invert ? !isUp : isUp;
  el.className = 'delta ' + (isGood ? 'good' : 'bad');
  const arrow = isUp ? '↑' : '↓';
  el.textContent = arrow + ' ' + Math.abs(value) + (opts && opts.suffix ? opts.suffix : '');
  el.title = 'vs previous 24h';
}
renderDelta('uptimeDelta', DATA.deltas_24h.uptime_pct, { suffix: 'pt' });
renderDelta('latencyDelta', DATA.deltas_24h.avg_latency, { suffix: 'ms', invertGood: true });

// ---------- "what's normal" ratings + thresholds ----------
// Each metric has a good/fair boundary and a direction: 'low' = smaller is
// better (latency, jitter, DNS, loss), 'high' = bigger is better (uptime,
// signal, speed). Defaults reflect common home-network norms; a household
// can override any of them in config.json → "thresholds": {"latency": {...}}.
const THRESHOLDS = {
  latency: { good: 40,   fair: 100,  dir: 'low',  unit: 'ms', labels: ['GOOD', 'FAIR', 'HIGH'] },
  jitter:  { good: 10,   fair: 30,   dir: 'low',  unit: 'ms', labels: ['GOOD', 'FAIR', 'HIGH'] },
  dns:     { good: 40,   fair: 100,  dir: 'low',  unit: 'ms', labels: ['GOOD', 'FAIR', 'SLOW'] },
  loss:    { good: 1,    fair: 2.5,  dir: 'low',  unit: '%',  labels: ['GOOD', 'FAIR', 'HIGH'] },
  uptime:  { good: 99.9, fair: 99,   dir: 'high', unit: '%',  labels: ['GOOD', 'FAIR', 'LOW']  },
  wifi:    { good: -60,  fair: -70,  dir: 'high', unit: 'dBm', labels: ['STRONG', 'OK', 'WEAK'] },
};
// merge per-metric overrides from config.json (only good/fair are honoured)
Object.keys(DATA.thresholds || {}).forEach(k => {
  if (THRESHOLDS[k] && DATA.thresholds[k] && typeof DATA.thresholds[k] === 'object') {
    if (DATA.thresholds[k].good != null) THRESHOLDS[k].good = DATA.thresholds[k].good;
    if (DATA.thresholds[k].fair != null) THRESHOLDS[k].fair = DATA.thresholds[k].fair;
  }
});
// 0 = good, 1 = fair, 2 = poor, null = no data
function rateLevel(v, key) {
  const t = THRESHOLDS[key];
  if (v == null || !t) return null;
  if (t.dir === 'low') return v <= t.good ? 0 : (v <= t.fair ? 1 : 2);
  return v >= t.good ? 0 : (v >= t.fair ? 1 : 2);
}
function hintText(key) {
  const t = THRESHOLDS[key];
  const g = t.good, f = t.fair, u = t.unit;
  return t.dir === 'low'
    ? `Good ≤ ${g} · OK ≤ ${f} ${u}`
    : `Good ≥ ${g} · OK ≥ ${f} ${u}`;
}
function applyRating(rateId, hintId, value, key, hintOverride) {
  const rEl = document.getElementById(rateId);
  const hEl = document.getElementById(hintId);
  const lvl = rateLevel(value, key);
  if (rEl) {
    if (lvl == null) { rEl.className = 'rating'; rEl.textContent = ''; }
    else {
      rEl.className = 'rating show ' + ['good', 'fair', 'poor'][lvl];
      rEl.textContent = THRESHOLDS[key].labels[lvl];
    }
  }
  if (hEl) hEl.textContent = hintOverride || hintText(key);
}
safely('metric ratings', function() {
  applyRating('rateUptime24', 'hintUptime24', DATA.stats_24h.uptime_pct, 'uptime');
  applyRating('rateUptime7',  'hintUptime7',  DATA.stats_7d.uptime_pct,  'uptime');
  applyRating('rateLatency',  'hintLatency',  DATA.stats_24h.avg_latency, 'latency');
  applyRating('rateDns',      'hintDns',      DATA.dns_24h ? DATA.dns_24h.avg : null, 'dns');
  applyRating('rateJitter',   'hintJitter',   DATA.jitter_24h, 'jitter');
});

// ---------- sparkline ----------
function renderSparkline() {
  const c = document.getElementById('sparkline');
  const vals = DATA.sparkline;
  if (!vals || vals.length < 2) { c.style.display = 'none'; return; }
  const ctx = c.getContext('2d');
  const w = c.width, h = c.height, pad = 4;
  const min = Math.min(...vals), max = Math.max(...vals);
  const span = (max - min) || 1;
  const accent = cssVar('--accent') || '#3fc6ff';
  ctx.clearRect(0, 0, w, h);
  ctx.save();
  ctx.shadowColor = accent;
  ctx.shadowBlur = REDUCE_MOTION ? 0 : 6;
  ctx.beginPath();
  vals.forEach((v, i) => {
    const x = pad + (i / (vals.length - 1)) * (w - pad * 2);
    const y = h - pad - ((v - min) / span) * (h - pad * 2);
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  ctx.strokeStyle = accent;
  ctx.lineWidth = 2;
  ctx.lineJoin = 'round';
  ctx.lineCap = 'round';
  ctx.stroke();
  const lastX = pad + (w - pad * 2);
  const lastY = h - pad - ((vals[vals.length - 1] - min) / span) * (h - pad * 2);
  ctx.beginPath();
  ctx.arc(lastX, lastY, 3, 0, Math.PI * 2);
  ctx.fillStyle = accent;
  ctx.fill();
  ctx.restore();
}
safely('sparkline', renderSparkline);

// ---------- outages: summary + timeline + filters + list ----------
safely('outages log', function() {
  const NOW = Date.parse(DATA.generated_at) || Date.now();
  const WIN = 7 * 86400 * 1000;          // timeline / summary window: 7 days
  const WIN_START = NOW - WIN;

  function durSecs(e) {
    const s = Date.parse(e.start);
    const en = e.end ? Date.parse(e.end) : NOW;   // ongoing runs "to now"
    if (isNaN(s)) return 0;
    return Math.max(0, (en - s) / 1000);
  }
  function fmtDur(secs) {
    if (secs == null) return '—';
    secs = Math.round(secs);
    if (secs < 60) return secs + 's';
    if (secs < 3600) return Math.floor(secs / 60) + 'm ' + (secs % 60) + 's';
    if (secs < 86400) return Math.floor(secs / 3600) + 'h ' + Math.round((secs % 3600) / 60) + 'm';
    return Math.floor(secs / 86400) + 'd ' + Math.round((secs % 86400) / 3600) + 'h';
  }

  // classify every event once: category, display label, colors, geometry
  const TL_COLOR = { outage: 'var(--status-critical)', dns: 'var(--status-serious)',
    slow: 'var(--status-warning)', device: 'var(--status-good)', ip: 'var(--series-blue)',
    paused: 'var(--muted)' };
  function classify(e) {
    let cat, label, badgeClass, rowClass, point = false;
    if (e.kind === 'outage' && e.scope === 'gateway') { cat='outage'; label='Gateway down'; badgeClass='badge-gateway'; rowClass='event-gateway'; }
    else if (e.kind === 'outage' && e.scope === 'router') { cat='outage'; label=(e.router_name||'Router')+' down'; badgeClass='badge-gateway'; rowClass='event-gateway'; }
    else if (e.kind === 'outage' && e.scope === 'dns') { cat='dns'; label='DNS failure'; badgeClass='badge-dns'; rowClass='event-dns'; }
    else if (e.kind === 'outage') { cat='outage'; label='Internet down'; badgeClass='badge-internet'; rowClass='event-internet'; }
    else if (e.kind === 'degraded') { cat='slow'; label='Slow / degraded'; badgeClass='badge-degraded'; rowClass='event-degraded'; }
    else if (e.kind === 'new_device') { cat='device'; label='New device'; badgeClass='badge-newdevice'; rowClass='event-newdevice'; point=true; }
    else if (e.kind === 'gap') { cat='paused'; label='Monitoring paused'; badgeClass='badge-gap'; rowClass='event-gap'; }
    else { cat='ip'; label='Public IP changed'; badgeClass='badge-ipchange'; rowClass='event-ipchange'; point=true; }
    return { ...e, cat, label, badgeClass, rowClass, point,
             startMs: Date.parse(e.start), endMs: e.end ? Date.parse(e.end) : NOW,
             durSecs: point ? 0 : durSecs(e), tlColor: TL_COLOR[cat] };
  }

  const allEvents = [
    ...DATA.outage_events.map(e => ({...e, kind: 'outage'})),
    ...DATA.degraded_events.map(e => ({...e, kind: 'degraded', scope: 'internet'})),
    ...DATA.ip_change_events.map(e => ({...e, kind: 'ip_change', scope: 'internet', ongoing: false, duration: '—'})),
    ...(DATA.new_device_events || []).map(e => ({...e, kind: 'new_device', scope: 'lan', ongoing: false, duration: '—'})),
    ...(DATA.monitoring_gaps || []).map(e => ({...e, kind: 'gap', scope: 'local',
        note: 'No data collected — Mac was likely asleep or the monitor was stopped'})),
  ].map(classify).sort((a, b) => b.startMs - a.startMs);

  // ---- summary (last 7 days) ----
  const inWin = allEvents.filter(e => e.startMs >= WIN_START);
  const hardDown = inWin.filter(e => e.cat === 'outage' || e.cat === 'dns');
  const downtime = hardDown.reduce((a, e) => a + e.durSecs, 0);
  const longest = hardDown.reduce((a, e) => Math.max(a, e.durSecs), 0);
  const slowCount = inWin.filter(e => e.cat === 'slow').length;
  // most-affected: tally hard-down events by a friendly location label
  const tally = {};
  hardDown.forEach(e => {
    let who = 'Internet';
    if (e.scope === 'gateway') who = 'Gateway';
    else if (e.scope === 'router') who = e.router_name || 'Router';
    else if (e.cat === 'dns') who = 'DNS';
    tally[who] = (tally[who] || 0) + 1;
  });
  let worst = null, worstN = 0;
  Object.keys(tally).forEach(k => { if (tally[k] > worstN) { worst = k; worstN = tally[k]; } });

  const chips = [
    { k: 'Outages · 7d', v: String(hardDown.length), s: hardDown.length ? 'reachability failures' : 'none — all clear',
      cls: hardDown.length ? 'bad' : 'good' },
    { k: 'Total downtime', v: fmtDur(downtime), s: 'summed outage time', cls: downtime > 0 ? 'bad' : 'good' },
    { k: 'Longest outage', v: longest ? fmtDur(longest) : '—', s: 'single worst event', cls: longest ? 'warn' : 'good' },
    { k: 'Most affected', v: worst || '—', s: worst ? worstN + (worstN === 1 ? ' outage' : ' outages') : (slowCount ? slowCount + ' slow spells' : 'nothing'),
      cls: worst ? 'warn' : 'good' },
  ];
  document.getElementById('outageSummary').innerHTML = chips.map(c =>
    `<div class="osum ${c.cls}"><div class="k">${c.k}</div><div class="v">${escapeHtml(c.v)}</div><div class="s">${escapeHtml(c.s)}</div></div>`
  ).join('');

  // ---- incident timeline (SVG, last 7 days) ----
  const tlWrap = document.getElementById('outageTimeline');
  const LEFT = 12, RIGHT = 988, TOP = 22, TRACK_H = 40, BOT = TOP + TRACK_H;
  const clamp = ms => Math.max(WIN_START, Math.min(NOW, ms));
  const xFor = ms => LEFT + (clamp(ms) - WIN_START) / WIN * (RIGHT - LEFT);
  let svg = `<svg class="timeline-svg" viewBox="0 0 1000 92" role="img" aria-label="Incident timeline, last 7 days">`;
  svg += `<rect class="tl-track" x="${LEFT}" y="${TOP}" width="${RIGHT - LEFT}" height="${TRACK_H}" rx="6"/>`;
  // day gridlines + labels (8 marks, one per day boundary)
  for (let i = 0; i <= 7; i++) {
    const x = LEFT + i / 7 * (RIGHT - LEFT);
    const t = WIN_START + i / 7 * WIN;
    svg += `<line class="tl-grid" x1="${x.toFixed(1)}" y1="${TOP - 4}" x2="${x.toFixed(1)}" y2="${BOT + 4}"/>`;
    const anchor = i === 0 ? 'start' : (i === 7 ? 'end' : 'middle');
    const lab = new Date(t).toLocaleDateString(undefined, { weekday: 'short' });
    svg += `<text class="tl-day" x="${x.toFixed(1)}" y="${BOT + 18}" text-anchor="${anchor}">${i === 7 ? 'today' : lab}</text>`;
  }
  // duration bars first (under the point ticks)
  const winEvents = allEvents.filter(e => e.endMs >= WIN_START && e.startMs <= NOW);
  winEvents.filter(e => !e.point).forEach(e => {
    const x0 = xFor(e.startMs), x1 = xFor(e.endMs);
    const w = Math.max(3, x1 - x0);
    const title = `${e.label} · ${new Date(e.startMs).toLocaleString()} · ${e.ongoing ? 'ongoing' : fmtDur(e.durSecs)}`;
    const op = e.cat === 'paused' ? 0.5 : 0.92;
    svg += `<rect class="tl-ev" x="${x0.toFixed(1)}" y="${TOP + 3}" width="${w.toFixed(1)}" height="${TRACK_H - 6}" rx="2" fill="${e.tlColor}" style="color:${e.tlColor}" opacity="${op}"><title>${escapeHtml(title)}</title></rect>`;
  });
  // point events (new device / IP change) as ticks
  winEvents.filter(e => e.point).forEach(e => {
    const x = xFor(e.startMs);
    const title = `${e.label} · ${new Date(e.startMs).toLocaleString()}`;
    svg += `<g fill="${e.tlColor}" style="color:${e.tlColor}"><rect class="tl-ev" x="${(x - 1).toFixed(1)}" y="${TOP + 2}" width="2" height="${TRACK_H - 4}"/><circle class="tl-ev" cx="${x.toFixed(1)}" cy="${TOP - 1}" r="3"/><title>${escapeHtml(title)}</title></g>`;
  });
  // "now" marker
  svg += `<line class="tl-now" x1="${RIGHT}" y1="${TOP - 6}" x2="${RIGHT}" y2="${BOT + 4}"/>`;
  svg += `<text class="tl-nowlab" x="${RIGHT}" y="${TOP - 9}" text-anchor="end">NOW</text>`;
  if (winEvents.length === 0) {
    svg += `<text x="500" y="${TOP + TRACK_H / 2 + 4}" text-anchor="middle" fill="var(--status-good)" font-size="12" font-family="var(--font-mono)">No incidents in the last 7 days</text>`;
  }
  svg += `</svg>`;
  tlWrap.innerHTML = svg;

  // timeline colour legend
  const legendKeys = [['Down', 'var(--status-critical)'], ['DNS', 'var(--status-serious)'],
    ['Slow', 'var(--status-warning)'], ['New device', 'var(--status-good)'], ['Paused', 'var(--muted)']];
  document.getElementById('timelineLegend').innerHTML = legendKeys.map(([n, c]) =>
    `<span class="tlk"><i style="background:${c}"></i>${n}</span>`).join('');

  // ---- filter chips ----
  const CAT_NAMES = { outage: 'Outages', dns: 'DNS', slow: 'Slow', device: 'Devices', ip: 'IP changes', paused: 'Paused' };
  const CAT_ORDER = ['outage', 'dns', 'slow', 'device', 'ip', 'paused'];
  const counts = {};
  allEvents.forEach(e => { counts[e.cat] = (counts[e.cat] || 0) + 1; });
  const present = CAT_ORDER.filter(c => counts[c]);
  const filtersWrap = document.getElementById('outageFilters');
  if (allEvents.length) {
    filtersWrap.innerHTML =
      `<button class="ofilter active" data-cat="all">All <span class="cnt">${allEvents.length}</span></button>` +
      present.map(c => `<button class="ofilter" data-cat="${c}">${CAT_NAMES[c]} <span class="cnt">${counts[c]}</span></button>`).join('');
  }

  // ---- the list (filterable, collapsible) ----
  const outWrap = document.getElementById('outagesTableWrap');
  const EVENTS_SHOWN = 10;
  let activeCat = 'all';

  function renderList() {
    const rowsData = (activeCat === 'all' ? allEvents : allEvents.filter(e => e.cat === activeCat)).slice(0, 200);
    if (rowsData.length === 0) {
      outWrap.innerHTML = activeCat === 'all'
        ? '<div class="outage-clear"><span class="status-dot" style="background:var(--status-good)"></span>No outages or slowdowns detected yet. Good news.</div>'
        : '<div class="empty">Nothing in this category.</div>';
      return;
    }
    const rows = rowsData.map((e, idx) => {
      const badge = `<span class="badge ${e.badgeClass}"><span class="dot"></span>${escapeHtml(e.label)}</span>`;
      const dur = e.ongoing ? '<span class="ongoing-tag">Ongoing</span>' : (e.point ? '—' : escapeHtml(e.duration));
      const hidden = idx >= EVENTS_SHOWN ? ' style="display:none" data-extra="1"' : '';
      return `<tr class="event-row ${e.rowClass}"${hidden}>
        <td>${new Date(e.startMs).toLocaleString()}</td>
        <td>${badge}</td>
        <td>${dur}</td>
        <td class="mono">${escapeHtml(e.note)}</td>
      </tr>`;
    }).join('');
    const extraCount = rowsData.length - EVENTS_SHOWN;
    const moreBtn = extraCount > 0
      ? `<div style="text-align:center; margin-top:10px;"><button id="eventsToggle" class="ghost-btn">Show ${extraCount} older</button></div>`
      : '';
    outWrap.innerHTML = `<table><thead><tr><th>Started</th><th>Type</th><th>Duration</th><th>Detail</th></tr></thead><tbody>${rows}</tbody></table>${moreBtn}`;
    if (extraCount > 0) {
      let expanded = false;
      document.getElementById('eventsToggle').addEventListener('click', () => {
        expanded = !expanded;
        outWrap.querySelectorAll('tr[data-extra]').forEach(tr => { tr.style.display = expanded ? '' : 'none'; });
        document.getElementById('eventsToggle').textContent = expanded ? 'Show fewer' : `Show ${extraCount} older`;
      });
    }
  }
  renderList();

  filtersWrap.addEventListener('click', (ev) => {
    const btn = ev.target.closest('.ofilter');
    if (!btn) return;
    activeCat = btn.dataset.cat;
    filtersWrap.querySelectorAll('.ofilter').forEach(b => b.classList.toggle('active', b === btn));
    renderList();
  });
});

// ---------- devices table ----------
const deviceIcon = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>';
const devWrap = document.getElementById('devicesTableWrap');
safely('devices note', function() {
  // Show when the scan actually ran — stale device data used to be
  // indistinguishable from fresh data, which hid a broken scanner for hours.
  const noteEl = document.getElementById('devicesNote');
  if (DATA.last_device_scan_ts) {
    const ageMin = (Date.now() - new Date(DATA.last_device_scan_ts).getTime()) / 60000;
    const online = (DATA.devices || []).filter(d => d.online).length;
    const total = (DATA.devices || []).length;
    let note = online + ' online · ' + total + ' seen this week · scanned ' + timeSince(DATA.last_device_scan_ts) + ' ago';
    if (ageMin > 15) {
      note += ' — stale! scans should run every 5 min; check the monitor service';
      noteEl.style.color = 'var(--status-critical)';
      noteEl.style.fontWeight = '700';
    }
    noteEl.textContent = note;
  }
});
if (!DATA.devices || DATA.devices.length === 0) {
  devWrap.innerHTML = '<div class="empty">No device scan data yet.</div>';
} else {
  let rows = DATA.devices.map(d => {
    // friendly name from devices.json wins; hostname as detail or fallback
    const friendly = d.name ? escapeHtml(d.name) : null;
    const host = d.hostname ? escapeHtml(d.hostname) : null;
    let label;
    if (friendly && host && friendly.toLowerCase() !== host.toLowerCase()) {
      label = `<b>${friendly}</b> <span class="mono">${host}</span>`;
    } else {
      label = friendly ? `<b>${friendly}</b>` : (host || 'Unknown device');
    }
    const pill = d.online
      ? '<span class="status-pill small status-up"><span class="status-dot"></span>Online</span>'
      : '<span class="status-pill small" style="background:var(--border-soft);color:var(--muted)"><span class="status-dot"></span>Away</span>';
    const seen = d.online ? 'now' : (d.last_seen ? timeSince(d.last_seen) + ' ago' : '—');
    return `<tr>
    <td><div class="device-name"><span class="device-icon">${deviceIcon}</span><span>${label}</span></div></td>
    <td class="mono">${escapeHtml(d.ip)}</td>
    <td class="mono">${escapeHtml(d.mac)}</td>
    <td>${pill}</td>
    <td>${seen}</td>
  </tr>`;
  }).join('');
  devWrap.innerHTML = `<table><thead><tr><th>Device</th><th>IP</th><th>MAC</th><th>Status</th><th>Last seen</th></tr></thead><tbody>${rows}</tbody></table>`;
}

// ---------- house map ----------
safely('house map', function() {
  const wrap = document.getElementById('houseMapWrap');
  const routers = (DATA.router_summary || []);
  const gw = DATA.gateway;
  if (!gw && routers.length === 0) {
    wrap.innerHTML = `<div class="empty">No routers configured yet. On the monitor PC, open <a href="http://localhost:8080/setup">the setup wizard</a> (or Settings &rarr; Routers) — or hand-edit <span class="mono">routers.json</span>; changes are picked up within 15 seconds. See the README for the format.</div>`;
    return;
  }

  // ---- dynamic geometry: floors come from config.json (or are derived
  // from routers.json), so any house layout renders correctly ----
  const H = DATA.house || {};
  const floorNames = (H.floors && H.floors.length)
    ? H.floors
    : [...new Set(routers.map(r => r.floor).filter(Boolean))];
  if (floorNames.length === 0) floorNames.push('Home');   // gateway-only fallback
  const UG = new Set(H.underground || []);
  const TOP = 122, BH = 172;              // wall top, per-floor band height
  const FLOORS = floorNames.map((k, i) => ({ key: k, y0: TOP + i * BH, y1: TOP + (i + 1) * BH }));
  const houseBottom = TOP + floorNames.length * BH;
  // ground level sits above the first underground floor; if there is no
  // underground floor, it's the bottom of the house
  let groundY = houseBottom;
  for (const f of FLOORS) { if (UG.has(f.key)) { groundY = f.y0; break; } }
  const totalH = houseBottom + 35;
  const HX0 = 100, HX1 = 900;             // house walls (yard on both sides)
  const CARD_W = 176, CARD_H = 88;        // router info card size
  const mainFloor = FLOORS.find(f => f.key === H.main_floor) || FLOORS[Math.floor(FLOORS.length / 2)];
  const MAIN = { x: 500, y: (mainFloor.y0 + mainFloor.y1) / 2 };

  const wifiIcon = (cx, cy, scale) => `
    <g transform="translate(${cx},${cy}) scale(${scale}) translate(-12,-13)" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round">
      <path d="M5 12.55a11 11 0 0 1 14.08 0"/>
      <path d="M8.53 16.11a6 6 0 0 1 6.95 0"/>
      <circle cx="12" cy="20" r="1.6" fill="currentColor" stroke="none"/>
    </g>`;

  const spread = (n, a, b) => Array.from({length: n}, (_, i) => a + (i + 1) * (b - a) / (n + 1));
  const linspace = (n, a, b) => n === 1 ? [(a + b) / 2] : Array.from({length: n}, (_, i) => a + i * (b - a) / (n - 1));

  // place each router on its floor; the main router's floor splits cards
  // left/right of it so nothing overlaps
  const placed = [], unplaced = [];
  const EDGE = HX0 + 14 + CARD_W / 2, EDGE2 = HX1 - 14 - CARD_W / 2;
  FLOORS.forEach(f => {
    let rs = routers.filter(r => r.floor === f.key);
    let xs;
    if (f.key === mainFloor.key) {
      // this floor hosts the main router — split its routers left/right
      const mainHalf = (CARD_W + 16) / 2;  // main card is a bit wider
      const leftRs = [], rightRs = [];
      rs.forEach((r, i) => (i % 2 === 0 ? leftRs : rightRs).push(r));
      const leftXs = spread(leftRs.length, HX0 + 14, MAIN.x - mainHalf - 12 - CARD_W / 2);
      const rightXs = spread(rightRs.length, MAIN.x + mainHalf + 12 + CARD_W / 2, HX1 - 14);
      rs = [...leftRs, ...rightRs];
      xs = [...leftXs, ...rightXs];
    } else {
      xs = linspace(rs.length, EDGE, EDGE2);
    }
    const cy = (f.y0 + f.y1) / 2 + 8;  // nudge down to clear the floor label
    rs.forEach((r, i) => placed.push({ ...r, x: xs[i], y: cy }));
  });
  routers.forEach(r => { if (!FLOORS.some(f => f.key === r.floor)) unplaced.push(r); });

  const fmtPct = v => v != null ? v + '%' : '—';
  const fmtMs = v => v != null ? v + ' ms' : '—';

  // One info card per router: name+status, IP, latency, and a 24h-uptime
  // bar — the same details as the table, readable at a glance on the map.
  function card(x, y, opts) {
    const w = opts.main ? CARD_W + 16 : CARD_W, h = CARD_H;
    const x0 = x - w / 2, y0 = y - h / 2;
    const cls = opts.main ? 'node-main' : (opts.status === 'up' ? 'node-up' : 'node-down');
    // 'tcp' = alive via its web port; 'arp' = alive but silent (answers
    // only ARP — blocks ping, no web admin on standard ports)
    const statusTxt = opts.status === 'up'
      ? (opts.method === 'tcp' ? 'Online · web' : opts.method === 'arp' ? 'Online · silent' : 'Online')
      : 'Offline';
    const pctv = opts.uptime_pct;
    const barW = w - 28;
    const fillW = pctv != null ? Math.max(2, barW * Math.min(100, pctv) / 100) : 0;
    return `<g class="${cls}">
      <rect class="card-box" x="${x0}" y="${y0}" width="${w}" height="${h}" rx="11"/>
      <rect class="card-accent" x="${x0 + 1}" y="${y0 + 12}" width="3" height="${h - 24}" rx="1.5" opacity="0.85"/>
      ${wifiIcon(x0 + w - 20, y0 + 19, 0.8)}
      <text class="card-name" x="${x0 + 14}" y="${y0 + 21}">${escapeHtml(opts.name)}</text>
      <text class="card-mono" x="${x0 + 14}" y="${y0 + 37}">${escapeHtml(opts.ip || '')}</text>
      <circle class="status-dot-svg" cx="${x0 + 18}" cy="${y0 + 50.5}" r="3.4"/>
      <text class="card-status" x="${x0 + 27}" y="${y0 + 54}">${statusTxt}</text>
      <text class="card-stats" x="${x0 + w - 12}" y="${y0 + 54}" text-anchor="end">${fmtMs(opts.avg_latency)}</text>
      <rect class="bar-track" x="${x0 + 14}" y="${y0 + 63}" width="${barW}" height="4" rx="2"/>
      ${pctv != null ? `<rect class="bar-fill" x="${x0 + 14}" y="${y0 + 63}" width="${Math.min(fillW, barW)}" height="4" rx="2"/>` : ''}
      <text class="card-stats" x="${x0 + 14}" y="${y0 + 80}">24h uptime ${fmtPct(pctv)}</text>
    </g>`;
  }

  let svg = `<svg class="house-svg" viewBox="0 0 1000 ${totalH}" role="img" aria-label="Map of routers by floor">`;

  // ---- gradients ----
  svg += `<defs>
    <linearGradient id="skyGrad" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="var(--scene-sky-top)"/><stop offset="1" stop-color="var(--scene-sky-bottom)"/>
    </linearGradient>
    <linearGradient id="earthGrad" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="var(--scene-earth-top)"/><stop offset="1" stop-color="var(--scene-earth-bottom)"/>
    </linearGradient>
  </defs>`;

  // ---- backdrop: sky above ground level, earth below ----
  svg += `<rect fill="url(#skyGrad)" x="0" y="0" width="1000" height="${totalH}" rx="12"/>`;
  svg += `<rect fill="url(#earthGrad)" x="0" y="${groundY - 2}" width="1000" height="${totalH - groundY + 2}"/>`;

  // sun (light theme) / moon + stars (dark theme) — swapped via CSS
  svg += `<g class="scene-sun"><circle cx="150" cy="70" r="32" fill="var(--scene-sun)" opacity="0.16"/><circle cx="150" cy="70" r="24" fill="var(--scene-sun)" opacity="0.32"/><circle cx="150" cy="70" r="16" fill="var(--scene-sun)"/></g>`;
  svg += `<g class="scene-moon"><circle cx="858" cy="64" r="30" fill="var(--scene-moon)" opacity="0.12"/><circle cx="858" cy="64" r="19" fill="var(--scene-moon)"/><circle cx="866" cy="58" r="16" fill="var(--scene-sky-top)"/></g>`;
  [[60,42,1.4],[120,90,1.0],[190,36,1.7],[250,72,0.9],[330,28,1.2],[400,58,0.8],[470,34,1.5],[560,80,1.0],[620,30,1.2],[700,54,0.9],[760,86,1.3],[930,34,1.6],[950,96,1.0],[975,60,1.2],[290,102,0.8],[860,110,0.9]].forEach((s, i) => {
    svg += `<circle class="scene-star" cx="${s[0]}" cy="${s[1]}" r="${s[2]}" style="animation-delay:${-(i * 0.45).toFixed(2)}s"/>`;
  });
  svg += `<g class="scene-cloud"><ellipse cx="270" cy="52" rx="26" ry="11"/><ellipse cx="250" cy="57" rx="16" ry="8"/><ellipse cx="291" cy="57" rx="17" ry="8"/></g>`;
  svg += `<g class="scene-cloud" style="animation-delay:-13s"><ellipse cx="676" cy="34" rx="20" ry="8"/><ellipse cx="661" cy="38" rx="12" ry="6"/><ellipse cx="692" cy="38" rx="13" ry="6"/></g>`;

  // yard: tree on the left, bush on the right, grass strip at ground level
  svg += `<rect class="scene-trunk" x="44" y="${groundY - 58}" width="9" height="54" rx="2"/>`;
  svg += `<circle class="scene-leaf" cx="48" cy="${groundY - 70}" r="24"/><circle class="scene-leaf" cx="33" cy="${groundY - 56}" r="16"/><circle class="scene-leaf" cx="64" cy="${groundY - 58}" r="15"/>`;
  svg += `<circle class="scene-leaf" cx="952" cy="${groundY - 18}" r="15"/><circle class="scene-leaf" cx="968" cy="${groundY - 13}" r="11"/>`;
  svg += `<rect class="scene-grass" x="0" y="${groundY - 8}" width="1000" height="10" rx="5"/>`;
  // label the ground line when part of the house is below it, using the
  // same words as the settings editor's divider
  if (groundY < houseBottom) {
    svg += `<text class="street-label" x="930" y="${groundY - 16}" text-anchor="end">street level</text>`;
  }

  // chimney (behind the roof) with drifting smoke
  svg += `<rect class="roof" x="760" y="40" width="34" height="52"/>`;
  svg += `<rect class="roof" x="754" y="33" width="46" height="9" rx="2"/>`;
  [[770, 5], [780, 6], [775, 4]].forEach((p, i) => {
    svg += `<circle class="scene-smoke" cx="${p[0]}" cy="24" r="${p[1]}" style="animation-delay:${-(i * 1.4).toFixed(1)}s"/>`;
  });

  // house: roof, walls, floors
  svg += `<polygon class="roof" points="500,20 ${HX0 - 24},${TOP} ${HX1 + 24},${TOP}"/>`;
  svg += `<rect class="wall" x="${HX0}" y="${TOP}" width="${HX1 - HX0}" height="${houseBottom - TOP}"/>`;
  FLOORS.forEach(f => {
    if (UG.has(f.key)) {
      svg += `<rect class="basement-band" x="${HX0 + 1}" y="${f.y0 + 1}" width="${HX1 - HX0 - 2}" height="${f.y1 - f.y0 - 2}"/>`;
    }
    if (f.y0 > TOP) {
      svg += `<line class="floor-sep" x1="${HX0}" y1="${f.y0}" x2="${HX1}" y2="${f.y0}"/>`;
    }
  });
  FLOORS.forEach(f => {
    const chipW = f.key.length * 6.6 + 20;
    svg += `<rect class="floor-chip" x="${HX0 + 12}" y="${f.y0 + 9}" width="${chipW}" height="20" rx="7"/>`;
    svg += `<text class="floor-label" x="${HX0 + 21}" y="${f.y0 + 23}">${escapeHtml(f.key)}</text>`;
  });

  // links (under the cards): a soft glow stroke + an animated dashed core,
  // plus a data packet travelling the path when the router is up
  placed.forEach((p, i) => {
    const midx = (MAIN.x + p.x) / 2, midy = (MAIN.y + p.y) / 2 - 26;
    const d = `M ${MAIN.x} ${MAIN.y} Q ${midx} ${midy} ${p.x} ${p.y}`;
    const up = p.status === 'up';
    svg += `<g class="linkgrp ${up ? 'up' : 'down'}">`;
    svg += `<path class="link-glow" d="${d}"/>`;
    svg += `<path class="link-core" d="${d}"/>`;
    if (up && !REDUCE_MOTION) {
      const dur = (2.6 + (i % 4) * 0.45).toFixed(2);
      svg += `<circle class="packet" r="2.6" opacity="0.9"><animateMotion dur="${dur}s" begin="${-(i * 0.7).toFixed(1)}s" repeatCount="indefinite" path="${d}"/></circle>`;
    }
    svg += `</g>`;
  });

  // radar ripples radiating from the main router
  if (gw && !REDUCE_MOTION) {
    svg += `<circle class="ripple" cx="${MAIN.x}" cy="${MAIN.y}" r="40" stroke-width="1.5">
      <animate attributeName="r" from="40" to="120" dur="3.6s" repeatCount="indefinite"/>
      <animate attributeName="opacity" values="0.5;0" dur="3.6s" repeatCount="indefinite"/>
    </circle>`;
    svg += `<circle class="ripple" cx="${MAIN.x}" cy="${MAIN.y}" r="40" stroke-width="1.5">
      <animate attributeName="r" from="40" to="120" begin="-1.8s" dur="3.6s" repeatCount="indefinite"/>
      <animate attributeName="opacity" values="0.5;0" begin="-1.8s" dur="3.6s" repeatCount="indefinite"/>
    </circle>`;
  }

  // router cards
  placed.forEach(p => { svg += card(p.x, p.y, p); });

  // main router card, drawn last so it sits on top of the link fan
  if (gw) {
    svg += card(MAIN.x, MAIN.y, {
      main: true, name: 'Main Router', ip: gw.ip,
      status: gw.status, uptime_pct: gw.uptime_pct, avg_latency: gw.avg_latency,
    });
  }

  svg += `</svg>`;
  wrap.innerHTML = svg;

  if (unplaced.length) {
    const n = document.getElementById('houseMapNote');
    n.style.display = 'block';
    n.textContent = 'Not on the map yet (no floor assigned): ' + unplaced.map(r => r.name).join(', ')
      + ' — pick each one’s floor in Settings → Routers.';
  }
});

// ---------- shared chart look ----------
function gridColor() { return cssVar('--grid'); }
function tickColor() { return cssVar('--muted'); }
function legendColor() { return cssVar('--text-secondary'); }
function surfaceColor() { return cssVar('--surface-1'); }
function textColor() { return cssVar('--text-primary'); }
function borderColor() { return cssVar('--border'); }
function monoFont() { return cssVar('--font-mono') || 'ui-monospace, Menlo, monospace'; }
function catColor(i) { return cssVar('--cat-' + ((i % 8) + 1)) || '#3987e5'; }

function baseTicks() { return { color: tickColor(), font: { family: monoFont(), size: 10 } }; }
function timeScale() {
  return { type: 'time', time: { unit: currentRangeHours <= 24 ? 'hour' : 'day' },
    grid: { color: gridColor() }, border: { display: false }, ticks: baseTicks() };
}
function fixedTimeScale(unit) {
  return { type: 'time', time: { unit: unit },
    grid: { color: gridColor() }, border: { display: false }, ticks: baseTicks() };
}
function yScale(titleText, extra) {
  return Object.assign({
    title: { display: true, text: titleText, color: tickColor(), font: { family: monoFont(), size: 10 } },
    grid: { color: gridColor() }, border: { display: false }, ticks: baseTicks(), beginAtZero: true,
  }, extra || {});
}
function tooltipBase() {
  return {
    backgroundColor: cssVar('--surface-2') || surfaceColor(),
    titleColor: textColor(),
    bodyColor: textColor(),
    borderColor: borderColor(),
    borderWidth: 1,
    padding: 10,
    boxPadding: 5,
    cornerRadius: 9,
    titleFont: { family: monoFont(), size: 11 },
    bodyFont: { family: monoFont(), size: 11 },
    displayColors: true,
  };
}
function legendOpts(show) {
  return { display: show, labels: { color: legendColor(), usePointStyle: true, pointStyle: 'line', font: { size: 11 } } };
}

const chartInstances = {};

function filterByRange(series, hours) {
  const cutoff = Date.now() - hours * 3600 * 1000;
  return series.filter(p => new Date(p.t).getTime() >= cutoff);
}

function hexToRgba(hex, alpha) {
  const h = hex.replace('#', '');
  const bigint = parseInt(h.length === 3 ? h.split('').map(c => c + c).join('') : h, 16);
  const r = (bigint >> 16) & 255, g = (bigint >> 8) & 255, b = bigint & 255;
  return `rgba(${r},${g},${b},${alpha})`;
}

// Draw horizontal reference lines (the good / high thresholds) across a chart
// via a tiny inline plugin — so "what's normal" is visible in the trend, not
// just the KPI cards. Avoids the annotation plugin (which isn't vendored, so
// it would fail offline — exactly when this dashboard matters most).
function refLines(lines) {
  return {
    id: 'refLines',
    afterDatasetsDraw(chart) {
      const y = chart.scales.y;
      if (!y) return;
      const { ctx, chartArea } = chart;
      ctx.save();
      ctx.font = '9px ' + (cssVar('--font-mono') || 'ui-monospace, monospace');
      lines.forEach(L => {
        if (L.value == null || isNaN(L.value)) return;
        const py = y.getPixelForValue(L.value);
        if (py < chartArea.top || py > chartArea.bottom) return;   // off-scale
        ctx.beginPath();
        ctx.moveTo(chartArea.left, py);
        ctx.lineTo(chartArea.right, py);
        ctx.lineWidth = 1;
        ctx.setLineDash([4, 4]);
        ctx.globalAlpha = 0.45;
        ctx.strokeStyle = L.color;
        ctx.stroke();
        if (L.label) {
          ctx.setLineDash([]);
          ctx.globalAlpha = 0.9;
          ctx.fillStyle = L.color;
          ctx.textAlign = 'right';
          ctx.textBaseline = 'bottom';
          ctx.fillText(L.label, chartArea.right - 5, py - 2);
        }
      });
      ctx.restore();
    },
  };
}

let currentRangeHours = 168;

function renderLatencyChart() {
  const series = filterByRange(DATA.latency_series, currentRangeHours);
  const jitter = filterByRange(DATA.jitter_series || [], currentRangeHours);
  const dns = filterByRange(DATA.dns_series || [], currentRangeHours);
  const blue = catColor(0), aqua = catColor(4), violet = catColor(6);
  const ctx = document.getElementById('latencyChart');
  if (chartInstances.latency) chartInstances.latency.destroy();
  const lineBase = {
    fill: false, borderWidth: 1.5, pointRadius: 0, pointHoverRadius: 4,
    tension: 0.2, spanGaps: false,
  };
  const datasets = [{
    label: 'Avg latency',
    data: series.map(p => p.v),
    borderColor: blue,
    backgroundColor: hexToRgba(blue, 0.10),
    fill: true,
    borderWidth: 2,
    pointRadius: 0,
    pointHoverRadius: 5,
    pointHoverBackgroundColor: blue,
    pointHoverBorderColor: surfaceColor(),
    pointHoverBorderWidth: 2,
    tension: 0.2,
    spanGaps: false,
  }];
  if (jitter.some(p => p.v != null)) {
    datasets.push({ ...lineBase, label: 'Jitter', data: jitter.map(p => p.v), borderColor: aqua, backgroundColor: aqua });
  }
  if (dns.some(p => p.v != null)) {
    datasets.push({ ...lineBase, label: 'DNS lookup', data: dns.map(p => p.v), borderColor: violet, backgroundColor: violet });
  }
  chartInstances.latency = new Chart(ctx, {
    type: 'line',
    data: { labels: series.map(p => new Date(p.t)), datasets },
    plugins: [ refLines([
      { value: THRESHOLDS.latency.fair, color: cssVar('--status-critical'), label: 'high ' + THRESHOLDS.latency.fair + 'ms' },
      { value: THRESHOLDS.latency.good, color: cssVar('--status-good'), label: 'good ' + THRESHOLDS.latency.good + 'ms' },
    ]) ],
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      scales: { x: timeScale(), y: yScale('ms') },
      plugins: {
        legend: legendOpts(datasets.length > 1),
        tooltip: { ...tooltipBase(), callbacks: { label: (c) => c.dataset.label + ': ' + (c.parsed.y == null ? 'no data' : c.parsed.y + ' ms') } },
      },
    },
  });
}

function renderLossChart() {
  const series = filterByRange(DATA.loss_series, currentRangeHours);
  const orange = catColor(5);
  const ctx = document.getElementById('lossChart');
  if (chartInstances.loss) chartInstances.loss.destroy();
  chartInstances.loss = new Chart(ctx, {
    type: 'line',
    plugins: [ refLines([
      { value: THRESHOLDS.loss.fair, color: cssVar('--status-critical'), label: 'high ' + THRESHOLDS.loss.fair + '%' },
      { value: THRESHOLDS.loss.good, color: cssVar('--status-good'), label: 'good ' + THRESHOLDS.loss.good + '%' },
    ]) ],
    data: {
      labels: series.map(p => new Date(p.t)),
      datasets: [{
        label: 'Packet loss',
        data: series.map(p => p.v),
        borderColor: orange,
        backgroundColor: hexToRgba(orange, 0.10),
        fill: true,
        borderWidth: 2,
        pointRadius: 0,
        pointHoverRadius: 5,
        pointHoverBackgroundColor: orange,
        pointHoverBorderColor: surfaceColor(),
        pointHoverBorderWidth: 2,
        tension: 0.2,
        spanGaps: false,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      scales: { x: timeScale(), y: yScale('%', { suggestedMax: 10 }) },
      plugins: {
        legend: legendOpts(false),
        tooltip: { ...tooltipBase(), callbacks: { label: (c) => (c.parsed.y == null ? 'no data' : c.parsed.y + '% loss') } },
      },
    },
  });
}

// ---------- per-router latency chart ----------
function renderRoutersChart() {
  const rc = DATA.routers_chart;
  const names = rc ? Object.keys(rc.series) : [];
  const canvas = document.getElementById('routersChart');
  if (!rc || !rc.buckets.length || names.length === 0) {
    canvas.style.display = 'none';
    document.getElementById('routersChartEmpty').style.display = 'block';
    return;
  }
  const cutoff = Date.now() - currentRangeHours * 3600 * 1000;
  const keep = rc.buckets.map(t => new Date(t).getTime() >= cutoff);
  const labels = rc.buckets.filter((_, i) => keep[i]).map(t => new Date(t));
  const datasets = names.map((name, i) => {
    const col = catColor(i);
    return {
      label: name,
      data: rc.series[name].filter((_, j) => keep[j]),
      borderColor: col,
      backgroundColor: col,
      borderWidth: 1.5,
      pointRadius: 0,
      pointHoverRadius: 4,
      tension: 0.2,
      spanGaps: false,
    };
  });
  if (chartInstances.routers) chartInstances.routers.destroy();
  chartInstances.routers = new Chart(canvas, {
    type: 'line',
    data: { labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      scales: { x: timeScale(), y: yScale('ms') },
      plugins: {
        legend: legendOpts(true),
        tooltip: { ...tooltipBase(), callbacks: { label: (c) => c.dataset.label + ': ' + (c.parsed.y == null ? 'no data' : c.parsed.y + ' ms') } },
      },
    },
  });
}

// One shared 24h/7d range drives every time-series chart. The same toggle
// appears in several section headers; clicking any of them updates them all
// and re-renders every range-aware chart in sync.
function syncRangeToggles() {
  document.querySelectorAll('[data-rangetoggle] button').forEach(b =>
    b.classList.toggle('active', parseInt(b.dataset.range, 10) === currentRangeHours));
}
function applyRange(hours) {
  currentRangeHours = hours;
  syncRangeToggles();
  safely('range charts', function() {
    renderLatencyChart();
    renderLossChart();
    renderRoutersChart();
    renderSpeedChart();
    renderWifiChart();
    renderDevCountChart();
  });
}
document.querySelectorAll('[data-rangetoggle]').forEach(tg => {
  tg.addEventListener('click', (e) => {
    const btn = e.target.closest('button');
    if (btn) applyRange(parseInt(btn.dataset.range, 10));
  });
});
syncRangeToggles();  // reflect the default range on every toggle at load

// ---------- speed test chart (range-aware) ----------
function rangeWord() { return currentRangeHours <= 24 ? '24 hours' : '7 days'; }

function renderSpeedChart() {
  const canvas = document.getElementById('speedChart');
  const emptyEl = document.getElementById('speedEmpty');
  const showEmpty = (msg) => {
    if (chartInstances.speed) { chartInstances.speed.destroy(); chartInstances.speed = null; }
    canvas.style.display = 'none'; emptyEl.style.display = 'block'; emptyEl.textContent = msg;
  };
  if (!DATA.speed_series || DATA.speed_series.length === 0) {
    return showEmpty('No speed test data yet — install a speed test tool (see README) and it will appear here automatically.');
  }
  const series = filterByRange(DATA.speed_series, currentRangeHours);
  if (series.length === 0) return showEmpty('No speed tests in the last ' + rangeWord() + '.');
  canvas.style.display = ''; emptyEl.style.display = 'none';
  const blue = catColor(0), aqua = catColor(4);
  if (chartInstances.speed) chartInstances.speed.destroy();
  const plan = DATA.plan || {};
  chartInstances.speed = new Chart(canvas, {
    type: 'line',
    plugins: [ refLines([
      { value: plan.down_mbps != null ? plan.down_mbps : null, color: cssVar('--cat-1'), label: 'plan ↓ ' + plan.down_mbps },
      { value: plan.up_mbps != null ? plan.up_mbps : null, color: cssVar('--cat-5'), label: 'plan ↑ ' + plan.up_mbps },
    ]) ],
    data: {
      labels: series.map(p => new Date(p.t)),
      datasets: [
        { label: 'Download (Mbps)', data: series.map(p => p.down), borderColor: blue, backgroundColor: blue, borderWidth: 2, pointRadius: 3, pointHoverRadius: 6, pointBackgroundColor: blue, pointBorderColor: surfaceColor(), pointBorderWidth: 2, tension: 0.2 },
        { label: 'Upload (Mbps)', data: series.map(p => p.up), borderColor: aqua, backgroundColor: aqua, borderWidth: 2, pointRadius: 3, pointHoverRadius: 6, pointBackgroundColor: aqua, pointBorderColor: surfaceColor(), pointBorderWidth: 2, tension: 0.2 },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      scales: { x: timeScale(), y: yScale('Mbps') },
      plugins: { legend: legendOpts(true), tooltip: tooltipBase() },
    },
  });
}

// ---------- wifi chart (range-aware) ----------
function renderWifiChart() {
  const canvas = document.getElementById('wifiChart');
  const emptyEl = document.getElementById('wifiEmpty');
  const showEmpty = (msg) => {
    if (chartInstances.wifi) { chartInstances.wifi.destroy(); chartInstances.wifi = null; }
    canvas.style.display = 'none'; emptyEl.style.display = 'block'; emptyEl.textContent = msg;
  };
  if (!DATA.wifi_series || DATA.wifi_series.length === 0) return showEmpty('No Wi-Fi signal data yet.');
  const series = filterByRange(DATA.wifi_series, currentRangeHours);
  if (series.length === 0) return showEmpty('No Wi-Fi data in the last ' + rangeWord() + '.');
  canvas.style.display = ''; emptyEl.style.display = 'none';
  const orange = catColor(5);
  if (chartInstances.wifi) chartInstances.wifi.destroy();
  chartInstances.wifi = new Chart(canvas, {
    type: 'line',
    plugins: [ refLines([
      { value: THRESHOLDS.wifi.good, color: cssVar('--status-good'), label: 'strong ' + THRESHOLDS.wifi.good },
      { value: THRESHOLDS.wifi.fair, color: cssVar('--status-critical'), label: 'weak ' + THRESHOLDS.wifi.fair },
    ]) ],
    data: {
      labels: series.map(p => new Date(p.t)),
      datasets: [
        { label: 'Signal (dBm)', data: series.map(p => p.rssi), borderColor: orange, backgroundColor: hexToRgba(orange, 0.10), fill: true, borderWidth: 2, pointRadius: 0, pointHoverRadius: 5, pointHoverBackgroundColor: orange, pointHoverBorderColor: surfaceColor(), pointHoverBorderWidth: 2, tension: 0.2 },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      scales: { x: timeScale(), y: yScale('dBm (higher is better)', { beginAtZero: false }) },
      plugins: { legend: legendOpts(false), tooltip: tooltipBase() },
    },
  });
}

// ---------- devices online over time (range-aware) ----------
function renderDevCountChart() {
  const canvas = document.getElementById('devCountChart');
  const emptyEl = document.getElementById('devCountEmpty');
  const all = DATA.device_count_series || [];
  const showEmpty = (msg) => {
    if (chartInstances.devCount) { chartInstances.devCount.destroy(); chartInstances.devCount = null; }
    canvas.style.display = 'none'; emptyEl.style.display = 'block'; emptyEl.textContent = msg;
  };
  if (all.length < 2) return showEmpty('No scan history yet.');
  const series = filterByRange(all, currentRangeHours);
  if (series.length < 2) return showEmpty('Not enough scans in the last ' + rangeWord() + '.');
  canvas.style.display = ''; emptyEl.style.display = 'none';
  const blue = catColor(0);
  if (chartInstances.devCount) chartInstances.devCount.destroy();
  chartInstances.devCount = new Chart(canvas, {
    type: 'line',
    data: {
      labels: series.map(p => new Date(p.t)),
      datasets: [{
        label: 'Devices',
        data: series.map(p => p.v),
        borderColor: blue,
        backgroundColor: hexToRgba(blue, 0.10),
        fill: true,
        stepped: true,
        borderWidth: 2,
        pointRadius: 0,
        pointHoverRadius: 4,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      scales: { x: timeScale(), y: yScale('devices', { ticks: Object.assign(baseTicks(), { precision: 0 }) }) },
      plugins: {
        legend: legendOpts(false),
        tooltip: { ...tooltipBase(), callbacks: { label: (c) => c.parsed.y + ' devices online' } },
      },
    },
  });
}

function rerenderCharts() {
  if (typeof Chart === 'undefined') return;
  Chart.defaults.font.family = 'system-ui, -apple-system, "Segoe UI", sans-serif';
  safely('sparkline', renderSparkline);
  safely('latency chart', renderLatencyChart);
  safely('loss chart', renderLossChart);
  safely('router latency chart', renderRoutersChart);
  safely('speed test chart', renderSpeedChart);
  safely('wifi chart', renderWifiChart);
  safely('device count chart', renderDevCountChart);
}
window.__rerenderCharts = rerenderCharts;
rerenderCharts();

// ---------- refresh button + auto-refresh ----------
safely('refresh controls', function() {
  document.getElementById('refreshBtn').addEventListener('click', () => location.reload());

  const autoBtn = document.getElementById('autoRefreshBtn');
  let autoOn = false;
  try { autoOn = localStorage.getItem('netmon-auto-refresh') === '1'; } catch (e) {}
  let timer = null;
  function applyAuto() {
    autoBtn.classList.toggle('active', autoOn);
    autoBtn.textContent = autoOn ? 'Auto: on' : 'Auto: off';
    if (timer) { clearInterval(timer); timer = null; }
    // the file on disk is regenerated every 60s, so reloading once a
    // minute keeps the page continuously current
    if (autoOn) timer = setInterval(() => location.reload(), 60000);
  }
  autoBtn.addEventListener('click', () => {
    autoOn = !autoOn;
    try { localStorage.setItem('netmon-auto-refresh', autoOn ? '1' : '0'); } catch (e) {}
    applyAuto();
  });
  applyAuto();
});

// restore the saved theme (done last: applyTheme re-renders the charts)
safely('theme restore', function() {
  let saved = null;
  try { saved = localStorage.getItem('netmon-theme'); } catch (e) {}
  if (saved === 'light' || saved === 'dark' || saved === 'auto') applyTheme(saved);
});
</script>
</body>
</html>
""".replace("__DATA_JSON__", data_json).replace(
        "__TITLE__", html_escape(data.get("title") or "Home Network Monitor"))


if __name__ == "__main__":
    if "--version" in sys.argv:
        print(__version__)
        sys.exit(0)
    main()
