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
import shutil
import sqlite3
import sys
import time
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
# ISP evidence report: a printable page of outages/speeds for complaining
# to the ISP with receipts. Heavier queries than the dashboard, and a
# 10-minute-old evidence document is as good as a fresh one — so it only
# regenerates when its file is older than REPORT_REGEN_MIN minutes.
REPORT_OUT_PATH = os.path.join(BASE_DIR, "report.html")
REPORT_WINDOW_DAYS = 90    # matches the ping retention window
REPORT_REGEN_MIN = 10
ROUTERS_CONFIG_PATH = os.path.join(BASE_DIR, "routers.json")
DEVICE_NAMES_PATH = os.path.join(BASE_DIR, "devices.json")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

LOOKBACK_HOURS = 24 * 7  # pull a week of data; the page itself lets you toggle 24h vs 7d

# A hole in the ping timeline longer than this means the monitor wasn't
# running (Mac asleep, service stopped) — shown as "monitoring paused"
# rather than being silently invisible. Pings normally land every 15s.
MONITOR_GAP_MIN = 5

# Defensive mirror of monitor.py's INTERVAL_DEFAULTS / INTERVAL_BOUNDS
# (same no-imports rule as the schema migration lists — update together).
# Used for the cadence footers: the CONFIGURED interval is the fallback
# and the stale baseline; what the footers prefer to display is the
# MEASURED cadence from the data itself, because real cadence = sleep +
# work time (e.g. a 15s router setting yields ~30s with ARP fallbacks).
INTERVAL_DEFAULTS = {
    "ping": 15, "router": 15, "wifi": 300, "devices": 300,
    "speedtest": 1800, "public_ip": 600, "dns": 60, "iot": 30,
}
INTERVAL_BOUNDS = {
    "ping": (5, 300), "router": (10, 600), "wifi": (60, 3600),
    "devices": (60, 3600), "speedtest": (600, 24 * 3600),
    "public_ip": (120, 3600), "dns": (15, 900), "iot": (10, 600),
}


def configured_intervals(site_config):
    vals = dict(INTERVAL_DEFAULTS)
    raw = site_config.get("intervals")
    if isinstance(raw, dict):
        for k, v in raw.items():
            if k in vals and isinstance(v, (int, float)) and not isinstance(v, bool):
                lo, hi = INTERVAL_BOUNDS[k]
                vals[k] = int(min(hi, max(lo, v)))
    return vals


def median_gap(ts_list, max_gaps=7):
    """Median seconds between consecutive timestamps over the most recent
    max_gaps gaps — a check's measured cadence. None when there isn't
    enough history to be meaningful. Kept deliberately SHORT (7 gaps, not
    30): when the user changes an interval in Settings, a long window
    keeps reporting the old cadence for hours — the footer said "every
    ~5m" half a day after the scan moved to 30m. Seven gaps converge
    within ~4 cycles while a median still shrugs off a couple of slow
    outlier cycles."""
    parsed = []
    for t in ts_list[-(max_gaps + 1):]:
        try:
            parsed.append(datetime.fromisoformat(t).timestamp())
        except ValueError:
            pass
    gaps = sorted(b - a for a, b in zip(parsed, parsed[1:]) if b > a)
    if len(gaps) < 2:
        return None
    return round(gaps[len(gaps) // 2], 1)


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
        # bufferbloat + wifi columns (mirror of monitor.py's list — keep in sync)
        "ALTER TABLE speedtests ADD COLUMN jitter_ms REAL",
        "ALTER TABLE speedtests ADD COLUMN loaded_latency_down_ms REAL",
        "ALTER TABLE speedtests ADD COLUMN loaded_latency_up_ms REAL",
        "ALTER TABLE speedtests ADD COLUMN packet_loss_pct REAL",
        "ALTER TABLE wifi ADD COLUMN bssid TEXT",
        "ALTER TABLE wifi ADD COLUMN band TEXT",
        # ping bursts / per-resolver DNS / flight-recorder evidence
        "ALTER TABLE pings ADD COLUMN sent INTEGER",
        "ALTER TABLE pings ADD COLUMN received INTEGER",
        "ALTER TABLE dns_checks ADD COLUMN resolver TEXT",
        "ALTER TABLE events ADD COLUMN evidence TEXT",
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
    conn.execute(
        """CREATE TABLE IF NOT EXISTS wifi_scan (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, ssid TEXT, bssid TEXT,
            channel TEXT, band TEXT, signal_pct REAL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS topology_checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, private_hops INTEGER,
            hop_ips TEXT, double_nat INTEGER, cgnat INTEGER, error TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS blips (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, end_ts TEXT NOT NULL,
            target_class TEXT NOT NULL, failed_checks INTEGER NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS iot_pings (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, mac TEXT NOT NULL,
            name TEXT, ip TEXT, success INTEGER NOT NULL, latency_ms REAL, method TEXT
        )"""
    )
    conn.commit()

    pings = q(conn, "SELECT ts, target, target_type, success, latency_ms, sent, received FROM pings WHERE ts >= ? ORDER BY ts", (since,))
    events = q(conn, "SELECT start_ts, end_ts, kind, scope, note, router_name, evidence FROM events WHERE start_ts >= ? ORDER BY start_ts DESC", (since,))
    speedtests = q(conn, "SELECT ts, download_mbps, upload_mbps, ping_ms, error,"
                   " jitter_ms, loaded_latency_down_ms, loaded_latency_up_ms, packet_loss_pct"
                   " FROM speedtests WHERE ts >= ? ORDER BY ts", (since,))
    wifi = q(conn, "SELECT ts, ssid, rssi_dbm, noise_dbm, channel, tx_rate_mbps, band FROM wifi WHERE ts >= ? ORDER BY ts", (since,))
    router_pings = q(conn, "SELECT ts, name, ip, success, latency_ms, method FROM router_pings WHERE ts >= ? ORDER BY ts", (since,))
    dns_checks_all = q(conn, "SELECT ts, domain, success, latency_ms, resolver FROM dns_checks WHERE ts >= ? ORDER BY ts", (since,))
    # The system-resolver rows are "the DNS every app actually experiences"
    # — they alone feed the chart/24h stats so history stays comparable.
    # Direct per-resolver rows power the resolver-status line on the card.
    dns_checks = [c for c in dns_checks_all if c["resolver"] in (None, "system")]
    resolver_status = {}
    dns_cutoff_1h = iso(datetime.now(timezone.utc) - timedelta(hours=1))
    for c in dns_checks_all:
        r = c["resolver"]
        if r in (None, "system"):
            continue
        st = resolver_status.setdefault(r, {"ok": None, "ms": None, "ts": None,
                                            "fails_1h": 0, "checks_1h": 0})
        st["ok"], st["ms"], st["ts"] = bool(c["success"]), c["latency_ms"], c["ts"]
        if c["ts"] >= dns_cutoff_1h:
            st["checks_1h"] += 1
            if not c["success"]:
                st["fails_1h"] += 1
    blips = q(conn, "SELECT ts, end_ts, target_class, failed_checks FROM blips WHERE ts >= ? ORDER BY ts", (since,))

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

    # Friendly device names from devices.json. Values are a plain name
    # string or {"name", "type", "watch"} for IoT devices — this normalizer
    # mirrors monitor.py's _device_meta_from_value / IOT_TYPES (no-imports
    # rule — update together).
    IOT_TYPES = ("camera", "intercom", "printer", "light", "plug", "speaker", "tv", "other")

    def norm_mac(mac):
        parts = str(mac).strip().lower().split(":")
        if len(parts) == 6:
            return ":".join(p.zfill(2) for p in parts)
        return str(mac).strip().lower()

    device_meta = {}
    for k, v in load_json_config(DEVICE_NAMES_PATH, {}).items():
        if isinstance(v, dict):
            name = str(v.get("name") or "").strip()
            typ = str(v.get("type") or "").strip().lower() or None
            if name:
                device_meta[norm_mac(k)] = {"name": name,
                                            "type": typ if typ in IOT_TYPES else None,
                                            "watch": bool(v.get("watch"))}
        elif str(v).strip():
            device_meta[norm_mac(k)] = {"name": str(v).strip(), "type": None, "watch": False}
    for d in devices:
        m = device_meta.get(norm_mac(d["mac"]))
        d["name"] = m["name"] if m else None
        d["type"] = m["type"] if m else None

    # IoT section: every typed or watched devices.json entry. Watched ones
    # carry live liveness from iot_pings; the rest ride the device-scan
    # census (online/away + last seen). watch-without-type groups as
    # 'other' client-side.
    iot_rows = q(conn, "SELECT ts, mac, name, ip, success, latency_ms, method FROM iot_pings WHERE ts >= ? ORDER BY ts", (since,))
    iot_last = {}          # mac -> latest iot_pings row
    iot_ts_by_mac = {}     # mac -> [ts, ...] for the measured cadence
    for r in iot_rows:
        iot_last[r["mac"]] = r
        iot_ts_by_mac.setdefault(r["mac"], []).append(r["ts"])
    dev_by_mac = {norm_mac(d["mac"]): d for d in devices}
    iot_devices = []
    for mac, m in device_meta.items():
        if not m["type"] and not m["watch"]:
            continue
        dv = dev_by_mac.get(mac)
        last = iot_last.get(mac)
        iot_devices.append({
            "mac": mac, "name": m["name"], "type": m["type"], "watch": m["watch"],
            "ip": (last["ip"] if last else None) or (dv["ip"] if dv else None),
            # watched liveness (None/'never' until the first probe lands)
            "status": (("up" if last["success"] else "down") if last else "never") if m["watch"] else None,
            "latency": last["latency_ms"] if last else None,
            "method": last["method"] if last else None,
            "last_check": last["ts"] if last else None,
            "measured": median_gap(iot_ts_by_mac.get(mac, [])),
            # scan-derived presence (used for unwatched rows)
            "online": dv["online"] if dv else None,
            "last_seen": dv["last_seen"] if dv else None,
        })
    # type order first (the client renders one group per type, in this
    # order), watched rows before unwatched within a group, then name
    type_rank = {t: i for i, t in enumerate(IOT_TYPES)}
    iot_devices.sort(key=lambda x: (type_rank.get(x["type"] or "other", 99), not x["watch"], x["name"].lower()))
    iot_last_check = max((r["ts"] for r in iot_rows), default=None)

    # ISP evidence report: throttled, and a failure here must never take the
    # dashboard down with it — the report is a bonus artifact.
    try:
        report_stale = (not os.path.exists(REPORT_OUT_PATH)
                        or (time.time() - os.path.getmtime(REPORT_OUT_PATH)) > REPORT_REGEN_MIN * 60)
        if report_stale:
            generate_report(conn, site_config)
    except Exception as e:
        print(f"report generation failed (dashboard unaffected): {e}")

    # Newest topology check (double-NAT verdict for the map note).
    try:
        topo_rows = q(conn, "SELECT ts, private_hops, hop_ips, double_nat, cgnat FROM topology_checks"
                      " ORDER BY id DESC LIMIT 1")
        topology = topo_rows[0] if topo_rows else None
    except sqlite3.OperationalError:
        topology = None

    public_ip_rows = q(conn, "SELECT ts, ip FROM public_ip WHERE ts >= ? AND ip IS NOT NULL ORDER BY ts", (since,))
    # Health of the public-IP checks themselves over the last 24h: repeated
    # fetch failures usually mean brief ISP drops between ping samples —
    # worth a hint even though no outage event fired.
    ip_health = q(
        conn,
        "SELECT COUNT(*) AS checks, SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) AS failures"
        " FROM public_ip WHERE ts >= ?",
        (iso(datetime.now(timezone.utc) - timedelta(hours=24)),),
    )[0]
    # Last public-IP check ATTEMPT (including failed fetches) — the cadence
    # footer should show when the check ran, not when it last succeeded.
    ip_last_row = q(conn, "SELECT MAX(ts) AS ts FROM public_ip")[0]
    ip_last_check = ip_last_row["ts"] if ip_last_row else None

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
            "last_check": g["ts"],
            "uptime_pct": round(100.0 * len(g24_ok) / len(g24), 2) if g24 else None,
            "avg_latency": round(sum(g24_lat) / len(g24_lat), 1) if g24_lat else None,
        }

    # Per-PACKET loss when the row has burst counts (sent/received), falling
    # back to whole-check granularity for pre-burst history. A check that
    # lost 1 packet of 3 used to be indistinguishable from a perfect one.
    def pkt_counts(p):
        if p.get("sent"):
            return p["sent"], (p["received"] or 0)
        return 1, (1 if p["success"] else 0)

    def window_stats(hours, offset_hours=0):
        end = now - timedelta(hours=offset_hours)
        start = end - timedelta(hours=hours)
        w = [p for p in external if start.isoformat() <= p["ts"] <= end.isoformat()]
        if not w:
            return {"uptime_pct": None, "avg_latency": None, "loss_pct": None, "count": 0}
        successes = [p for p in w if p["success"]]
        latencies = [p["latency_ms"] for p in successes if p["latency_ms"] is not None]
        # uptime = check-level reachability ("could we reach the internet");
        # loss = packet-level (burst rows), the number that explains stutter
        w_sent = w_recv = 0
        for p in w:
            s, r = pkt_counts(p)
            w_sent += s
            w_recv += r
        return {
            "uptime_pct": round(100.0 * len(successes) / len(w), 2),
            "avg_latency": round(sum(latencies) / len(latencies), 1) if latencies else None,
            "loss_pct": round(100.0 * (1 - w_recv / w_sent), 2) if w_sent else None,
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
    instability_events = [e for e in events if e["kind"] == "instability"]
    ip_change_events = [e for e in events if e["kind"] == "ip_change"]
    new_device_events = [e for e in events if e["kind"] == "new_device"]
    wifi_roam_events = [e for e in events if e["kind"] == "wifi_roam"]

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
            router_meta[rname] = {"floor": (item.get("floor") or "").strip() or None, "order": idx,
                                  # role 'isp' marks the ISP modem/ONT: monitored like any
                                  # router but drawn on the house WALL, not a floor
                                  "role": (item.get("role") or "").strip() or None}

    # Deleting a router in Settings must actually remove it from the
    # dashboard: without this filter, any router pinged in the last 7 days
    # kept reappearing from history (map, pills, per-router chart) and the
    # map nagged "pick its floor" for a router the user just deleted. When
    # routers.json is missing/empty, fall back to history-derived names so
    # a config-less install still shows what it monitors. Historical
    # events in the outage log are unaffected — that's the record.
    if router_meta:
        router_names = [n for n in router_names if n in router_meta]
    # Combo-box installs: a routers.json entry — typically the role='isp'
    # Internet box — can BE the default gateway (the ISP modem-router
    # doubling as the house router). The ping thread already monitors that
    # device as the Main Router, so keeping it here would draw one device
    # twice on the map (wall box + main pill, with a WAN link from the
    # device to itself) and duplicate its chart line. Drop it and carry
    # the ISP box's name on the gateway node instead ("isp_name" — the JS
    # shows it on the Main Router card and the diagnosis banner swaps to
    # ISP-flavored wording). monitor.py's router_loop skips pinging such
    # entries for the same reason; router_pings rows for them are just the
    # pre-skip history tail. Matched on the CONFIGURED IP, not the last
    # ping row's — the config is current truth once the monitor stops
    # refreshing those rows.
    if gateway_info:
        merged = [(item.get("name") or "").strip() for item in router_config
                  if (item.get("name") or "").strip()
                  and str(item.get("ip") or "").strip() == gateway_info["ip"]]
        if merged:
            router_names = [n for n in router_names if n not in merged]
            isp_named = next((n for n in merged
                              if router_meta.get(n, {}).get("role") == "isp"), None)
            if isp_named:
                gateway_info["isp_name"] = isp_named
    router_summary = []
    for name in router_names:
        rows_for_router = [p for p in router_pings if p["name"] == name]
        latest_r = rows_for_router[-1]
        stats24 = router_window_stats(name, 24)
        meta = router_meta.get(name, {})
        router_summary.append({
            "name": name,
            "floor": meta.get("floor"),
            "role": meta.get("role"),
            "ip": latest_r["ip"],
            "status": "up" if latest_r["success"] else "down",
            "latency": latest_r["latency_ms"],
            "method": latest_r.get("method"),
            "last_check": latest_r["ts"],
            "measured": median_gap([p["ts"] for p in rows_for_router]),
            "uptime_pct": stats24["uptime_pct"],
            "avg_latency": stats24["avg_latency"],
        })
    # Sort by routers.json order (user-controlled, naturally groups floors
    # if the file is arranged that way).
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
            if r.get("role") == "isp":
                continue   # the wall box is not on a floor
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
        b = buckets.setdefault(k, {"latencies": [], "total": 0, "success": 0,
                                   "sent": 0, "recv": 0, "by_target": {}})
        b["total"] += 1
        s, r = pkt_counts(p)
        b["sent"] += s
        b["recv"] += r
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

    # per-custom-target latency buckets (config custom_targets; pings rows
    # carry target_type='custom' keyed by the target NAME)
    custom_rows = [p for p in pings if p["target_type"] == "custom"]
    target_buckets = {}
    target_names = []
    for p in custom_rows:
        if p["target"] not in target_names:
            target_names.append(p["target"])
        if p["success"] and p["latency_ms"] is not None:
            target_buckets.setdefault((p["target"], bucket_key(p["ts"])), []).append(p["latency_ms"])

    # Build a *complete* 5-min bucket axis over the whole span, including
    # empty buckets as None — so monitoring gaps show as visible breaks in
    # the charts instead of the line quietly connecting across them.
    all_keys = (set(buckets.keys()) | {k for (_n, k) in router_buckets}
                | {k for (_n, k) in target_buckets} | set(dns_buckets.keys()))
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
        loss = round(100.0 * (1 - b["recv"] / b["sent"]), 1) if b and b["sent"] else None
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
    targets_chart = {
        "buckets": full_keys,
        "series": {
            name: [
                (round(sum(target_buckets[(name, k)]) / len(target_buckets[(name, k)]), 1)
                 if (name, k) in target_buckets else None)
                for k in full_keys
            ]
            for name in target_names
        },
    }

    sparkline = [p["v"] for p in latency_series[-12:] if p["v"] is not None]

    speed_series = [s for s in speedtests if s.get("download_mbps") is not None]
    # Failed speed-test runs, EXCLUDING the standing "no tool installed"
    # message — that's an empty-state, not an incident, and would paint a
    # mark every 30 minutes forever.
    speed_failures = [
        {"t": s["ts"], "error": (s["error"] or "")[:160]}
        for s in speedtests
        if s.get("download_mbps") is None and s.get("error")
        and "no speed test tool installed" not in s["error"]
    ]

    def parse_evidence(e):
        """events.evidence is JSON written by the monitor's flight recorder;
        pass it through parsed (or None) so the JS never eval's anything."""
        raw = e.get("evidence")
        if not raw:
            return None
        try:
            ev = json.loads(raw)
            return ev if isinstance(ev, dict) and "skipped" not in ev else None
        except (ValueError, TypeError):
            return None

    blips_cutoff_24h = iso(now - timedelta(hours=24))
    blips_24h = sum(1 for b in blips if b["ts"] >= blips_cutoff_24h)

    data = {
        "generated_at": now.isoformat(),
        "version": __version__,
        "default_range_hours": site_config.get("default_range_hours"),
        "update": check_for_update(site_config),
        "current_status": current_status,
        "current_latency": latest["latency_ms"] if latest else None,
        "stats_24h": stats_24h,
        "stats_7d": stats_7d,
        "deltas_24h": deltas_24h,
        "sparkline": sparkline,
        "outage_events": [
            {"start": e["start_ts"], "end": e["end_ts"], "scope": e["scope"], "note": e["note"],
             "router_name": e.get("router_name"), "evidence": parse_evidence(e),
             "duration": fmt_duration(e["start_ts"], e["end_ts"]), "ongoing": e["end_ts"] is None}
            for e in outage_events
        ],
        "degraded_events": [
            {"start": e["start_ts"], "end": e["end_ts"], "note": e["note"],
             "evidence": parse_evidence(e),
             "duration": fmt_duration(e["start_ts"], e["end_ts"]), "ongoing": e["end_ts"] is None}
            for e in degraded_events
        ],
        "instability_events": [
            {"start": e["start_ts"], "end": e["end_ts"], "note": e["note"],
             "duration": fmt_duration(e["start_ts"], e["end_ts"]), "ongoing": e["end_ts"] is None}
            for e in instability_events
        ],
        # micro-outages: too short for the events log, charted as timeline
        # ticks + a 24h counter (their absence is itself a good sign)
        "blips": [{"t": b["ts"], "end": b["end_ts"], "cls": b["target_class"],
                   "checks": b["failed_checks"]} for b in blips],
        "blips_24h": blips_24h,
        "resolver_status": resolver_status,
        "ip_change_events": [
            {"start": e["start_ts"], "note": e["note"]}
            for e in ip_change_events
        ],
        "new_device_events": [
            {"start": e["start_ts"], "note": e["note"]}
            for e in new_device_events
        ],
        "wifi_roam_events": [
            {"start": e["start_ts"], "note": e["note"]}
            for e in wifi_roam_events
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
        # which router/AP the monitor PC hangs off (config.json
        # "monitor_location", set in Settings → General): speed tests and
        # latency measure THAT path, and the dashboard should say so
        "monitor_location": (site_config.get("monitor_location") or "").strip() or None,
        "latency_series": latency_series,
        "loss_series": loss_series,
        "jitter_series": jitter_series,
        "dns_series": dns_series,
        "jitter_24h": jitter_24h,
        "dns_24h": dns_24h,
        "device_count_series": device_count_series,
        "routers_chart": routers_chart,
        "targets_chart": targets_chart,
        "speed_series": [{"t": s["ts"], "down": s["download_mbps"], "up": s["upload_mbps"], "ping": s["ping_ms"],
                          "jitter": s.get("jitter_ms"), "lat_down": s.get("loaded_latency_down_ms"),
                          "lat_up": s.get("loaded_latency_up_ms"), "ploss": s.get("packet_loss_pct")}
                         for s in speed_series],
        "speed_failures": speed_failures,
        "public_ip_health": {"checks": ip_health.get("checks") or 0, "failures": ip_health.get("failures") or 0},
        "topology": topology,
        "devices": devices,
        "last_device_scan_ts": last_scan_ts,
        "iot_devices": iot_devices,
        # Per-source "when did this last run" timestamps for the cadence
        # footers (command · age · frequency on every card). Frequencies are
        # hardcoded in the JS to mirror monitor.py's *_INTERVAL_SEC constants.
        # speed uses the last ATTEMPT (failed runs included) — the footer
        # answers "is the check running", not "did it succeed".
        "checks": {
            "ping": latest["ts"] if latest else None,
            "dns": dns_checks[-1]["ts"] if dns_checks else None,
            "speed": speedtests[-1]["ts"] if speedtests else None,
            "wifi": wifi[-1]["ts"] if wifi else None,
            "public_ip": ip_last_check,
            "devices": last_scan_ts,
            "iot": iot_last_check,
            # which sweep the device scan actually uses (monitor.py checks
            # nmap every cycle; same PATH here, so this matches in practice)
            "device_cmd": "nmap sweep + arp" if shutil.which("nmap") else "ping sweep + arp",
            "wifi_cmd": "netsh wlan" if sys.platform == "win32" else "system_profiler",
            # arp-tier evidence: on Windows the monitor reads the neighbor
            # STATE (fresh every router cycle); elsewhere it's the lingering
            # cache, refreshed by the device sweep — footers must not claim
            # freshness the platform can't deliver
            "arp_cmd": "arp state" if sys.platform == "win32" else "arp cache",
            # configured cadence (config.json "intervals" over defaults) vs
            # what the data actually shows — the footers prefer measured
            "freq": configured_intervals(site_config),
            "measured": {
                "ping": median_gap([p["ts"] for p in gateway_pings]),
                "dns": median_gap([c["ts"] for c in dns_checks]),
                "speed": median_gap([s["ts"] for s in speedtests]),
                "wifi": median_gap([w["ts"] for w in wifi]),
                "public_ip": median_gap([r["ts"] for r in public_ip_rows]),
                "devices": median_gap([d["t"] for d in device_count_series]),
                # one shared footer for the section: cadence of the whole
                # pass, i.e. any watched device's samples
                "iot": median_gap([r["ts"] for r in iot_rows if r["mac"] == iot_rows[-1]["mac"]]) if iot_rows else None,
            },
        },
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
    --font-mono: ui-monospace, "SF Mono", SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
    --cat-1: #2a78d6; --cat-2: #008300; --cat-3: #e87ba4; --cat-4: #eda100;
    --cat-5: #1baf7a; --cat-6: #eb6834; --cat-7: #4a3aa7; --cat-8: #e34948;
    --series-blue: #2a78d6;
    --series-green: #008300;
    --series-orange: #eb6834;
    --status-good: #0ca30c;
    --status-good-bg: rgba(12,163,12,0.10);
    --status-silent: #518f68;   /* muted sage: up, but only via the ARP cache */
    --glow-silent: rgba(81,143,104,0.22);
    --status-warning: #fab219;
    --status-warning-bg: rgba(250,178,25,0.15);
    --status-serious: #c94e1d;   /* vermilion: kept 25°+ of hue from amber --status-warning */
    --status-serious-bg: rgba(201,78,29,0.10);
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
      --cat-1: #3987e5; --cat-2: #008300; --cat-3: #d55181; --cat-4: #c98500;
      --cat-5: #199e70; --cat-6: #d95926; --cat-7: #9085e9; --cat-8: #e66767;
      --series-blue: #3987e5;
      --series-green: #008300;
      --series-orange: #d95926;
      --status-good: #0ca30c;
      --status-good-bg: rgba(12,163,12,0.14);
      --status-silent: #63a97e;   /* muted sage: up, but only via the ARP cache */
      --glow-silent: rgba(99,169,126,0.28);
      --status-warning: #fab219;
      --status-warning-bg: rgba(250,178,25,0.12);
      --status-serious: #f4703c;   /* vermilion: kept 25°+ of hue from amber --status-warning */
      --status-serious-bg: rgba(244,112,60,0.14);
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
    --cat-1: #3987e5; --cat-2: #008300; --cat-3: #d55181; --cat-4: #c98500;
    --cat-5: #199e70; --cat-6: #d95926; --cat-7: #9085e9; --cat-8: #e66767;
    --series-blue: #3987e5;
    --series-green: #008300;
    --series-orange: #d95926;
    --status-good: #0ca30c;
    --status-good-bg: rgba(12,163,12,0.14);
    --status-warning: #fab219;
    --status-warning-bg: rgba(250,178,25,0.12);
    --status-serious: #f4703c;   /* vermilion: kept 25°+ of hue from amber --status-warning */
    --status-serious-bg: rgba(244,112,60,0.14);
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
  /* No visible scrollbars ANYWHERE (scrolling still works with wheel,
     touch, and keyboard), and nothing may push the page wider than the
     window. Applied to every element because different browsers hang the
     page scrollbar on different elements (html vs body). */
  * { scrollbar-width: none !important; -ms-overflow-style: none !important; }
  *::-webkit-scrollbar { display: none !important; width: 0 !important; height: 0 !important; }
  html, body { overflow-x: hidden; }
  .chart-box > canvas { max-width: 100%; }
  #devicesTableWrap, #outagesTableWrap { overflow-x: auto; }
  /* Expanded event/device lists scroll inside a capped box instead of
     growing the page by 30+ rows. Scrollbars are hidden globally, so the
     toggle button below the box stays the visible affordance. */
  .list-scroll.expanded { max-height: 55vh; overflow-y: auto; }
  body::after { content:""; position:fixed; left:0; right:0; top:0; height:220px; pointer-events:none; z-index:0;
    background: radial-gradient(60% 100% at 50% 0%, var(--accent-soft), transparent 55%); }
  .topline { position:fixed; top:0; left:0; right:0; height:2px; z-index:5;
    background: linear-gradient(90deg, transparent, var(--accent) 25%, var(--accent) 75%, transparent); opacity:.7; }
  /* slim jump-to-section bar; slides in once the deck is scrolled past */
  .quick-nav { position:fixed; top:10px; left:50%; transform:translate(-50%,-260%); z-index:6;
    display:flex; gap:2px; background: var(--surface-1); border:1px solid var(--border);
    border-radius:999px; padding:4px; box-shadow: var(--shadow); transition: transform .25s ease; }
  .quick-nav.show { transform:translate(-50%,0); }
  .quick-nav button { border:none; background:transparent; color: var(--muted); font-size:12px; font-weight:600;
    padding: 5px 13px; border-radius:999px; cursor:pointer; transition: color .12s ease, background .12s ease; }
  .quick-nav button:hover { color: var(--accent); background: var(--accent-soft); }
  /* always-visible flavor: sits in-flow under the topbar; the fixed bar
     above still slides in once this one scrolls out of view */
  .quick-nav-static { position:static; transform:none; justify-content:center;
    margin: -8px 0 18px; flex-wrap:wrap; }
  /* live filter box over the devices table */
  .search-box { background: var(--surface-1); border:1px solid var(--border); border-radius:8px;
    color: var(--text-primary); font-size:12.5px; padding:6px 11px; width:190px; outline:none;
    font-family: inherit; }
  .search-box:focus { border-color: var(--accent); }
  .search-box::placeholder { color: var(--muted); }
  /* first-seen-in-24h marker in the devices table */
  .dev-new { display:inline-block; margin-left:7px; padding:1px 6px; border-radius:5px; font-size:9.5px;
    font-weight:700; font-family: var(--font-mono); letter-spacing:.08em; text-transform:uppercase;
    color: var(--status-good); background: var(--status-good-bg); vertical-align:1px; }
  /* 1560 (was 1220): on modern wide monitors the old cap left a third of
     the screen empty and crushed the 3-up chart row to ~370px each.
     Content is still capped — full-bleed dashboards get unreadable. */
  .wrap { padding: 34px 24px 60px; max-width: 1560px; margin: 0 auto; position:relative; z-index:1; }

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
  h1 { font-size: 20px; margin: 0 0 5px 0; letter-spacing: -.01em; font-weight: 650; }
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
  .theme-toggle button { border:none; background:transparent; color: var(--muted); font-size:12px; font-weight:600;
    padding: 6px 11px; border-radius: 7px; cursor:pointer;
    transition: color .12s ease, background .12s ease; }
  .theme-toggle button:hover { color: var(--text-primary); }
  .theme-toggle button.active { background: var(--accent-soft); color: var(--accent); box-shadow: inset 0 0 0 1px var(--accent-glow); }
  #refreshCtl button { display:inline-flex; align-items:center; gap:5px; }
  #refreshCtl button.active { background: var(--status-good-bg); color: var(--status-good); box-shadow: inset 0 0 0 1px var(--glow-good); }
  #settingsLink { background: var(--surface-1); border:1px solid var(--border); border-radius:10px;
    box-shadow: var(--shadow); color: var(--muted); font-size:12px; font-weight:600;
    padding: 10px 14px; cursor:pointer;
    text-decoration:none; transition: color .12s ease; }
  #settingsLink:hover { color: var(--accent); }
  /* right-hand tool cluster: wraps as a unit; the transient test-now
     result gets its own full row instead of shoving the theme toggle
     into an awkward mid-row wrap when it appears */
  .topbar-tools { display:flex; gap:10px; row-gap:8px; align-items:center; flex-wrap:wrap;
    justify-content:flex-end; margin-left:auto; }
  #testNowResult { flex-basis:100%; text-align:right; order:9; }
  @media (max-width: 480px) {
    .topbar-tools { justify-content:flex-start; margin-left:0; }
    #testNowResult { text-align:left; }
  }

  /* the "command deck": house map front and center, stat cards flanking
     it left/right on wide screens. Below 1200px it degrades to map first,
     then cards in the old auto-fit grid (source order = map first). */
  .deck { display: grid; grid-template-columns: repeat(auto-fit, minmax(215px, 1fr)); gap: 14px;
    margin-bottom: 12px; /* breathing room before the per-router chart */ }
  .deck-map { grid-column: 1 / -1; }
  /* flex-column cards so the cadence footer pins to the bottom when the
     side columns stretch to match the map's height */
  .deck .card { display: flex; flex-direction: column; }
  .deck .card .check-foot { margin-top: auto; padding-top: 7px; }
  @media (min-width: 1200px) {
    /* dense: without it the auto-placement cursor, having filled the left
       column to row 4, would start the right column at row 4 too */
    .deck { grid-template-columns: minmax(230px, 300px) minmax(0, 1fr) minmax(230px, 300px);
      grid-auto-flow: row dense; }
    .deck-map { grid-column: 2; grid-row: 1 / span 4; }
    .deck-l { grid-column: 1; }
    .deck-r { grid-column: 3; }
  }
  .card { position:relative; background: linear-gradient(180deg, var(--surface-2), var(--surface-1) 58%);
    border: 1px solid var(--border); border-radius: 12px; padding: 18px 20px 16px; box-shadow: var(--shadow); }
  .card h3 { margin: 0 0 8px 0; font-size: 13px; letter-spacing: 0; color: var(--text-secondary);
    font-weight: 600; }
  .card-head { display:flex; align-items:center; justify-content:space-between; gap:8px; margin-bottom:10px; }
  .card-head h3 { margin:0; }
  /* upper bound matters: in the ≥1200px deck the hero lives in a single
     side column — a 2-column span there would overlap the map */
  @media (min-width: 940px) and (max-width: 1199px) { .card-hero { grid-column: span 2; } }
  .stat-row { display:flex; align-items:flex-end; justify-content:space-between; gap: 10px; }
  .stat-value { font-size: 30px; font-weight: 650; letter-spacing: -.01em; line-height:1;
    font-variant-numeric: tabular-nums; }
  /* the one stat that IS a machine value: the public IP stays mono */
  .stat-value-ip { font-family: var(--font-mono); font-size: 18px; font-weight: 600; letter-spacing: 0; }
  .stat-value .unit { font-size: 14px; color: var(--muted); font-weight: 600; margin-left: 2px; }
  .stat-sub { font-size: 12.5px; color: var(--text-secondary); margin-top: 8px; }
  /* rating pill on each metric card; the good/fair thresholds live in its
     hover tooltip — a visible legend on every card was badge fatigue */
  .rating { font-family: var(--font-mono); font-size: 10px; font-weight: 800; letter-spacing: .1em;
    padding: 3px 8px; border-radius: 5px; display: none; white-space: nowrap; flex-shrink: 0; cursor: help; }
  .rating.show { display: inline-block; }
  .rating.good { color: var(--status-good); background: var(--status-good-bg); box-shadow: inset 0 0 0 1px var(--glow-good); }
  .rating.fair { color: color-mix(in srgb, var(--status-warning) 80%, black); background: var(--status-warning-bg); }
  .rating.poor { color: var(--status-critical); background: var(--status-critical-bg); box-shadow: inset 0 0 0 1px var(--glow-bad); }
  .delta { font-size: 12px; font-weight: 600; display:inline-flex; align-items:center; gap:2px; font-variant-numeric: tabular-nums; }
  .delta.good { color: var(--success-text); }
  .delta.bad { color: var(--status-critical); }
  .delta.flat { color: var(--muted); }
  .sparkline { flex-shrink:0; }

  .status-pill { display: inline-flex; align-items: center; gap: 8px; padding: 6px 14px 6px 11px; border-radius: 8px;
    font-size: 13.5px; font-weight: 800; font-family: var(--font-mono); letter-spacing: .08em; text-transform: uppercase; }
  .status-up { background: var(--status-good-bg); color: var(--status-good);
    box-shadow: inset 0 0 0 1px var(--glow-good); }
  .status-down { background: var(--status-critical-bg); color: var(--status-critical);
    box-shadow: inset 0 0 0 1px var(--glow-bad); }
  .status-dot { width: 8px; height: 8px; border-radius: 50%; background: currentColor; box-shadow: 0 0 6px currentColor; }
  .status-pill.small { font-size: 10.5px; padding: 3px 9px 3px 8px; letter-spacing: .08em; border-radius: 6px; box-shadow:none; }
  .status-pill.small .status-dot { box-shadow:none; }
  /* sage variant for "Online · silent" (probe/arp tier) — same decoder as the map */
  .status-silent-pill { background: var(--status-good-bg); color: var(--status-silent); }
  /* small muted type tag next to a device name (camera / printer / ...) */
  .dev-type { display:inline-block; margin-left:7px; padding:1px 6px; border-radius:5px; font-size:9.5px;
    font-weight:700; font-family: var(--font-mono); letter-spacing:.08em; text-transform:uppercase;
    color:var(--muted); background: var(--border-soft); vertical-align:1px; }
  /* devices: all-devices table left, IoT chips right (stacked on phones;
     left card takes the full row when no IoT devices are tagged) */
  .dev-cols { display:grid; grid-template-columns: 3fr 2fr; gap:12px; align-items:start; }
  .dev-cols.no-iot { grid-template-columns: 1fr; }
  @media (max-width: 940px) { .dev-cols { grid-template-columns: 1fr; } }
  /* IoT devices: dense chip grid (one compact card per device) */
  .iot-grid { display:grid; grid-template-columns: repeat(auto-fill, minmax(205px, 1fr)); gap:8px; }
  .iot-chip { background: var(--surface-2); border:1px solid var(--border-soft); border-radius:9px;
    padding:8px 11px; min-width:0; }
  .iot-chip-top { display:flex; align-items:center; justify-content:space-between; gap:8px; }
  .iot-chip-top .dev-id { min-width:0; }
  .iot-chip-top b { font-size:13px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .iot-chip-top .status-pill.small { flex-shrink:0; }
  .iot-chip-sub { font-family: var(--font-mono); font-size:10.5px; color:var(--muted); margin-top:4px;
    overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  #iotTableWrap { overflow-x: auto; }

  section { margin-bottom: 34px; scroll-margin-top: 58px; }
  section .section-head { display:flex; align-items:baseline; justify-content:space-between; margin-bottom: 12px;
    flex-wrap:wrap; gap:8px; padding-bottom: 9px; border-bottom: 1px solid var(--border-soft); }
  section h2 { font-size: 17px; margin: 0; font-weight: 650; letter-spacing: 0;
    display:flex; align-items:center; gap:9px; }
  section h2::before { content:""; width:7px; height:10px; background: var(--accent);
    clip-path: polygon(0 0, 100% 50%, 0 100%); flex-shrink:0; }
  section .section-note { font-size: 12px; color: var(--muted); }

  .range-toggle { display:flex; gap:4px; background: var(--surface-1); border:1px solid var(--border); border-radius: 9px; padding: 3px; }
  .range-toggle button { border:none; background:transparent; color: var(--muted); font-size:12px; font-weight:600;
    padding: 5px 11px; border-radius: 6px; cursor:pointer; }
  .range-toggle button.active { background: var(--accent-soft); color: var(--accent); box-shadow: inset 0 0 0 1px var(--accent-glow); }
  /* per-card tool cluster (mini range toggle + check-now) in the top-right
     corner of a chart card; labels get right padding so they can't run
     underneath it */
  .card-tools { position:absolute; top:12px; right:14px; display:flex; gap:8px; align-items:center; z-index:2; }
  .ghost-btn.mini { font-size:11px; padding:4px 10px; border-radius:6px; }
  .range-toggle.mini { padding:2px; gap:2px; background: var(--surface-2); }
  .range-toggle.mini button { font-size:11px; padding:3px 8px; border-radius:5px; }
  .chart-card.with-tools > .chart-label { padding-right: 130px; }

  .chart-card { background: var(--surface-1); border: 1px solid var(--border); border-radius: 12px; padding: 18px;
    box-shadow: var(--shadow); overflow-x:auto; position:relative; }
  /* hover readout: tracks the cursor's x but sits BELOW the plot area
     (never covering the lines being read); transiently overlays the
     card footer, which is fine — it only exists while hovering */
  .chart-tip { position:absolute; z-index:4; transform:translateX(-50%);
    display:none; flex-direction:column; gap:4px; min-width:130px; max-width:250px;
    background: var(--surface-2); border:1px solid var(--border); border-radius:10px;
    padding: 8px 11px; font-family: var(--font-mono); font-size:11.5px; color: var(--text-primary);
    pointer-events:none; box-shadow: var(--shadow); font-variant-numeric: tabular-nums; }
  .chart-tip.show { display:flex; }
  .chart-tip .tip-time { color: var(--text-secondary); font-weight:700; padding-bottom:4px;
    margin-bottom:2px; border-bottom:1px solid var(--border-soft); white-space:nowrap; }
  .chart-tip .tip-item { display:flex; align-items:center; gap:7px; white-space:nowrap; }
  .chart-tip .tip-item i { display:inline-block; width:9px; height:3px; border-radius:2px; flex-shrink:0; }
  .chart-card + .chart-card { margin-top: 12px; }
  /* two-up responsive grid: two charts per row on wide screens, one on phones */
  .chart-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 12px; }
  .chart-card + .chart-grid { margin-top: 12px; }  /* full-width card above a chart pair */
  .chart-grid > .chart-card { margin-top: 0; }
  /* fixed-height wrapper so maintainAspectRatio:false charts stay compact */
  .chart-box { position: relative; width: 100%; height: 225px; }
  .chart-box.sm { height: 185px; }
  .chart-box.lg { height: 250px; }
  .chart-box > canvas { position: absolute; inset: 0; }
  /* keep charts compact on phones too (single-column there) */
  @media (max-width: 640px) {
    .chart-box { height: 165px; }
    .chart-box.sm { height: 145px; }
    .chart-box.lg { height: 190px; }
  }
  /* check-cadence footer: command · age · frequency. Deliberately dimmer
     than everything else on the card — it's metadata, not a stat. Turns
     amber when a check runs way past its cadence (per-card stall tell). */
  .check-foot { margin-top: 10px; padding-top: 7px; border-top: 1px solid var(--border);
    font-size: 11px; color: var(--muted); font-family: var(--font-mono);
    font-variant-numeric: tabular-nums; }
  .check-foot.stale { color: var(--status-warning); font-weight: 700; }
  .chart-label { font-size: 12.5px; font-weight: 600; color: var(--text-secondary); margin-bottom: 10px; }
  /* "Focused: <router> ✕" chip on the per-router chart while a map link
     has the chart narrowed to one line */
  .chart-focus { margin-left: 10px; font-size: 11.5px; font-weight: 600; color: var(--accent);
    background: var(--accent-soft); border-radius: 6px; padding: 2px 9px; cursor: pointer; }
  .chart-focus:hover { box-shadow: inset 0 0 0 1px var(--accent-glow); }
  table { width: 100%; border-collapse: collapse; font-size: 13.5px; }
  th { text-align: left; color: var(--muted); font-weight: 600; font-size: 11px; text-transform: uppercase;
    letter-spacing: .06em; border-bottom: 1px solid var(--grid); padding: 8px 10px; }
  td { padding: 9px 10px; border-bottom: 1px solid var(--border-soft); font-variant-numeric: tabular-nums; vertical-align: middle; }
  /* phones: tighter cells so the 4-column tables fit without side-scroll
     (scrollbars are hidden globally, so horizontal overflow is invisible).
     overflow-wrap:anywhere collapses the name column's min-content width —
     without it one long device name forces the whole table past the
     viewport — and the 2-line clamp keeps verbose names from stacking
     rows 200px tall. */
  @media (max-width: 480px) {
    /* two-up stat cards: a single 990px column of cards buried the whole
       dashboard below a screen and a half of scrolling */
    .deck { grid-template-columns: 1fr 1fr; gap: 10px; }
    .deck-map, .card-hero { grid-column: span 2; }
    .stat-value { font-size: 25px; }
    table { font-size: 12px; }
    th, td { padding: 8px 4px; }
    td.mono, .device-name .mono { font-size: 11px; }
    .status-pill.small { padding: 2px 7px; font-size: 10px; }
    .device-icon { display: none; }
    .device-name b { font-weight: 600; }
    .device-name span { overflow-wrap: anywhere; }
    tr.event-row td.mono { overflow-wrap: anywhere; }
    /* clamp only the NAME line on phones — the MAC sub-line below it
       must stay visible, so the clamp moved off the container */
    .dev-id > span:first-child { display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
    /* .device-name prefix: outranks the base .dev-mac rule, which is
       declared LATER in this sheet and would otherwise win the tie */
    .device-name .dev-mac { font-size: 9.5px; }
  }
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
  .badge-iot { color: var(--series-blue); } .badge-iot .dot { background: var(--series-blue); }
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
    font-size:12px; font-weight:600;
    padding:7px 16px; border-radius:8px; cursor:pointer; transition: color .12s ease, border-color .12s ease; }
  .ghost-btn:hover { color: var(--accent); border-color: var(--accent-glow); }

  /* ---------- outages: summary chips + incident timeline + filters ---------- */
  .outage-summary { display:grid; grid-template-columns: repeat(auto-fit, minmax(148px,1fr)); gap:10px; margin-bottom:18px; }
  .osum { background: var(--surface-2); border:1px solid var(--border-soft); border-radius:10px; padding:11px 13px 12px; position:relative; overflow:hidden; }
  .osum[data-cat] { cursor: pointer; transition: border-color .12s ease; }
  .osum[data-cat]:hover { border-color: var(--accent-glow); }
  .osum::before { content:""; position:absolute; left:0; top:0; bottom:0; width:3px; background: var(--muted); opacity:.85; }
  .osum.good::before { background: var(--status-good); } .osum.warn::before { background: var(--status-warning); } .osum.bad::before { background: var(--status-critical); }
  .osum .k { font-size:11px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); font-weight:600; }
  .osum .v { font-size:22px; font-weight:650; font-variant-numeric:tabular-nums; margin-top:6px; letter-spacing:-.01em; line-height:1; }
  .osum .s { font-size:10.5px; color:var(--text-secondary); margin-top:5px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .osum.good .v { color: var(--status-good); } .osum.warn .v { color: var(--status-warning); } .osum.bad .v { color: var(--status-critical); }

  .timeline-head { display:flex; align-items:baseline; justify-content:space-between; gap:10px; margin-bottom:9px; flex-wrap:wrap; }
  .timeline-label { font-size:12.5px; font-weight:600; color:var(--text-secondary); }
  .timeline-legend { display:flex; gap:12px; flex-wrap:wrap; font-size:11px; color:var(--text-secondary); }
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
  .timeline-svg [data-ev] { cursor: pointer; }
  .timeline-svg [data-ev]:hover { opacity: 1; }
  .timeline-empty { padding:14px 2px; }
  /* timeline click → the matching log row flashes so the eye lands on it */
  tr.event-row.flash td { animation: rowFlash 2s ease-out; }
  @keyframes rowFlash { 0% { background: var(--accent-soft); } 100% { background: transparent; } }
  tr.event-row .rel { color: var(--muted); font-size: 11.5px; }
  /* flight-recorder evidence: toggle button in the detail cell + a
     collapsed full-width row rendering the snapshot */
  .ev-btn { font-family: var(--font-mono); font-size: 10px; padding: 1px 7px; margin-left: 7px;
    border-radius: 5px; border: 1px solid var(--border); background: var(--surface-1);
    color: var(--accent); cursor: pointer; }
  .ev-btn:hover { border-color: var(--accent); }
  .ev-row td { background: var(--surface-1); border-left: 3px solid var(--border); }
  .ev-body { font-family: var(--font-mono); font-size: 11px; line-height: 1.9; color: var(--text-secondary);
    padding: 4px 2px; }
  .ev-body .ev-k { color: var(--muted); font-weight: 700; text-transform: uppercase; font-size: 9.5px;
    letter-spacing: .1em; margin-right: 7px; }
  .ev-body .ev-ok { color: var(--status-good); }
  .ev-body .ev-bad { color: var(--status-critical); }
  .ev-body .ev-note { color: var(--muted); font-size: 10px; }
  .ev-trace { margin: 3px 0 0; padding: 7px 10px; background: var(--surface-2); border-radius: 6px;
    overflow-x: auto; font-size: 10.5px; line-height: 1.55; color: var(--text-secondary); }

  .outage-filters { display:flex; gap:6px; flex-wrap:wrap; margin:18px 0 12px; }
  .ofilter { border:1px solid var(--border); background:var(--surface-1); color:var(--muted);
    font-size:12px; font-weight:600; padding:5px 11px; border-radius:7px;
    cursor:pointer; display:inline-flex; align-items:center; gap:6px; transition: color .12s ease, background .12s ease; }
  .ofilter:hover { color:var(--text-primary); }
  .ofilter.active { background: var(--accent-soft); color: var(--accent); box-shadow: inset 0 0 0 1px var(--accent-glow); }
  .ofilter .cnt { font-size:9.5px; opacity:.75; font-variant-numeric:tabular-nums; }
  .outage-clear { display:flex; align-items:center; gap:8px; color:var(--status-good); font-family:var(--font-mono);
    font-size:12px; font-weight:700; padding:16px 2px; }
  .outage-clear .status-dot { box-shadow: 0 0 6px var(--glow-good); }

  .device-name { display:flex; align-items:center; gap:8px; }
  .dev-id { display:flex; flex-direction:column; min-width:0; }
  .chart-sublabel { font-size: 11px; color: var(--muted);
    font-weight: 400; margin-left: 10px; }
  .dev-mac { font-family: var(--font-mono); font-size: 10.5px; color: var(--muted); letter-spacing: .02em; }
  .device-icon { width:26px; height:26px; border-radius:7px; background: var(--surface-2); border:1px solid var(--border);
    display:flex; align-items:center; justify-content:center; flex-shrink:0; color: var(--muted); }
  .mono { font-family: var(--font-mono); font-size: 12px; color: var(--text-secondary); }

  .empty { color: var(--muted); font-size: 13px; padding: 20px 4px; text-align:center; }
  .legend-note { display: grid; grid-template-columns: repeat(auto-fit, minmax(340px, 1fr)); gap: 8px 28px;
    font-size: 12.5px; color: var(--text-secondary); margin-top: 12px; padding-top: 12px; border-top: 1px solid var(--border-soft); }
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
  /* outdated-version disclaimer: same banner bones, accent flavor */
  .update-banner { background: var(--accent-soft); border-left-color: var(--accent); }
  .update-banner b { display:inline; margin:0; }
  .update-banner a { color: var(--accent); font-weight: 600; text-decoration: none; }
  .update-banner a:hover { text-decoration: underline; }

  /* "What's wrong right now" verdict — one plain-language line + a
     recommended action, colored by severity. The single most important
     element on the page when something is broken. */
  .diag-banner { border: 1px solid var(--border); border-left: 4px solid var(--muted);
    border-radius: 10px; padding: 13px 16px; margin-bottom: 18px;
    display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; }
  .diag-banner .diag-head { font-size: 15px; font-weight: 700; }
  .diag-banner .diag-action { font-size: 13px; color: var(--text-secondary); }
  .diag-banner .diag-chip { font-family: var(--font-mono); font-size: 11px; color: var(--muted);
    margin-left: auto; white-space: nowrap; }
  .diag-banner .diag-also { flex-basis: 100%; font-size: 12px; color: var(--muted); }
  .diag-ok      { border-left-color: var(--status-good);     background: var(--status-good-bg); }
  .diag-warn    { border-left-color: var(--status-warning);  background: var(--status-warning-bg); }
  .diag-serious { border-left-color: var(--status-serious);  background: var(--status-serious-bg); }
  .diag-crit    { border-left-color: var(--status-critical); background: var(--status-critical-bg); }
  .diag-ok .diag-head { color: var(--status-good); }
  .diag-serious .diag-head { color: var(--status-serious); }
  .diag-crit .diag-head { color: var(--status-critical); }

  /* ---------- house map ---------- */
  .house-svg { display:block; width:100%; max-width: 960px; margin: 0 auto; height:auto; }
  .house-svg .wall { fill: var(--scene-wall); stroke: var(--baseline); stroke-width: 1.5; }
  .house-svg .roof { fill: var(--scene-roof); stroke: var(--baseline); stroke-width: 1.5; stroke-linejoin: round; }
  .house-svg .basement-band { fill: var(--grid); opacity: 0.32; }
  .house-svg .floor-sep { stroke: var(--grid); stroke-width: 1.2; stroke-dasharray: 2 6; }
  .house-svg .floor-label { fill: var(--muted); font-size: 9.5px; font-weight: 700; letter-spacing: .12em;
    text-transform: uppercase; font-family: var(--font-mono); }
  .house-svg .floor-chip { fill: var(--surface-1); stroke: var(--border-soft); }
  .house-svg .street-label { fill: var(--muted); font-size: 9px; font-weight: 700; letter-spacing: .16em;
    text-transform: uppercase; font-family: var(--font-mono); opacity: 0.75; }
  .house-svg .datum-line { stroke: var(--baseline); stroke-width: 1.3; }
  .house-svg .datum-mark { fill: var(--muted); opacity: .8; }
  .house-svg .node-up { color: var(--status-good); }
  /* alive but only via the ARP cache — same family, clearly quieter */
  .house-svg .node-silent { color: var(--status-silent); }
  .house-svg .node-down { color: var(--status-critical); }
  .house-svg .node-main { color: var(--accent); }
  .house-svg g.node-up { filter: drop-shadow(0 0 7px var(--glow-good)); }
  .house-svg g.node-silent { filter: drop-shadow(0 0 6px var(--glow-silent)); }
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
  /* compact router pills — details live in the hover card */
  .house-svg .pillgrp { cursor: pointer; }
  .house-svg .pill-box { fill: var(--surface-1); stroke: currentColor; stroke-width: 1.5; }
  .house-svg .pill-name { fill: var(--text-primary); font-size: 11px; font-weight: 700; }
  .house-svg .isp-sub { fill: var(--muted); font-size: 9px; font-family: var(--font-mono);
    text-transform: uppercase; letter-spacing: .1em; }
  /* compact (phone) map: the viewBox shrinks to ~330 units so labels keep
     most of their size; bump the small ones so nothing lands under ~9px.
     Keep .pill-name in sync with the JS pillW() char-width estimate. */
  .house-svg.compact .pill-name { font-size: 12px; }
  .house-svg.compact .floor-label { font-size: 11px; }
  .house-svg.compact .street-label { font-size: 10.5px; }
  .house-svg.compact .net-label { font-size: 11.5px; }
  .house-svg.compact .net-stat { font-size: 11px; }
  .house-svg .hovercard { opacity: 0; pointer-events: none; transition: opacity .15s ease; }
  /* shown cards accept the mouse so the "chart" link inside is clickable
     (hide runs on a short delay so crossing the pill→card gap survives) */
  .house-svg .hovercard.show { opacity: 1; pointer-events: auto; }
  .house-svg .card-link { fill: var(--accent); font-size: 10px; font-weight: 700; cursor: pointer; }
  .house-svg .card-link:hover { fill: var(--text-primary); }
  /* SVG flavors of the cadence footer (hover cards + internet node) */
  .house-svg .card-div { stroke: var(--border); stroke-width: 1; }
  .house-svg .card-foot { fill: var(--muted); font-size: 9px; font-family: var(--font-mono); }
  .house-svg .card-foot.stale { fill: var(--status-warning); font-weight: 700; }
  .house-svg .net-foot { fill: var(--text-secondary); font-size: 8px; font-family: var(--font-mono); opacity: .85; }
  .house-svg .net-foot.stale { fill: var(--status-warning); opacity: 1; font-weight: 700; }
  /* wi-fi coverage bubbles behind each AP */
  .house-svg .cover { fill: currentColor; stroke: currentColor; stroke-opacity: .15; opacity: .06; }
  .house-svg .cover.up { color: var(--status-good); }
  .house-svg .cover.silent { color: var(--status-silent); }
  .house-svg .cover.main { color: var(--accent); }
  .house-svg .cover.down { color: var(--status-critical); opacity: .12; animation: coverPulse 2.2s ease-in-out infinite; }
  @keyframes coverPulse { 50% { opacity: .26; } }
  /* windows: slim panes, lit while that floor's access points are all up */
  .house-svg .win { stroke: var(--baseline); stroke-opacity: .45; stroke-width: 1; }
  .house-svg .win.lit { fill: var(--scene-sun); opacity: .5; }
  .house-svg .win.off { fill: var(--grid); opacity: .9; }
  /* the internet uplink node outside the house */
  .house-svg .net-node.up { color: var(--status-good); }
  .house-svg .net-node.down { color: var(--status-critical); }
  .house-svg g.net-node.up { filter: drop-shadow(0 0 7px var(--glow-good)); }
  .house-svg g.net-node.down { filter: drop-shadow(0 0 9px var(--glow-bad)); }
  .house-svg .net-box { fill: var(--surface-1); stroke: currentColor; stroke-width: 1.5; }
  .house-svg .net-label { fill: var(--text-primary); font-size: 10px; font-weight: 800; letter-spacing: .12em;
    text-transform: uppercase; font-family: var(--font-mono); }
  .house-svg .net-stat { fill: var(--text-secondary); font-size: 9.5px; font-family: var(--font-mono); }
  .house-svg .net-node.down .net-stat { fill: currentColor; font-weight: 700; }
  @media (prefers-reduced-motion: reduce) {
    .house-svg .linkgrp.up .link-core, .house-svg .node-down .status-dot-svg,
    .house-svg .cover.down,
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
    <div class="topbar-tools">
      <div class="theme-toggle" id="refreshCtl">
        <button id="refreshBtn" title="Reload the page now">
          <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-2.64-6.36"/><polyline points="21 3 21 9 15 9"/></svg>
          Refresh
        </button>
        <button id="autoRefreshBtn" title="Reload automatically every minute — the page regenerates every 60s">Auto: off</button>
        <button id="testNowBtn" title="Run a live connectivity check right now: 5 pings to the router + internet and a DNS lookup (~15 seconds)" style="display:none">Test now</button>
      </div>
      <span id="testNowResult" class="section-note" style="display:none"></span>
      <div class="theme-toggle" id="globalRange" title="Set every chart's time range at once (each chart also has its own toggle)">
        <button data-range="3">3h</button>
        <button data-range="24">24h</button>
        <button data-range="168">7d</button>
      </div>
      <div class="theme-toggle" id="themeToggle">
        <button data-theme="light">Light</button>
        <button data-theme="dark">Dark</button>
        <button data-theme="auto">Auto</button>
      </div>
      <a id="settingsLink" style="display:none" title="Configure routers, floors, and device names (only available on the monitor PC)">Settings</a>
    </div>
  </div>

  <nav class="quick-nav quick-nav-static" aria-label="Sections">
    <button data-goto="sec-deck">Map</button>
    <button data-goto="sec-charts">Latency</button>
    <button data-goto="sec-speed">Speed</button>
    <button data-goto="sec-outages">Outages</button>
    <button data-goto="sec-devices">Devices</button>
  </nav>

  <div id="updateBanner" class="warning-banner update-banner"></div>

  <div id="diagBanner" class="diag-banner" style="display:none"></div>

  <nav id="quickNav" class="quick-nav" aria-label="Jump to section">
    <button data-goto="sec-deck">Map</button>
    <button data-goto="sec-charts">Latency</button>
    <button data-goto="sec-speed">Speed</button>
    <button data-goto="sec-outages">Outages</button>
    <button data-goto="sec-devices">Devices</button>
  </nav>

  <section id="sec-deck">
    <div class="section-head">
      <h2>Routers &amp; access points</h2>
      <span class="section-note">from routers.json</span>
    </div>
    <div class="deck">
      <div class="chart-card panel-hud deck-map">
        <div id="houseMapWrap"></div>
        <div id="houseMapNote" class="section-note" style="display:none; text-align:center; margin-top:6px;"></div>
      </div>
      <div class="card card-hero deck-l">
        <h3>Current status</h3>
        <div class="stat-row">
          <div>
            <div id="statusPill"></div>
            <div class="stat-sub" id="currentLatency"></div>
          </div>
          <canvas class="sparkline" id="sparkline" width="150" height="44"></canvas>
        </div>
        <div class="check-foot" id="cfStatus"></div>
      </div>
      <div class="card deck-l">
        <div class="card-head"><h3>Uptime · 24h</h3><span class="rating" id="rateUptime24"></span></div>
        <div class="stat-row">
          <div class="stat-value" id="uptime24h">—</div>
          <div class="delta" id="uptimeDelta"></div>
        </div>
        <div class="stat-sub" id="loss24h"></div>
        <div class="check-foot" id="cfUptime24"></div>
      </div>
      <div class="card deck-l">
        <div class="card-head"><h3>Uptime · 7d</h3><span class="rating" id="rateUptime7"></span></div>
        <div class="stat-value" id="uptime7d">—</div>
        <div class="stat-sub" id="loss7d"></div>
        <div class="check-foot" id="cfUptime7"></div>
      </div>
      <div class="card deck-l">
        <div class="card-head"><h3>Avg latency · 24h</h3><span class="rating" id="rateLatency"></span></div>
        <div class="stat-row">
          <div class="stat-value" id="avgLatency24h">—</div>
          <div class="delta" id="latencyDelta"></div>
        </div>
        <div class="stat-sub">to 1.1.1.1 / 8.8.8.8 / 9.9.9.9</div>
        <div class="check-foot" id="cfLatency"></div>
      </div>
      <div class="card deck-r">
        <div class="card-head"><h3>DNS · 24h</h3><span class="rating" id="rateDns"></span></div>
        <div class="stat-value" id="dnsAvg">—</div>
        <div class="stat-sub" id="dnsSub">name-lookup speed</div>
        <div class="check-foot" id="cfDns"></div>
      </div>
      <div class="card deck-r">
        <div class="card-head"><h3>Jitter · 24h</h3><span class="rating" id="rateJitter"></span></div>
        <div class="stat-value" id="jitter24h">—</div>
        <div class="stat-sub">latency stability — lower is steadier (calls/gaming)</div>
        <div class="check-foot" id="cfJitter"></div>
      </div>
      <div class="card deck-r">
        <div class="card-head"><h3>Speed · last test</h3><span class="rating" id="rateSpeed"></span></div>
        <div class="stat-value" id="speedLast">—</div>
        <div class="stat-sub" id="speedLastSub">Mbps down / up</div>
        <div class="check-foot" id="cfSpeedCard"></div>
      </div>
      <div class="card deck-r">
        <h3>Public IP</h3>
        <div class="stat-value stat-value-ip" id="publicIp">—</div>
        <div class="stat-sub" id="publicIpStable"></div>
        <div class="check-foot" id="cfPublicIp"></div>
      </div>
    </div>
    <div class="chart-card with-tools" id="routersCard">
      <div class="card-tools"><span class="range-toggle mini" data-chart="routers"><button data-range="3">3h</button><button data-range="24">24h</button><button data-range="168">7d</button></span></div>
      <div class="chart-label">Per-router latency (ms)<span class="chart-focus" id="routerFocus" style="display:none" title="Click to show all routers again"></span></div>
      <div class="chart-box lg"><canvas id="routersChart"></canvas></div>
      <div id="routersChartEmpty" class="empty" style="display:none">No router ping history yet.</div>
      <div class="check-foot" id="cfRouters"></div>
    </div>
    <div class="chart-card with-tools" id="targetsCard" style="display:none">
      <div class="card-tools"><span class="range-toggle mini" data-chart="targets"><button data-range="3">3h</button><button data-range="24">24h</button><button data-range="168">7d</button></span></div>
      <div class="chart-label">Your targets — latency (ms)<span class="chart-sublabel">custom destinations from Settings; each gets its own outage events</span></div>
      <div class="chart-box"><canvas id="targetsChart"></canvas></div>
      <div class="check-foot" id="cfTargets"></div>
    </div>
  </section>

  <section id="sec-charts">
    <div class="section-head">
      <h2>Latency &amp; packet loss</h2>
    </div>
    <div class="chart-grid">
      <div class="chart-card with-tools">
        <div class="card-tools"><button class="ghost-btn mini" data-checknow="quick" data-checknote="latency" style="display:none" title="Run a live ping + DNS check right now (~10s) — the result is recorded like any scheduled check">Check now</button><span class="range-toggle mini" data-chart="latency"><button data-range="3">3h</button><button data-range="24">24h</button><button data-range="168">7d</button></span></div>
        <div class="chart-label">Average latency (ms)</div>
        <div class="chart-box"><canvas id="latencyChart"></canvas></div>
        <div class="stat-sub" id="ck-latency" style="display:none"></div>
        <div class="check-foot" id="cfLatencyChart"></div>
      </div>
      <div class="chart-card with-tools">
        <div class="card-tools"><button class="ghost-btn mini" data-checknow="quick" data-checknote="loss" style="display:none" title="Run a live ping + DNS check right now (~10s) — the result is recorded like any scheduled check">Check now</button><span class="range-toggle mini" data-chart="loss"><button data-range="3">3h</button><button data-range="24">24h</button><button data-range="168">7d</button></span></div>
        <div class="chart-label">Packet loss (%)</div>
        <div class="chart-box sm"><canvas id="lossChart"></canvas></div>
        <div class="stat-sub" id="ck-loss" style="display:none"></div>
        <div class="check-foot" id="cfLossChart"></div>
      </div>
    </div>
  </section>

  <section id="sec-speed">
    <div class="section-head">
      <h2>Speed</h2>
    </div>
    <div class="chart-card with-tools">
      <div class="card-tools"><button class="ghost-btn mini" data-checknow="speed" data-checknote="speed" style="display:none" title="Run a full speed test right now (~1 min) — it briefly loads the line and is recorded like any scheduled test">Check now</button><span class="range-toggle mini" data-chart="speed"><button data-range="3">3h</button><button data-range="24">24h</button><button data-range="168">7d</button></span></div>
      <div class="chart-label">Speed test (Mbps)<span id="speedVantage" class="chart-sublabel"></span></div>
      <div class="chart-box"><canvas id="speedChart"></canvas></div>
      <div id="speedEmpty" class="empty" style="display:none">No speed test data yet — install a speed test tool (see README) and it will appear here automatically.</div>
      <div class="stat-sub" id="ck-speed" style="display:none"></div>
      <div id="speedFailNote" class="stat-sub" style="display:none"></div>
      <div class="check-foot" id="cfSpeed"></div>
    </div>
    <div class="chart-grid">
      <div class="chart-card with-tools">
        <div class="card-tools"><span class="range-toggle mini" data-chart="loaded"><button data-range="3">3h</button><button data-range="24">24h</button><button data-range="168">7d</button></span></div>
        <div class="chart-label" style="display:flex; align-items:center; gap:8px;">Latency under load (ms)
          <span class="rating" id="rateBufferbloat"></span></div>
        <div class="chart-box sm"><canvas id="loadedLatencyChart"></canvas></div>
        <div id="loadedLatencyEmpty" class="empty" style="display:none">No latency-under-load data yet — it needs the official Ookla speedtest CLI (see README) and appears after the next test.</div>
        <div id="bufferbloatHint" class="stat-sub" style="display:none"></div>
        <div class="check-foot" id="cfBufferbloat"></div>
      </div>
    </div>
  </section>

  <section id="sec-outages">
    <div class="section-head">
      <h2>Outages &amp; degradation log</h2>
      <span class="section-note">last 7 days, most recent first · <a href="report.html" title="Printable outage/speed summary — evidence for ISP complaints">ISP evidence report &rarr;</a></span>
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
        <div class="legend-item"><span class="badge badge-iot"><span class="dot"></span>IoT device down</span><span>A watched device (camera, printer, …) stopped answering — that device, not your internet; excluded from uptime.</span></div>
        <div class="legend-item"><span class="badge badge-gap"><span class="dot"></span>Monitoring paused</span><span>No data was collected (Mac asleep or monitor stopped) — not an outage, but not measured uptime either.</span></div>
      </div>
    </div>
  </section>

  <section id="sec-devices">
    <div class="section-head">
      <h2>Devices on your network</h2>
      <div style="display:flex; align-items:center; gap:12px; flex-wrap:wrap;">
        <span class="section-note" id="devicesNote">most recent scan</span>
        <span class="section-note" id="devScanNote" style="display:none"></span>
        <button class="ghost-btn mini" id="devScanBtn" style="display:none" title="Sweep the network for devices right now (~10-30s) — same scan the monitor runs on its schedule">Scan now</button>
        <input type="search" id="devSearch" class="search-box" placeholder="Filter — name, IP, MAC" autocomplete="off" spellcheck="false">
      </div>
    </div>
    <div class="chart-card with-tools">
      <div class="card-tools"><span class="range-toggle mini" data-chart="devcount"><button data-range="3">3h</button><button data-range="24">24h</button><button data-range="168">7d</button></span></div>
      <div class="chart-label">Devices online over time</div>
      <div class="chart-box sm"><canvas id="devCountChart"></canvas></div>
      <div id="devCountEmpty" class="empty" style="display:none">No scan history yet.</div>
      <div class="check-foot" id="cfDevices"></div>
    </div>
    <!-- chart on top, then all devices left / IoT devices right; the
         right card hides (and the left goes full width) when nothing is
         typed/watched in devices.json -->
    <div class="dev-cols no-iot" id="devCols">
      <div class="chart-card">
        <div class="chart-label">All devices</div>
        <div id="devicesTableWrap"></div>
      </div>
      <div class="chart-card" id="iotCard" style="display:none">
        <div class="chart-label">IoT devices<span class="chart-sublabel" id="iotNote"></span></div>
        <div id="iotTableWrap"></div>
        <div class="check-foot" id="cfIot"></div>
      </div>
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
  // DATA.update is only ever non-null when a strictly NEWER release
  // exists, so this doubles as the outdated-version disclaimer
  const ub = document.getElementById('updateBanner');
  if (ub) {
    const link = DATA.update.url
      ? ' <a href="' + escapeHtml(DATA.update.url) + '" target="_blank" rel="noopener">See what’s new &rarr;</a>' : '';
    ub.innerHTML = 'This monitor is running v' + escapeHtml(DATA.version || '?')
      + ' — <b>v' + escapeHtml(DATA.update.latest) + ' is available.</b>' + link;
    ub.style.display = 'block';
  }
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
safely('speed card', function() {
  // last successful speed test; the sub-line rates it against the plan.
  // Cutoffs come from thresholds.plan_pct (defaults 90/80) — the same
  // numbers the ISP evidence report uses for its below-plan list.
  const s = (DATA.speed_series || []).slice(-1)[0];
  const el = document.getElementById('speedLast'), sub = document.getElementById('speedLastSub');
  // where the monitor measures FROM: a speed number without its vantage
  // point invites blaming the ISP for a bad in-house cable (been there)
  const via = DATA.monitor_location ? ' · via ' + escapeHtml(DATA.monitor_location) : '';
  if (!s || s.down == null) { sub.textContent = 'no speed test yet'; return; }
  el.innerHTML = Math.round(s.down) + '<span class="unit">&#8595;</span> '
    + (s.up != null ? Math.round(s.up) : '—') + '<span class="unit">&#8593;</span>';
  const plan = DATA.plan || {};
  if (plan.down_mbps) {
    const PP = Object.assign({ good: 90, fair: 80 }, (DATA.thresholds || {}).plan_pct || {});
    const pct = Math.round(100 * s.down / plan.down_mbps);
    sub.innerHTML = pct + '% of the ' + plan.down_mbps + ' Mbps plan' + via;
    const rEl = document.getElementById('rateSpeed');
    const lvl = pct >= PP.good ? 0 : pct >= PP.fair ? 1 : 2;
    rEl.className = 'rating show ' + ['good', 'fair', 'poor'][lvl];
    rEl.textContent = ['GOOD', 'FAIR', 'LOW'][lvl];
    rEl.title = 'vs your plan: ' + PP.good + '%+ good, ' + PP.fair + '%+ fair';
    if (lvl === 2) sub.style.color = 'var(--status-warning)';
  } else {
    sub.innerHTML = 'Mbps down / up' + via;
  }
});
if (DATA.dns_24h && DATA.dns_24h.checks) {
  // sub-line: system-resolver health, then the direct per-resolver verdict
  // (router DNS proxy vs 1.1.1.1 vs 8.8.8.8 — queried separately, so
  // "router DNS ✗" while the publics answer = reboot the router, not the ISP)
  let sub = DATA.dns_24h.failures
    ? DATA.dns_24h.failures + ' failed lookups' : 'all lookups succeeded';
  const rs = DATA.resolver_status || {};
  const names = Object.keys(rs);
  if (names.length) {
    const label = n => n === 'gateway' ? 'router' : n;
    const bad = names.filter(n => rs[n] && rs[n].ok === false);
    sub += bad.length ? ' · ' + bad.map(n => label(n) + ' DNS ✗').join(' · ')
                      : ' · ' + names.length + ' resolvers ✓';
    const el = document.getElementById('dnsSub');
    el.title = names.map(n => label(n) + ': ' + (rs[n].ok ? (rs[n].ms != null ? rs[n].ms + 'ms' : 'ok') : 'FAILING')
      + (rs[n].fails_1h ? ` (${rs[n].fails_1h}/${rs[n].checks_1h} failed last hour)` : '')).join(' · ');
    el.textContent = sub;
  } else {
    document.getElementById('dnsSub').textContent = sub;
  }
}
setStat('jitter24h', DATA.jitter_24h, 'ms');

function timeSince(ts) {
  const secs = (Date.now() - new Date(ts).getTime()) / 1000;
  if (secs < 3600) return Math.max(1, Math.round(secs / 60)) + 'm';
  if (secs < 86400) return Math.round(secs / 3600) + 'h';
  return Math.round(secs / 86400) + 'd';
}

// ---------- check-cadence footers ----------
// Every card riding a recurring check gets a footer: command · age ·
// frequency. Elements carry data-* attrs and are re-queried on each tick,
// so footers baked into re-rendered SVG (hover cards, internet node) keep
// working without re-registration. Ages come from the wall clock (ticked
// every 10s), so they stay honest between the page's 60s regens; a check
// running way past its cadence turns the footer amber. Frequencies mirror
// monitor.py's *_INTERVAL_SEC constants — update both together.
function agoShort(secs) {
  if (secs < 60) return Math.max(0, Math.round(secs)) + 's';
  if (secs < 3600) return Math.round(secs / 60) + 'm';
  if (secs < 86400) return Math.round(secs / 3600) + 'h';
  return Math.round(secs / 86400) + 'd';
}
function freqShort(s) {
  // friendly rounding for measured (non-round) cadences: 30.2 -> "30s",
  // 92 -> "1.5m", 1810 -> "30m"
  if (s < 120) return Math.round(s / 5) * 5 + 's';
  if (s < 3600) { const m = Math.round(s / 30) / 2; return (m % 1 ? m.toFixed(1) : m) + 'm'; }
  const h = Math.round(s / 1800) / 2; return (h % 1 ? h.toFixed(1) : h) + 'h';
}
function setCheckFoot(id, cmd, ts, freqSec, approx) {
  const el = document.getElementById(id);
  if (!el || !freqSec) return;
  el.dataset.checkfoot = '1';
  el.dataset.cmd = cmd;
  el.dataset.freq = freqSec;
  if (approx) el.dataset.approx = '1'; else delete el.dataset.approx;
  if (ts) el.dataset.ts = ts; else delete el.dataset.ts;
}
function tickCheckFoots() {
  document.querySelectorAll('[data-checkfoot]').forEach(el => {
    const freq = +el.dataset.freq;
    // fmt: long = "ping · 6s ago · every ~30s" (HTML cards),
    //      mid  = "ping · 6s ago · ~30s"       (hover cards — 176px wide),
    //      min  = "ping · 6s · ~30s"           (internet node — 124px wide)
    const fmt = el.dataset.fmt || 'long';
    // "~" marks a MEASURED cadence (median gap in the actual data); a bare
    // value is the configured interval (no data to measure yet)
    const tilde = el.dataset.approx ? '~' : '';
    let mid = 'no data yet', stale = false;
    if (el.dataset.ts) {
      const secs = (Date.now() - new Date(el.dataset.ts).getTime()) / 1000;
      mid = agoShort(secs) + (fmt === 'min' ? '' : ' ago');
      // generous slack: the page itself only regenerates every 60s, so a
      // 15s check legitimately reads up to ~75s old right before a refresh
      stale = secs > freq * 2 + 150;
    }
    el.textContent = el.dataset.cmd + ' · ' + mid + ' · '
      + (fmt === 'long' ? 'every ' : '') + tilde + freqShort(freq);
    el.classList.toggle('stale', stale);
  });
}
// measured cadence when the data supports it, configured interval as the
// fallback — exposed globally because the house map's SVG footers (built
// later, rebuilt on rerender) need the same numbers
function checkEff(key) {
  const C = DATA.checks || {};
  const m = (C.measured || {})[key], f = (C.freq || {})[key];
  // A cycle is sleep(configured) + work time, so the real cadence can
  // never be SHORTER than the configured interval. Measured below
  // configured means the median still reflects history from before an
  // interval change — trust the config until the data catches up (this
  // is what made the device footer claim "~5m", amber and all, right
  // after the user set the scan to 30m).
  if (m && (!f || m >= f)) return { freq: m, approx: true };
  return { freq: f || m, approx: false };
}
safely('speed vantage', function() {
  // the chart-level version of the card's "via X" note — where the
  // monitor PC measures from (config.json monitor_location)
  if (DATA.monitor_location) {
    document.getElementById('speedVantage').textContent =
      'measured from the monitor PC via ' + DATA.monitor_location;
  }
});
safely('check footers', function() {
  const C = DATA.checks || {};
  const ping = checkEff('ping'), dns = checkEff('dns'), spd = checkEff('speed'),
        pip = checkEff('public_ip'), dev = checkEff('devices');
  setCheckFoot('cfStatus',       'ping', C.ping, ping.freq, ping.approx);
  setCheckFoot('cfUptime24',     'ping', C.ping, ping.freq, ping.approx);
  setCheckFoot('cfUptime7',      'ping', C.ping, ping.freq, ping.approx);
  setCheckFoot('cfLatency',      'ping', C.ping, ping.freq, ping.approx);
  setCheckFoot('cfJitter',       'ping', C.ping, ping.freq, ping.approx);
  setCheckFoot('cfLatencyChart', 'ping', C.ping, ping.freq, ping.approx);
  setCheckFoot('cfLossChart',    'ping', C.ping, ping.freq, ping.approx);
  setCheckFoot('cfDns',          'dns lookup', C.dns, dns.freq, dns.approx);
  setCheckFoot('cfPublicIp',     'https query', C.public_ip, pip.freq, pip.approx);
  setCheckFoot('cfSpeed',        'ookla speedtest cli', C.speed, spd.freq, spd.approx);
  setCheckFoot('cfSpeedCard',    'ookla speedtest cli', C.speed, spd.freq, spd.approx);
  setCheckFoot('cfBufferbloat',  'ookla speedtest cli', C.speed, spd.freq, spd.approx);
  setCheckFoot('cfDevices',      C.device_cmd || 'device scan', C.devices, dev.freq, dev.approx);
  // only when something is watched — an empty freq would render "no data"
  if ((DATA.iot_devices || []).some(d => d.watch)) {
    const iot = checkEff('iot');
    setCheckFoot('cfIot', 'ping / tcp / arp', C.iot, iot.freq, iot.approx);
  }
  // per-router chart rides the router thread; freshest check + median of
  // the per-router measured cadences (they move together — one loop)
  const rs = DATA.router_summary || [];
  const rLast = rs.map(r => r.last_check).filter(Boolean).sort().pop();
  const rMeas = rs.map(r => r.measured).filter(Boolean).sort((a, b) => a - b);
  const rEff = rMeas.length ? { freq: rMeas[Math.floor(rMeas.length / 2)], approx: true }
                            : { freq: ((C.freq || {}).router) || 15, approx: false };
  setCheckFoot('cfRouters', 'ping / tcp / arp', rLast, rEff.freq, rEff.approx);
  setCheckFoot('cfTargets', 'ping burst', C.ping, ping.freq, ping.approx);  // rides the ping thread
  tickCheckFoots();
  setInterval(tickCheckFoots, 10000);
});
document.getElementById('publicIp').textContent = DATA.current_public_ip || '—';
if (DATA.current_public_ip && DATA.ip_stable_since) {
  const prefix = DATA.ip_stable_at_least ? 'stable for at least ' : 'stable for ';
  document.getElementById('publicIpStable').textContent = prefix + timeSince(DATA.ip_stable_since);
} else {
  document.getElementById('publicIpStable').textContent = '';
}
safely('public ip health', function() {
  // Repeated public-IP fetch failures usually mean the connection blipped
  // between ping samples — an ISP-flakiness signal that used to be
  // collected and then silently dropped. Only warn on a real pattern
  // (≥3 failures AND >25% of checks), never on a single transient.
  const h = DATA.public_ip_health || {};
  if ((h.failures || 0) >= 3 && h.failures / Math.max(1, h.checks) > 0.25) {
    const el = document.getElementById('publicIpStable');
    el.innerHTML = escapeHtml(el.textContent)
      + '<br><span style="color:var(--status-warning); font-weight:600;">⚠ '
      + h.failures + ' of ' + h.checks + ' IP checks failed in 24h — brief ISP drops can cause this</span>';
  }
});

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
  // Bufferbloat = how much latency CLIMBS while the line is saturated
  // (loaded minus idle). Boundaries follow Waveform's widely-used grading:
  // ≤30ms added is an A, 100ms+ is where calls/games visibly suffer.
  bufferbloat: { good: 30, fair: 100, dir: 'low', unit: 'ms', labels: ['GOOD', 'FAIR', 'POOR'] },
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
function applyRating(rateId, value, key) {
  const rEl = document.getElementById(rateId);
  const lvl = rateLevel(value, key);
  if (!rEl) return;
  if (lvl == null) { rEl.className = 'rating'; rEl.textContent = ''; rEl.title = ''; }
  else {
    rEl.className = 'rating show ' + ['good', 'fair', 'poor'][lvl];
    rEl.textContent = THRESHOLDS[key].labels[lvl];
    // thresholds moved off the card face into the badge tooltip
    rEl.title = hintText(key);
  }
}
safely('metric ratings', function() {
  applyRating('rateUptime24', DATA.stats_24h.uptime_pct, 'uptime');
  applyRating('rateUptime7',  DATA.stats_7d.uptime_pct,  'uptime');
  applyRating('rateLatency',  DATA.stats_24h.avg_latency, 'latency');
  applyRating('rateDns',      DATA.dns_24h ? DATA.dns_24h.avg : null, 'dns');
  applyRating('rateJitter',   DATA.jitter_24h, 'jitter');
  // Bufferbloat: rated from the newest speed test that has loaded-latency
  // data (Ookla CLI only). delta = worst loaded latency minus idle ping.
  const loaded = (DATA.speed_series || []).filter(s => s.lat_down != null || s.lat_up != null);
  if (loaded.length) {
    const s = loaded[loaded.length - 1];
    const delta = Math.round(Math.max(s.lat_down || 0, s.lat_up || 0) - (s.ping || 0));
    applyRating('rateBufferbloat', delta, 'bufferbloat');
    const hintEl = document.getElementById('bufferbloatHint');
    if (hintEl && rateLevel(delta, 'bufferbloat') >= 1) {
      hintEl.style.display = '';
      hintEl.textContent = 'Latency climbs +' + delta + 'ms when the line is busy (bufferbloat) — '
        + 'enabling SQM / Smart Queue Management (QoS) on the router usually fixes this.';
    }
  }
});

// ---------- wifi channel advice + double-NAT hint ----------

// ---------- diagnosis banner: "what's wrong right now, in plain words" ----------
// Rule table, first match wins. Order mirrors the monitor's own causal
// precedence (a dead gateway suppresses the internet-down diagnosis: if
// your router is down, of course the internet behind it looks down too).
safely('diagnosis banner', function() {
  const banner = document.getElementById('diagBanner');
  const ongoing = kind => (DATA[kind] || []).filter(e => e.ongoing);
  const durTxt = e => e && e.start ? timeSince(e.start) : null;

  const openOutages = ongoing('outage_events');
  const byScope = s => openOutages.filter(e => e.scope === s);
  const matched = [];   // {cls, head, action, chip}

  // 1. This page itself is stale — trust nothing else on it.
  const ageMin = (Date.now() - Date.parse(DATA.generated_at)) / 60000;
  if (ageMin > 3) {
    matched.push({ cls: 'diag-warn',
      head: 'This page’s data is ' + Math.round(ageMin) + ' minutes old.',
      action: 'The dashboard generator may have stopped — check the NetMon Dashboard task on the monitor PC, then reload.' });
  }
  // 2. Monitoring paused: nothing is being measured right now.
  const gaps = DATA.monitoring_gaps || [];
  if (gaps.length && gaps[gaps.length - 1].ongoing) {
    matched.push({ cls: 'diag-warn',
      head: 'Monitoring is paused — nothing is being measured.',
      action: 'Wake the monitor PC or check the NetMon Monitor task; the network itself may be fine.' });
  }
  // 3. Gateway (main router) down → local problem, not the ISP. Except on
  //    combo-box installs (the gateway IS the ISP box — gateway.isp_name
  //    is set by the Python-side merge): there "not your ISP" would be
  //    exactly wrong, since the dead device is the ISP's own hardware.
  const gwIsp = DATA.gateway && DATA.gateway.isp_name;
  const gwOut = byScope('gateway')[0];
  if (gwOut || (DATA.gateway && DATA.gateway.status === 'down')) {
    matched.push({ cls: 'diag-crit',
      head: gwIsp
        ? 'The ISP’s box ("' + gwIsp + '") — which is also your main router — isn’t responding.'
        : 'Your main router isn’t responding — this is a problem in the house, not with your ISP.',
      action: gwIsp
        ? 'Power-cycle it (unplug 10 seconds, plug back in). Wi-Fi and internet will drop for ~2 minutes while it restarts — and if it doesn’t come back, that’s a call to the ISP: the box is their hardware.'
        : 'Power-cycle the main router (unplug 10 seconds, plug back in). Wi-Fi and internet will drop for ~2 minutes while it restarts.',
      chip: gwOut ? 'down for ' + durTxt(gwOut) : null });
  }
  // 4. Internet down while the gateway is fine → ISP.
  const netOut = byScope('internet')[0];
  if (netOut) {
    matched.push({ cls: 'diag-crit',
      head: 'The internet is down, but your own router is fine — this is your ISP’s problem.',
      action: 'Check the modem/ISP box lights, then report it. The outage log below is your evidence.',
      chip: 'down for ' + durTxt(netOut) });
  } else if (DATA.current_status === 'down') {
    // failing right now but not yet debounced into an event (3 checks)
    matched.push({ cls: 'diag-serious',
      head: 'Connection checks are failing right now…',
      action: 'Confirming it’s a real outage (takes under a minute). Refresh shortly.' });
  }
  // 5. DNS: pings fine, names dead — the "internet feels broken" classic.
  const dnsOut = byScope('dns')[0];
  if (dnsOut) {
    matched.push({ cls: 'diag-serious',
      head: 'Websites won’t load by name, even though the connection itself is up (DNS failure).',
      action: 'Set the router’s DNS servers to 1.1.1.1 and 8.8.8.8 — or reboot the main router.',
      chip: 'for ' + durTxt(dnsOut) });
  }
  // 6. One or more access points down (debounced events first, live
  //    router_summary as a softer fallback — that's a single ping sample).
  //    The ISP box gets its own wording: it's not "a spot with weak
  //    Wi-Fi", it's the whole house's uplink hardware.
  const ispName = (((DATA.router_summary || []).find(r => r.role === 'isp')) || {}).name;
  const routerOuts = byScope('router');
  const ispOut = ispName ? routerOuts.find(e => e.router_name === ispName) : null;
  if (ispOut) {
    matched.push({ cls: 'diag-serious',
      head: 'The ISP’s box ("' + ispName + '") isn’t responding.',
      action: 'If the internet is still up it may just be ignoring checks — but if this coincides with drops, power-cycle the ISP box; if it doesn’t come back, that’s a call to the ISP, not a router problem.',
      chip: 'down for ' + durTxt(ispOut) });
  }
  const apOuts = routerOuts.filter(e => e !== ispOut);
  if (apOuts.length) {
    const names = apOuts.map(e => e.router_name || 'a router').join(', ');
    matched.push({ cls: 'diag-serious',
      head: (apOuts.length === 1 ? 'Access point "' + names + '" is down.' : 'Access points down: ' + names + '.'),
      action: 'Power-cycle ' + (apOuts.length === 1 ? 'it' : 'them') + '. Wi-Fi near ' + (apOuts.length === 1 ? 'that spot' : 'those spots') + ' will be weak or dead until then.',
      chip: 'down for ' + durTxt(apOuts[0]) });
  } else if (!ispOut) {
    const softDown = (DATA.router_summary || []).filter(r => r.status === 'down');
    if (softDown.length) {
      matched.push({ cls: 'diag-warn',
        head: softDown.map(r => r.name).join(', ') + ' ' + (softDown.length === 1 ? 'is' : 'are') + ' not responding right now.',
        action: 'Might be a blip — if this banner is still here in a few minutes, power-cycle ' + (softDown.length === 1 ? 'it' : 'them') + '.' });
    }
  }
  // 7. Flapping: repeated micro-drops, none long enough to be an outage.
  //    Feels like "the internet keeps hiccuping" and used to be invisible.
  const flap = ongoing('instability_events')[0];
  if (flap) {
    matched.push({ cls: 'diag-serious',
      head: 'The connection is flapping — repeated brief drops in the last hour.',
      action: 'Each drop is seconds long, but together they point at an unstable line/modem. The blip log below is exactly the evidence ISPs ask for.',
      chip: 'flapping for ' + durTxt(flap) });
  }
  // 8. Degraded: up, but measurably worse than the thresholds.
  const degr = ongoing('degraded_events')[0];
  if (degr) {
    const parts = [];
    const s = DATA.stats_24h || {};
    if (s.loss_pct != null && rateLevel(s.loss_pct, 'loss') >= 1) parts.push('packet loss ' + s.loss_pct + '%');
    if (s.avg_latency != null && rateLevel(s.avg_latency, 'latency') >= 1) parts.push('latency ' + s.avg_latency + 'ms');
    if (DATA.jitter_24h != null && rateLevel(DATA.jitter_24h, 'jitter') >= 1) parts.push('jitter ' + DATA.jitter_24h + 'ms');
    matched.push({ cls: 'diag-warn',
      head: 'The connection is up but struggling' + (parts.length ? ' — ' + parts.join(', ') + ' (24h)' : '') + '.',
      action: 'If this keeps happening, the report below is evidence for your ISP.',
      chip: 'for ' + durTxt(degr) });
  }
  // 9. All clear.
  if (!matched.length) {
    if ((DATA.stats_24h || {}).uptime_pct == null) return;  // brand-new install, nothing to say yet
    matched.push({ cls: 'diag-ok',
      head: 'All systems healthy.',
      action: '24h uptime ' + DATA.stats_24h.uptime_pct + '%' +
        (DATA.current_latency != null ? ' · ' + Math.round(DATA.current_latency) + ' ms right now' : '') });
  }

  const top = matched[0];
  let html = '<span class="diag-head">' + escapeHtml(top.head) + '</span>'
           + '<span class="diag-action">' + escapeHtml(top.action || '') + '</span>';
  if (top.chip) html += '<span class="diag-chip">' + escapeHtml(top.chip) + '</span>';
  if (matched.length > 1) {
    html += '<span class="diag-also">Also: ' + matched.slice(1).map(m => escapeHtml(m.head)).join(' ') + '</span>';
  }
  banner.className = 'diag-banner ' + top.cls;
  banner.innerHTML = html;
  banner.style.display = '';
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
    paused: 'var(--muted)', wifi: 'var(--series-blue)', flap: 'var(--status-serious)',
    tgt: 'var(--series-blue)', iot: 'var(--series-blue)' };
  function classify(e) {
    let cat, label, badgeClass, rowClass, point = false;
    if (e.kind === 'outage' && e.scope === 'gateway') { cat='outage'; label='Gateway down'; badgeClass='badge-gateway'; rowClass='event-gateway'; }
    else if (e.kind === 'outage' && e.scope === 'router') { cat='outage'; label=(e.router_name||'Router')+' down'; badgeClass='badge-gateway'; rowClass='event-gateway'; }
    else if (e.kind === 'outage' && e.scope === 'dns') { cat='dns'; label='DNS failure'; badgeClass='badge-dns'; rowClass='event-dns'; }
    else if (e.kind === 'outage' && e.scope === 'target') { cat='tgt'; label=(e.router_name||'Target')+' unreachable'; badgeClass='badge-dns'; rowClass='event-dns'; }
    else if (e.kind === 'outage' && e.scope === 'iot') { cat='iot'; label=(e.router_name||'IoT device')+' down'; badgeClass='badge-iot'; rowClass='event-ipchange'; }
    else if (e.kind === 'outage') { cat='outage'; label='Internet down'; badgeClass='badge-internet'; rowClass='event-internet'; }
    else if (e.kind === 'degraded') { cat='slow'; label='Slow / degraded'; badgeClass='badge-degraded'; rowClass='event-degraded'; }
    else if (e.kind === 'instability') { cat='flap'; label='Flapping'; badgeClass='badge-degraded'; rowClass='event-degraded'; }
    else if (e.kind === 'new_device') { cat='device'; label='New device'; badgeClass='badge-newdevice'; rowClass='event-newdevice'; point=true; }
    else if (e.kind === 'wifi_roam') { cat='wifi'; label='Wi-Fi roamed'; badgeClass='badge-ipchange'; rowClass='event-ipchange'; point=true; }
    else if (e.kind === 'gap') { cat='paused'; label='Monitoring paused'; badgeClass='badge-gap'; rowClass='event-gap'; }
    else { cat='ip'; label='Public IP changed'; badgeClass='badge-ipchange'; rowClass='event-ipchange'; point=true; }
    return { ...e, cat, label, badgeClass, rowClass, point,
             startMs: Date.parse(e.start), endMs: e.end ? Date.parse(e.end) : NOW,
             durSecs: point ? 0 : durSecs(e), tlColor: TL_COLOR[cat] };
  }

  const allEvents = [
    ...DATA.outage_events.map(e => ({...e, kind: 'outage'})),
    ...DATA.degraded_events.map(e => ({...e, kind: 'degraded', scope: 'internet'})),
    ...(DATA.instability_events || []).map(e => ({...e, kind: 'instability', scope: 'internet'})),
    ...DATA.ip_change_events.map(e => ({...e, kind: 'ip_change', scope: 'internet', ongoing: false, duration: '—'})),
    ...(DATA.new_device_events || []).map(e => ({...e, kind: 'new_device', scope: 'lan', ongoing: false, duration: '—'})),
    ...(DATA.wifi_roam_events || []).map(e => ({...e, kind: 'wifi_roam', scope: 'lan', ongoing: false, duration: '—'})),
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

  const blips = DATA.blips || [];
  // chips carry a log-filter category — clicking one activates the
  // matching filter pill below (only when that pill exists)
  const chips = [
    { k: 'Outages · 7d', v: String(hardDown.length), s: hardDown.length ? 'reachability failures' : 'none — all clear',
      cls: hardDown.length ? 'bad' : 'good', cat: 'outage' },
    { k: 'Total downtime', v: fmtDur(downtime), s: 'summed outage time', cls: downtime > 0 ? 'bad' : 'good', cat: 'outage' },
    { k: 'Longest outage', v: longest ? fmtDur(longest) : '—', s: 'single worst event', cls: longest ? 'warn' : 'good', cat: 'outage' },
    { k: 'Most affected', v: worst || '—', s: worst ? worstN + (worstN === 1 ? ' outage' : ' outages') : (slowCount ? slowCount + ' slow spells' : 'nothing'),
      cls: worst ? 'warn' : 'good', cat: worst === 'DNS' ? 'dns' : 'outage' },
    // micro-drops: recovered before the outage threshold, so they appear
    // nowhere else — yet frequent blips ARE the unstable-line signature
    { k: 'Blips · 7d', v: String(blips.length),
      s: blips.length ? (DATA.blips_24h || 0) + ' in last 24h — drops too brief to be outages' : 'no micro-drops either',
      cls: blips.length ? 'warn' : 'good', cat: 'flap' },
  ];
  document.getElementById('outageSummary').innerHTML = chips.map(c =>
    `<div class="osum ${c.cls}"${c.cat ? ` data-cat="${c.cat}" title="Click to filter the log"` : ''}><div class="k">${c.k}</div><div class="v">${escapeHtml(c.v)}</div><div class="s">${escapeHtml(c.s)}</div></div>`
  ).join('');
  document.getElementById('outageSummary').addEventListener('click', (ev) => {
    const chip = ev.target.closest('[data-cat]');
    if (!chip) return;
    const btn = filtersWrap.querySelector(`.ofilter[data-cat="${chip.dataset.cat}"]`);
    if (btn) { btn.click(); btn.scrollIntoView({ behavior: 'smooth', block: 'center' }); }
  });

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
    svg += `<rect class="tl-ev" data-ev="${e.startMs}|${e.cat}" x="${x0.toFixed(1)}" y="${TOP + 3}" width="${w.toFixed(1)}" height="${TRACK_H - 6}" rx="2" fill="${e.tlColor}" style="color:${e.tlColor}" opacity="${op}"><title>${escapeHtml(title)}</title></rect>`;
  });
  // point events (new device / IP change) as ticks
  winEvents.filter(e => e.point).forEach(e => {
    const x = xFor(e.startMs);
    const title = `${e.label} · ${new Date(e.startMs).toLocaleString()}`;
    svg += `<g fill="${e.tlColor}" style="color:${e.tlColor}" data-ev="${e.startMs}|${e.cat}"><rect class="tl-ev" x="${(x - 1).toFixed(1)}" y="${TOP + 2}" width="2" height="${TRACK_H - 4}"/><circle class="tl-ev" cx="${x.toFixed(1)}" cy="${TOP - 1}" r="3"/><title>${escapeHtml(title)}</title></g>`;
  });
  // blips: short amber ticks hugging the bottom edge of the track (point
  // events already own the top edge). One tick per micro-drop.
  blips.filter(b => Date.parse(b.t) >= WIN_START).forEach(b => {
    const x = xFor(Date.parse(b.t));
    const title = `Blip · ${new Date(Date.parse(b.t)).toLocaleString()} · ${b.checks} failed check${b.checks === 1 ? '' : 's'} (${b.cls})`;
    svg += `<rect class="tl-ev" x="${(x - 1).toFixed(1)}" y="${BOT - 9}" width="2" height="8" fill="var(--status-warning)" style="color:var(--status-warning)" opacity="0.9"><title>${escapeHtml(title)}</title></rect>`;
  });
  // "now" marker
  svg += `<line class="tl-now" x1="${RIGHT}" y1="${TOP - 6}" x2="${RIGHT}" y2="${BOT + 4}"/>`;
  // clicking a timeline event jumps to (and flashes) its row in the log
  tlWrap.addEventListener('click', (ev) => {
    const el = ev.target && ev.target.closest ? ev.target.closest('[data-ev]') : null;
    if (!el) return;
    jumpToEvent(el.getAttribute('data-ev'));
  });
  function jumpToEvent(key) {
    const cat = key.split('|')[1];
    // the row only exists under a filter that includes it
    if (activeCat !== 'all' && activeCat !== cat) {
      activeCat = 'all';
      filtersWrap.querySelectorAll('.ofilter').forEach(b => b.classList.toggle('active', b.dataset.cat === 'all'));
      renderList();
    }
    const row = outWrap.querySelector(`tr[data-ev="${key}"]`);
    if (!row) return;
    if (row.style.display === 'none') {
      const t = document.getElementById('eventsToggle');
      if (t) t.click();   // it's in the collapsed "older" set — expand
    }
    if (row.scrollIntoView) row.scrollIntoView({ behavior: 'smooth', block: 'center' });
    row.classList.remove('flash');
    void row.offsetWidth;   // restart the animation on repeat clicks
    row.classList.add('flash');
  }
  svg += `<text class="tl-nowlab" x="${RIGHT}" y="${TOP - 9}" text-anchor="end">NOW</text>`;
  if (winEvents.length === 0) {
    svg += `<text x="500" y="${TOP + TRACK_H / 2 + 4}" text-anchor="middle" fill="var(--status-good)" font-size="12" font-family="var(--font-mono)">No incidents in the last 7 days</text>`;
  }
  svg += `</svg>`;
  tlWrap.innerHTML = svg;

  // timeline colour legend
  const legendKeys = [['Down', 'var(--status-critical)'], ['DNS', 'var(--status-serious)'],
    ['Slow', 'var(--status-warning)'], ['Blip', 'var(--status-warning)'],
    ['New device', 'var(--status-good)'], ['Paused', 'var(--muted)']];
  document.getElementById('timelineLegend').innerHTML = legendKeys.map(([n, c]) =>
    `<span class="tlk"><i style="background:${c}"></i>${n}</span>`).join('');

  // ---- filter chips ----
  const CAT_NAMES = { outage: 'Outages', dns: 'DNS', tgt: 'Targets', iot: 'IoT', slow: 'Slow', flap: 'Flapping', device: 'Devices', ip: 'IP changes', wifi: 'Wi-Fi roams', paused: 'Paused' };
  const CAT_ORDER = ['outage', 'dns', 'tgt', 'iot', 'slow', 'flap', 'device', 'ip', 'wifi', 'paused'];
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
  const EVENTS_SHOWN = 8;
  let activeCat = 'all';

  // Render a flight-recorder snapshot in plain terms. Every field is
  // optional — partial captures render whatever they managed to grab.
  function evidenceHtml(ev) {
    const parts = [];
    if (ev.dns) {
      const line = Object.keys(ev.dns).map(k => {
        const v = ev.dns[k] || {};
        const nm = k === 'gateway' ? 'router' : k;
        return `<span class="${v.ok ? 'ev-ok' : 'ev-bad'}">${escapeHtml(nm)} ${v.ok ? '✓' + (v.ms != null ? ' ' + v.ms + 'ms' : '') : '✗'}</span>`;
      }).join(' · ');
      parts.push('<div><span class="ev-k">DNS at failure</span> ' + line + '</div>');
    }
    if (ev.gateway_ping) {
      const g = ev.gateway_ping;
      const ok = (g.received || 0) > 0;
      parts.push(`<div><span class="ev-k">Gateway</span> <span class="${ok ? 'ev-ok' : 'ev-bad'}">${g.received}/${g.sent} pings answered${g.avg_ms != null ? ', ' + g.avg_ms + 'ms' : ''}</span></div>`);
    }
    if (ev.routers_alive) {
      const line = Object.keys(ev.routers_alive).map(n => {
        const s = ev.routers_alive[n];
        const ok = s === 'ping' || s === 'web' || s === 'probe' || s === true;
        return `<span class="${ok ? 'ev-ok' : 'ev-bad'}">${escapeHtml(n)} ${ok ? '✓' : '?'}</span>`;
      }).join(' · ');
      parts.push('<div><span class="ev-k">Routers</span> ' + line + ' <span class="ev-note">(? = answered nothing in a hurry — not proof it was down)</span></div>');
    }
    if (ev.traceroute && ev.traceroute.length) {
      // NB: double backslash — this JS lives in a Python string; a lone
      // backslash-n would reach the browser as a real newline mid-string
      parts.push('<div><span class="ev-k">Traceroute (where the path stopped)</span><pre class="ev-trace">'
        + ev.traceroute.map(escapeHtml).join('\\n') + '</pre></div>');
    } else if (ev.traceroute_error) {
      parts.push('<div><span class="ev-k">Traceroute</span> <span class="ev-bad">failed to run</span></div>');
    }
    const cap = [];
    if (ev.captured_ts) cap.push('captured ' + new Date(Date.parse(ev.captured_ts)).toLocaleString());
    if (ev.capture_secs != null) cap.push('took ' + ev.capture_secs + 's');
    if (cap.length) parts.push('<div class="ev-note">' + escapeHtml(cap.join(' · ')) + '</div>');
    return '<div class="ev-body">' + (parts.join('') || '<span class="ev-note">snapshot was empty</span>') + '</div>';
  }

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
      // flight-recorder snapshot (captured in the outage's first seconds):
      // a toggle in the detail cell + a collapsed full-width row under it
      const evBtn = e.evidence ? ` <button class="ev-btn" data-evtoggle="${idx}">evidence</button>` : '';
      const evRow = e.evidence ? `<tr class="ev-row" data-evrow="${idx}"${idx >= EVENTS_SHOWN ? ' data-extra="1"' : ''} style="display:none">
        <td colspan="4">${evidenceHtml(e.evidence)}</td>
      </tr>` : '';
      return `<tr class="event-row ${e.rowClass}" data-ev="${e.startMs}|${e.cat}"${hidden}>
        <td>${new Date(e.startMs).toLocaleString()}<span class="rel"> · ${timeSince(e.startMs)} ago</span></td>
        <td>${badge}</td>
        <td>${dur}</td>
        <td class="mono">${escapeHtml(e.note)}${evBtn}</td>
      </tr>${evRow}`;
    }).join('');
    const extraCount = rowsData.length - EVENTS_SHOWN;
    const moreBtn = extraCount > 0
      ? `<div style="text-align:center; margin-top:10px;"><button id="eventsToggle" class="ghost-btn">Show ${extraCount} older</button></div>`
      : '';
    outWrap.innerHTML = `<div class="list-scroll"><table><thead><tr><th>Started</th><th>Type</th><th>Duration</th><th>Detail</th></tr></thead><tbody>${rows}</tbody></table></div>${moreBtn}`;
    if (extraCount > 0) {
      let expanded = false;
      document.getElementById('eventsToggle').addEventListener('click', () => {
        expanded = !expanded;
        outWrap.querySelectorAll('tr[data-extra]').forEach(tr => {
          // evidence rows stay closed until their own toggle opens them;
          // collapsing the list closes any that were open
          if (tr.hasAttribute('data-evrow')) { if (!expanded) tr.style.display = 'none'; return; }
          tr.style.display = expanded ? '' : 'none';
        });
        // capped scroll box so expanding 30+ events doesn't balloon the page
        outWrap.querySelector('.list-scroll').classList.toggle('expanded', expanded);
        document.getElementById('eventsToggle').textContent = expanded ? 'Show fewer' : `Show ${extraCount} older`;
      });
    }
  }
  renderList();

  // evidence toggles (delegated: rows regenerate on every filter change)
  outWrap.addEventListener('click', (ev) => {
    const btn = ev.target.closest('.ev-btn');
    if (!btn) return;
    const row = outWrap.querySelector(`tr[data-evrow="${btn.dataset.evtoggle}"]`);
    if (row) row.style.display = row.style.display === 'none' ? '' : 'none';
  });

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
    const scanCmd = (DATA.checks && DATA.checks.device_cmd) || 'device scan';
    const scanEff = checkEff('devices');
    const scanFreq = (scanEff.approx ? '~' : '') + freqShort(scanEff.freq);
    let note = online + ' online · ' + total + ' seen this week · '
      + scanCmd + ' · scanned ' + timeSince(DATA.last_device_scan_ts) + ' ago · every ' + scanFreq;
    if (ageMin > Math.max(15, scanEff.freq * 3 / 60)) {
      note += ' — stale! scans should run every ' + scanFreq + '; check the monitor service';
      noteEl.style.color = 'var(--status-critical)';
      noteEl.style.fontWeight = '700';
    }
    noteEl.textContent = note;
  }
});
function renderDevices(query) {
  if (!DATA.devices || DATA.devices.length === 0) {
    devWrap.innerHTML = '<div class="empty">No device scan data yet.</div>';
    return;
  }
  const NEW_CUTOFF = (Date.parse(DATA.generated_at) || Date.now()) - 24 * 3600 * 1000;
  const deviceRow = (d, hidden) => {
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
    // MAC as a muted sub-line under the name — visible without adding a
    // column (a MAC column was the widest cell and forced side-scrolling
    // on phones, which is why it was banished to a tooltip for a while)
    const macLine = d.mac ? `<span class="dev-mac">${escapeHtml(d.mac)}</span>` : '';
    // devices.json type (camera/printer/...) as a small tag — the device
    // also appears in the IoT section above, but stays here so the scan
    // counts and the devices-online chart keep matching the table
    if (d.type) label += `<span class="dev-type">${escapeHtml(d.type)}</span>`;
    // first_seen inside the last 24h = joined (or returned) recently.
    // Window-clamped first_seen can't false-positive here: a long-present
    // device's clamp sits at the 7d edge, far outside 24h.
    if (d.first_seen && Date.parse(d.first_seen) >= NEW_CUTOFF) label += '<span class="dev-new">new</span>';
    return `<tr${hidden ? ' style="display:none" data-away="1"' : ''}>
    <td><div class="device-name"><span class="device-icon">${deviceIcon}</span><span class="dev-id"><span>${label}</span>${macLine}</span></div></td>
    <td class="mono">${escapeHtml(d.ip)}</td>
    <td>${pill}</td>
    <td>${seen}</td>
  </tr>`;
  };
  // live filter: name / hostname / IP / MAC substring; a query shows every
  // match regardless of the away-collapse (an away device is exactly what
  // a search is usually hunting for)
  const qn = (query || '').trim().toLowerCase();
  const match = d => !qn || [d.name, d.hostname, d.ip, d.mac].some(v => v && String(v).toLowerCase().indexOf(qn) !== -1);
  const shown = DATA.devices.filter(match);
  if (shown.length === 0) {
    devWrap.innerHTML = '<div class="empty">No devices match “' + escapeHtml(query) + '”.</div>';
    return;
  }
  // online devices up front; away ones collapsed behind a toggle so 20
  // idle phones don't add 900px of table
  const online = shown.filter(d => d.online);
  const away = shown.filter(d => !d.online);
  const collapseAway = !qn && online.length > 0 && away.length > 0;
  const rows = online.map(d => deviceRow(d, false)).join('')
    + away.map(d => deviceRow(d, collapseAway)).join('');
  const awayBtn = collapseAway
    ? `<div style="text-align:center; margin-top:10px;"><button id="awayToggle" class="ghost-btn">Show ${away.length} away device${away.length === 1 ? '' : 's'}</button></div>`
    : '';
  devWrap.innerHTML = `<table><thead><tr><th>Device</th><th>IP</th><th>Status</th><th>Last seen</th></tr></thead><tbody>${rows}</tbody></table>${awayBtn}`;
  if (collapseAway) {
    let awayShown = false;
    document.getElementById('awayToggle').addEventListener('click', () => {
      awayShown = !awayShown;
      devWrap.querySelectorAll('tr[data-away]').forEach(tr => { tr.style.display = awayShown ? '' : 'none'; });
      document.getElementById('awayToggle').textContent = awayShown
        ? 'Hide away devices' : `Show ${away.length} away device${away.length === 1 ? '' : 's'}`;
    });
  }
}
safely('devices table', function() { renderDevices(''); });
safely('devices search', function() {
  const box = document.getElementById('devSearch');
  if (!box) return;
  box.addEventListener('input', () => renderDevices(box.value));
});

// ---------- quick-nav ----------
// The page is ~5 screens tall; once the command deck is scrolled past, a
// slim jump bar slides in so Outages/Devices are one click, not a hunt.
safely('quick nav', function() {
  // both bars (the always-visible static one under the topbar and the
  // fixed one that slides in when scrolled) share the click handling
  document.querySelectorAll('.quick-nav').forEach(nav => {
    nav.addEventListener('click', (e) => {
      const b = e.target.closest('button');
      if (!b) return;
      const sec = document.getElementById(b.dataset.goto);
      if (sec && sec.scrollIntoView) sec.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
  });
  const fixedNav = document.getElementById('quickNav');
  if (!fixedNav) return;
  let shown = false;
  window.addEventListener('scroll', () => {
    const want = (window.scrollY || 0) > 480;
    if (want !== shown) { shown = want; fixedNav.classList.toggle('show', shown); }
  }, { passive: true });
});

// ---------- IoT devices ----------
safely('iot devices', function() {
  // Typed/watched devices from devices.json. Watched rows carry live
  // liveness from the iot thread (same 4-tier ladder as routers, so the
  // same "Online · web" / "Online · silent" decoding applies); unwatched
  // rows ride the device-scan census. Section hidden when nothing is
  // tagged — same pattern as the custom-targets card.
  const list = DATA.iot_devices || [];
  const card = document.getElementById('iotCard');
  if (!card || !list.length) return;
  card.style.display = '';
  const cols = document.getElementById('devCols');
  if (cols) cols.classList.remove('no-iot');
  const watched = list.filter(d => d.watch);
  const downN = watched.filter(d => d.status === 'down').length;
  document.getElementById('iotNote').textContent =
    list.length + ' device' + (list.length === 1 ? '' : 's') + ' · ' + watched.length + ' watched'
    + (downN ? ' · ' + downN + ' unreachable' : '');

  function pill(d) {
    if (d.watch) {
      if (d.status === 'up') {
        const silent = d.method === 'probe' || d.method === 'arp';
        const txt = d.method === 'tcp' ? 'Online · web' : silent ? 'Online · silent' : 'Online';
        // same plain-language decoder as the map hover cards
        const tip = d.method === 'tcp' ? 'Alive via its web port — it ignores pings (common)'
          : d.method === 'probe' ? 'Proven alive each check: it refuses a closed-port probe. Ignores pings — normal'
          : d.method === 'arp' ? 'Seen answering network-presence (ARP) checks. Ignores pings — normal for stealthy devices'
          : 'Answers ping normally';
        return '<span class="status-pill small ' + (silent ? 'status-silent-pill' : 'status-up')
          + '" title="' + escapeHtml(tip) + '"><span class="status-dot"></span>' + txt + '</span>';
      }
      if (d.status === 'down') {
        return '<span class="status-pill small status-down"><span class="status-dot"></span>Down</span>';
      }
      return '<span class="status-pill small" style="background:var(--border-soft);color:var(--muted)">Never seen</span>';
    }
    return d.online
      ? '<span class="status-pill small status-up"><span class="status-dot"></span>Online</span>'
      : '<span class="status-pill small" style="background:var(--border-soft);color:var(--muted)"><span class="status-dot"></span>Away</span>';
  }
  function lastCol(d) {
    if (d.watch) {
      if (!d.last_check) return 'waiting for first check';
      const lat = d.latency != null ? Math.round(d.latency) + ' ms · ' : '';
      return lat + timeSince(d.last_check) + ' ago';
    }
    if (d.online) return 'now';
    return d.last_seen ? timeSince(d.last_seen) + ' ago' : '—';
  }
  // Dense chip grid (was a 4-column table with a full-width group-header
  // row per type — it ate a screen of height for a handful of devices).
  // One compact chip per device: status dot + name + type tag on top,
  // latency/last-seen line below; MAC + IP live in the hover title.
  const chip = d => {
    const watchTag = d.watch ? '' : '<span class="dev-type" title="Not actively watched — status comes from the periodic device scan. Tick Watch in Settings → Devices for live checks.">scan only</span>';
    const typeTag = '<span class="dev-type">' + escapeHtml(d.type || 'other') + '</span>';
    const title = escapeHtml(d.mac + (d.ip ? ' · ' + d.ip : ''));
    return '<div class="iot-chip" title="' + title + '">'
      + '<div class="iot-chip-top"><span class="dev-id"><span><b>' + escapeHtml(d.name) + '</b>' + typeTag + watchTag + '</span></span>' + pill(d) + '</div>'
      + '<div class="iot-chip-sub">' + escapeHtml(d.ip || '—') + ' · ' + lastCol(d) + '</div>'
      + '</div>';
  };
  document.getElementById('iotTableWrap').innerHTML =
    '<div class="iot-grid">' + list.map(chip).join('') + '</div>';
});

// ---------- house map ----------
// Named (not an anonymous safely block) so the resize listener below can
// re-render when the window crosses the compact/desktop threshold — a
// phone rotate used to need a full page reload to switch layouts.
function renderHouseMap() {
  const wrap = document.getElementById('houseMapWrap');
  // The ISP box (role:'isp' in routers.json) is not an AP on a floor —
  // topologically it sits BETWEEN the internet and the main router, so it
  // renders as a utility box on the house wall where the fiber enters,
  // never as a floor pill. Combo-box installs (the box IS the gateway)
  // never reach here: the Python side drops that row from router_summary
  // and sets gateway.isp_name instead, so the fiber runs straight into
  // the Main Router pill rather than drawing one device as two nodes.
  const ispBox = (DATA.router_summary || []).find(r => r.role === 'isp');
  const routers = (DATA.router_summary || []).filter(r => r.role !== 'isp');
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
  // Phones: the 1000-unit-wide scene scales down to <300 CSS px, which
  // renders every label at ~4px. Below ~520px we switch to a compact
  // portrait layout: a ~330-unit viewBox (text keeps ~90% of its size),
  // pills packed into centered rows, and each floor's band grows to fit
  // its rows — the "auto-scaling floor height" mode.
  const compact = (wrap.clientWidth || 900) < 520;
  renderHouseMap.lastCompact = compact;
  const W = compact ? 330 : 1000;
  const TOP = compact ? 30 : 96, BH = 172; // wall top, desktop floor band height
  const HX0 = compact ? 14 : 180, HX1 = compact ? W - 14 : 910;  // house walls
  const CARD_W = 176, CARD_H = 106;       // hover-card size (was the always-on card)
  const mainKey = (H.main_floor && floorNames.includes(H.main_floor))
    ? H.main_floor : floorNames[Math.floor(floorNames.length / 2)];

  // Compact pill: status dot + name. Everything else (IP, latency, uptime)
  // lives in the hover card so the house doesn't read as a wall of boxes.
  // (Defined before the floor build — compact floors are sized from pill
  // widths.) Compact bumps the per-char estimate with the CSS font bump.
  const pillLabel = name => name.length > 20 ? name.slice(0, 19) + '…' : name;
  const pillW = (name, main) => 34 + pillLabel(name).length * (main ? 7.2 : 6.6) * (compact ? 1.1 : 1);

  let FLOORS;
  if (!compact) {
    FLOORS = floorNames.map((k, i) => ({ key: k, y0: TOP + i * BH, y1: TOP + (i + 1) * BH }));
  } else {
    // pack each floor's pills into rows (main router gets its own row,
    // centered), then size the floor band to the rows it holds
    const maxRowW = HX1 - HX0 - 24;
    let y = TOP;
    FLOORS = floorNames.map(k => {
      const rows = [];
      if (gw && k === mainKey) rows.push([{ __main: true, name: 'Main Router', main: true }]);
      let cur = [], wsum = 0;
      routers.filter(r => r.floor === k).forEach(r => {
        const pw = pillW(r.name, false) + 14;
        if (cur.length && wsum + pw > maxRowW) { rows.push(cur); cur = []; wsum = 0; }
        cur.push(r); wsum += pw;
      });
      if (cur.length) rows.push(cur);
      const h = Math.max(86, 38 + rows.length * 48 + 8);
      const f = { key: k, y0: y, y1: y + h, rows };
      y = f.y1;
      return f;
    });
  }
  const houseBottom = FLOORS[FLOORS.length - 1].y1;
  // ground level sits above the first underground floor; if there is no
  // underground floor, it's the bottom of the house
  let groundY = houseBottom;
  for (const f of FLOORS) { if (UG.has(f.key)) { groundY = f.y0; break; } }
  // Desktop: house sits right of center — the earth on the left hosts the
  // buried fiber uplink, so the map shows the whole chain internet ->
  // router -> APs. Compact: no yard, so the fiber node moves below the
  // house instead. Guarantee enough underground depth for it either way.
  const mainFloor = FLOORS.find(f => f.key === mainKey);
  const MAIN = { x: compact ? (HX0 + HX1) / 2 : 545, y: (mainFloor.y0 + mainFloor.y1) / 2 };
  // Compact + ISP box: push the internet node further down so the wall box
  // fits between the foundation and the node without covering any pills.
  const NET = { x: compact ? 80 : 88,
                y: (compact ? Math.max(groundY, houseBottom) + (ispBox ? 96 : 0) : groundY) + 52 };
  // The ISP box mounts ON the wall: desktop = straddling the left wall
  // just above the street datum (where a real ONT/meter hangs); compact =
  // between the foundation and the internet node.
  const ISP = ispBox
    ? (compact ? { x: NET.x, y: houseBottom + 44 } : { x: HX0, y: groundY - 44 })
    : null;
  const totalH = Math.max(houseBottom + 35, NET.y + 70);
  const netUp = DATA.current_status !== 'down';

  const wifiIcon = (cx, cy, scale) => `
    <g transform="translate(${cx},${cy}) scale(${scale}) translate(-12,-13)" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round">
      <path d="M5 12.55a11 11 0 0 1 14.08 0"/>
      <path d="M8.53 16.11a6 6 0 0 1 6.95 0"/>
      <circle cx="12" cy="20" r="1.6" fill="currentColor" stroke="none"/>
    </g>`;

  const spread = (n, a, b) => Array.from({length: n}, (_, i) => a + (i + 1) * (b - a) / (n + 1));
  const linspace = (n, a, b) => n === 1 ? [(a + b) / 2] : Array.from({length: n}, (_, i) => a + i * (b - a) / (n - 1));

  // place each router on its floor. Desktop: the main router's floor
  // splits pills left/right of it so nothing overlaps, and the right edge
  // reserves room for the windows. Compact: walk the pre-packed rows.
  const placed = [], unplaced = [];
  if (!compact) {
    const EDGE = HX0 + 95, EDGE2 = HX1 - 185;
    FLOORS.forEach(f => {
      let rs = routers.filter(r => r.floor === f.key);
      let xs;
      if (f.key === mainFloor.key) {
        // this floor hosts the main router — split its routers left/right
        const leftRs = [], rightRs = [];
        rs.forEach((r, i) => (i % 2 === 0 ? leftRs : rightRs).push(r));
        const leftXs = spread(leftRs.length, HX0 + 24, MAIN.x - 140);
        const rightXs = spread(rightRs.length, MAIN.x + 140, EDGE2 + 70);
        rs = [...leftRs, ...rightRs];
        xs = [...leftXs, ...rightXs];
      } else {
        xs = linspace(rs.length, EDGE, EDGE2);
      }
      const cy = (f.y0 + f.y1) / 2 + 8;  // nudge down to clear the floor label
      rs.forEach((r, i) => placed.push({ ...r, x: xs[i], y: cy }));
    });
  } else {
    FLOORS.forEach(f => {
      (f.rows || []).forEach((row, ri) => {
        const cy = f.y0 + 38 + ri * 48 + 24;
        const widths = row.map(r => pillW(r.name, r.main));
        const total = widths.reduce((a, b) => a + b, 0) + (row.length - 1) * 14;
        let x = (HX0 + HX1) / 2 - total / 2;
        row.forEach((r, i) => {
          const cx = x + widths[i] / 2;
          x += widths[i] + 14;
          if (r.__main) { MAIN.x = cx; MAIN.y = cy; }
          else placed.push({ ...r, x: cx, y: cy });
        });
      });
    });
  }
  routers.forEach(r => { if (!FLOORS.some(f => f.key === r.floor)) unplaced.push(r); });

  const fmtPct = v => v != null ? v + '%' : '—';
  const fmtMs = v => v != null ? v + ' ms' : '—';

  // Where the monitor PC hangs (config monitor_location): that node's
  // hover card carries the speed-test readout — the speed belongs to the
  // path measured, not to the internet cloud. 'main'/'isp'/'router' says
  // which drawn node hosts it; null = not configured / name not drawn
  // (then the internet node keeps the speed line as before).
  const MONLOC = DATA.monitor_location;
  const lastSpeed = (DATA.speed_series || []).slice(-1)[0];
  const speedHost = MONLOC
    ? (gw && (MONLOC === 'Main Router' || MONLOC === gw.isp_name) ? 'main'
       : (ispBox && ispBox.name === MONLOC) ? 'isp'
       : placed.some(p => p.name === MONLOC) ? 'router' : null)
    : null;

  // One info card per router: name+status, IP, latency, and a 24h-uptime
  // bar — the same details as the table, readable at a glance on the map.
  // opts.showSpeed appends the monitor-vantage speed block (+30 tall).
  const cardH = opts => CARD_H + (opts.showSpeed && lastSpeed && lastSpeed.down != null ? 30 : 0);
  function card(x, y, opts) {
    const w = opts.main ? CARD_W + 16 : CARD_W, h = cardH(opts);
    const x0 = x - w / 2, y0 = y - h / 2;
    const cls = nodeCls(opts);
    // 'tcp' = alive via its web port; 'probe' = alive but silent, proven
    // fresh each cycle by a closed-port RST; 'arp' = alive but silent,
    // only the lingering ARP cache vouches for it (firewall ate the probe)
    const statusTxt = opts.status === 'up'
      ? (opts.method === 'tcp' ? 'Online · web'
         : (opts.method === 'probe' || opts.method === 'arp') ? 'Online · silent' : 'Online')
      : 'Offline';
    // plain-language decoder for the status jargon, as a native tooltip
    const statusTip = opts.status !== 'up' ? '' : (
      opts.method === 'tcp' ? 'Alive via its web admin port — it ignores pings (common for routers)'
      : opts.method === 'probe' ? 'Proven alive each check: it actively refuses a closed-port probe. Ignores pings — normal, not a fault'
      : opts.method === 'arp' ? 'Seen answering network-presence (ARP) checks. Ignores pings entirely — a stealth firewall, normal for some access points'
      : 'Answers ping normally');
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
      <text class="card-status" x="${x0 + 27}" y="${y0 + 54}">${statusTxt}${statusTip ? `<title>${escapeHtml(statusTip)}</title>` : ''}</text>
      ${opts.chartable ? `<text class="card-link" x="${x0 + w - 12}" y="${y0 + 37}" text-anchor="end" data-chartlink="${escapeHtml(opts.name)}">chart &#8595;</text>` : ''}
      <text class="card-stats" x="${x0 + w - 12}" y="${y0 + 54}" text-anchor="end">${fmtMs(opts.avg_latency)}</text>
      <rect class="bar-track" x="${x0 + 14}" y="${y0 + 63}" width="${barW}" height="4" rx="2"/>
      ${pctv != null ? `<rect class="bar-fill" x="${x0 + 14}" y="${y0 + 63}" width="${Math.min(fillW, barW)}" height="4" rx="2"/>` : ''}
      <text class="card-stats" x="${x0 + 14}" y="${y0 + 80}">24h uptime ${fmtPct(pctv)}</text>
      <line class="card-div" x1="${x0 + 14}" y1="${y0 + 87}" x2="${x0 + w - 14}" y2="${y0 + 87}"/>
      <text class="card-foot" x="${x0 + 14}" y="${y0 + 99}" data-checkfoot="1" data-fmt="mid"
        data-cmd="${methodCmd(opts.method)}" data-freq="${footEff(opts).freq}"${footEff(opts).approx ? ' data-approx="1"' : ''}${opts.last_check ? ` data-ts="${opts.last_check}"` : ''}></text>
      ${opts.showSpeed && lastSpeed && lastSpeed.down != null ? `
      <line class="card-div" x1="${x0 + 14}" y1="${y0 + 106}" x2="${x0 + w - 14}" y2="${y0 + 106}"/>
      <text class="card-stats" x="${x0 + 14}" y="${y0 + 119}">&#8595;${Math.round(lastSpeed.down)} &#8593;${lastSpeed.up != null ? Math.round(lastSpeed.up) : '—'} Mbps · monitor PC</text>
      <text class="card-foot" x="${x0 + 14}" y="${y0 + 131}" data-checkfoot="1" data-fmt="mid"
        data-cmd="speedtest" data-freq="${checkEff('speed').freq}"${checkEff('speed').approx ? ' data-approx="1"' : ''}${(DATA.checks || {}).speed ? ` data-ts="${(DATA.checks || {}).speed}"` : ''}></text>` : ''}
    </g>`;
  }
  // how the router was reached on its last check — this doubles as the
  // plain-language decoder for "Online · silent": "rst probe" = proven
  // alive this cycle by a closed-port refusal; "arp state" (Windows) =
  // the neighbor cache confirmed an ARP answer this cycle; "arp cache"
  // (mac/linux) = only the lingering table, device-sweep freshness
  function methodCmd(m) {
    return m === 'tcp' ? 'tcp 80/443' : m === 'probe' ? 'rst probe'
         : m === 'arp' ? (((DATA.checks || {}).arp_cmd) || 'arp cache') : 'ping';
  }
  // silent routers (RST-probe or ARP-cache tier) get their own muted
  // green so the map distinguishes "answers ping" from "merely alive"
  function nodeCls(opts) {
    if (opts.main) return 'node-main';
    if (opts.status !== 'up') return 'node-down';
    return (opts.method === 'arp' || opts.method === 'probe') ? 'node-silent' : 'node-up';
  }
  function footEff(opts) {
    // arp-tier on mac/linux: evidence only refreshes with the device
    // sweep; on Windows the state read is per-cycle, so fall through to
    // the router cadence like every other method
    if (opts.method === 'arp' && ((DATA.checks || {}).arp_cmd) !== 'arp state') return checkEff('devices');
    if (opts.main) return checkEff('ping');                 // gateway rides the ping thread
    // same configured-is-the-floor rule as checkEff: per-router measured
    // cadence below the configured router interval is stale history
    const rf = ((DATA.checks || {}).freq || {}).router || 15;
    if (opts.measured && opts.measured >= rf) return { freq: opts.measured, approx: true };
    return { freq: rf, approx: false };
  }

  function pill(x, y, opts, hcId) {
    const label = pillLabel(opts.name);
    const w = pillW(opts.name, opts.main), h = (opts.main ? 30 : 26) + (compact ? 4 : 0);
    const x0 = x - w / 2, y0 = y - h / 2;
    const cls = nodeCls(opts);
    return `<g class="pillgrp ${cls}" data-hc="${hcId}">
      <rect class="pill-box" x="${x0}" y="${y0}" width="${w}" height="${h}" rx="${h / 2}"/>
      <circle class="status-dot-svg" cx="${x0 + 14}" cy="${y}" r="4"/>
      <text class="pill-name" x="${x0 + 24}" y="${y + 4}"${opts.main ? ` style="font-size:${compact ? 13 : 12}px"` : ''}>${escapeHtml(label)}</text>
    </g>`;
  }
  // Hover card position: above the pill when there's room, else below.
  // The whole card is scaled up around its center: at the 1000-unit
  // desktop viewBox the raw card text landed around 8-9 real pixels —
  // technically dense, practically squinting. Scaling the group keeps
  // every proportion and lets card() keep its unit-space layout.
  const CARD_SCALE = compact ? 1.15 : 1.3;
  function hovercard(p, opts, hcId) {
    const hk = cardH(opts) * CARD_SCALE, wk = (CARD_W + (opts.main ? 16 : 0)) * CARD_SCALE;
    const above = p.y - 14 - hk > TOP - 30;
    const cy = above ? p.y - 20 - hk / 2 : p.y + 20 + hk / 2;
    const cx = Math.min(Math.max(p.x, wk / 2 + 8), W - wk / 2 - 8);
    return `<g class="hovercard" id="${hcId}"><g transform="translate(${cx} ${cy}) scale(${CARD_SCALE}) translate(${-cx} ${-cy})">${card(cx, cy, opts)}</g></g>`;
  }

  let svg = `<svg class="house-svg${compact ? ' compact' : ''}" viewBox="0 0 ${W} ${totalH}" role="img" aria-label="Map of the internet connection and routers by floor">`;

  // ---- architectural backdrop: hatched earth below the street datum ----
  svg += `<defs>
    <pattern id="earthHatch" width="9" height="9" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
      <line x1="0" y1="0" x2="0" y2="9" stroke="var(--baseline)" stroke-width="1" opacity="0.30"/>
    </pattern>
  </defs>`;
  svg += `<rect fill="url(#earthHatch)" x="8" y="${groundY}" width="${W - 16}" height="${totalH - groundY - 6}"/>`;

  // street-level datum line with an elevation marker, like a section drawing
  svg += `<line class="datum-line" x1="16" y1="${groundY}" x2="${W - 16}" y2="${groundY}"/>`;
  svg += `<path class="datum-mark" d="M 34 ${groundY - 12} h 12 l -6 12 z"/>`;
  svg += `<text class="street-label" x="54" y="${groundY - 6}">street level</text>`;

  // house: flat roof slab, walls, floors
  svg += `<rect class="roof" x="${HX0 - 14}" y="${TOP - 9}" width="${HX1 - HX0 + 28}" height="9" rx="2"/>`;
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
    const chipW = f.key.length * (compact ? 7.4 : 6.6) + 20;
    svg += `<rect class="floor-chip" x="${HX0 + 12}" y="${f.y0 + 9}" width="${chipW}" height="20" rx="7"/>`;
    svg += `<text class="floor-label" x="${HX0 + 21}" y="${f.y0 + 23}">${escapeHtml(f.key)}</text>`;
  });

  // windows on the right of each above-ground floor, lit while that
  // floor's access points are all up — a dark floor means trouble there.
  // Compact skips them: every horizontal unit belongs to the pills, and
  // floor status is already readable from the pill dots.
  if (!compact) FLOORS.forEach(f => {
    if (f.y0 >= groundY) return;  // basements don't get windows
    const statuses = routers.filter(r => r.floor === f.key).map(r => r.status);
    if (f.key === mainFloor.key && gw) statuses.push(gw.status);
    const lit = statuses.length ? statuses.every(s => s === 'up') : netUp;
    const wy = (f.y0 + f.y1) / 2 - 13;
    [HX1 - 104, HX1 - 78, HX1 - 52].forEach(wx => {
      svg += `<rect class="win ${lit ? 'lit' : 'off'}" x="${wx}" y="${wy}" width="14" height="42" rx="2"/>`;
    });
  });

  // ---- the internet itself: a cloud outside the house, wired to the
  // main router. House green + cloud red = it's the ISP, not your Wi-Fi.
  // With an ISP box configured the chain is drawn honestly in two hops —
  // internet → wall box → main router — instead of pretending the fiber
  // plugs straight into the user's own router.
  const drawWan = (d, up, slow) => {
    svg += `<g class="linkgrp ${up ? 'up' : 'down'}">`;
    svg += `<path class="link-glow" d="${d}"/><path class="link-core" d="${d}"/>`;
    if (up && !REDUCE_MOTION) {
      svg += `<circle class="packet" r="3" opacity="0.9"><animateMotion dur="${slow || 2.6}s" repeatCount="indefinite" path="${d}"/></circle>`;
      svg += `<circle class="packet" r="2.4" opacity="0.7"><animateMotion dur="${slow || 2.6}s" begin="-1.3s" repeatCount="indefinite" path="${d}"/></circle>`;
    }
    svg += `</g>`;
  };
  if (gw && ISP) {
    const nodeRight = NET.x + 62;
    let fiberD;
    if (!compact) {
      // buried run from the street node, riser up the OUTSIDE of the wall
      // into the box bottom
      fiberD = `M ${nodeRight} ${NET.y} L ${HX0 - 34} ${NET.y} Q ${HX0} ${NET.y} ${HX0} ${NET.y - 34} L ${HX0} ${ISP.y + 24}`;
    } else {
      // straight rise from the node top into the box bottom
      fiberD = `M ${NET.x} ${NET.y - 30} L ${NET.x} ${ISP.y + 24}`;
    }
    // segment 1 (street → ISP box) carries internet reachability;
    // segment 2 (ISP box → main router) carries the box's own liveness —
    // exactly the split the STC-box monitoring exists to make visible
    drawWan(fiberD, netUp);
    const boxEdgeX = compact ? ISP.x : ISP.x + 55;
    const boxEdgeY = compact ? ISP.y - 24 : ISP.y;
    const d2 = `M ${boxEdgeX} ${boxEdgeY} Q ${(boxEdgeX + MAIN.x) / 2} ${(boxEdgeY + MAIN.y) / 2 - 26} ${MAIN.x} ${MAIN.y}`;
    drawWan(d2, ispBox.status === 'up');
  } else if (gw) {
    // Fiber: a buried run at cable depth from the street node, then a
    // riser straight up through the house to the main router.
    const nodeRight = NET.x + 62;
    let wanD;
    // buried run + riser needs vertical AND horizontal clearance; the
    // compact layout rarely has the horizontal kind, so it takes the
    // direct curve up through the section instead
    if (MAIN.y + 30 < NET.y - 34 && MAIN.x - 34 > nodeRight + 10) {
      wanD = `M ${nodeRight} ${NET.y} L ${MAIN.x - 34} ${NET.y} Q ${MAIN.x} ${NET.y} ${MAIN.x} ${NET.y - 34} L ${MAIN.x} ${MAIN.y + 17}`;
    } else {
      // main router unusually low (e.g. in the basement) — connect directly
      wanD = `M ${nodeRight} ${NET.y} Q ${(nodeRight + MAIN.x) / 2} ${NET.y} ${MAIN.x} ${MAIN.y + 17}`;
    }
    drawWan(wanD, netUp);
  }
  const CHK = DATA.checks || {};
  svg += `<g class="net-node ${netUp ? 'up' : 'down'}">`;
  svg += `<rect class="net-box" x="${NET.x - 62}" y="${NET.y - 30}" width="124" height="84" rx="10"/>`;
  svg += `<circle class="status-dot-svg" cx="${NET.x - 46}" cy="${NET.y - 13}" r="4"/>`;
  svg += `<text class="net-label" x="${NET.x - 36}" y="${NET.y - 9}">Internet</text>`;
  svg += `<text class="net-stat" x="${NET.x - 46}" y="${NET.y + 8}">${
    netUp ? (DATA.current_latency != null ? Math.round(DATA.current_latency) + ' ms' : 'online') : 'OFFLINE'}</text>`;
  // The speed readout lives on the monitor-location router's card when
  // one is configured (the number describes THAT path, not "the
  // internet") — the cloud only keeps it on location-less installs.
  if (!speedHost && netUp && lastSpeed && lastSpeed.down != null) {
    svg += `<text class="net-stat" x="${NET.x - 46}" y="${NET.y + 22}" opacity="0.8">&#8595;${Math.round(lastSpeed.down)} &#8593;${Math.round(lastSpeed.up)} Mbps</text>`;
  } else if (!netUp) {
    svg += `<text class="net-stat" x="${NET.x - 46}" y="${NET.y + 22}">check the ISP</text>`;
  }
  // cadence lines: the latency above is 15s-fresh but the speed figures can
  // be up to 30min old — worth saying so right on the node
  const netPing = checkEff('ping'), netSpd = checkEff('speed');
  svg += `<text class="net-foot" x="${NET.x - 46}" y="${NET.y + 36}" data-checkfoot="1" data-fmt="min"
    data-cmd="ping" data-freq="${netPing.freq}"${netPing.approx ? ' data-approx="1"' : ''}${CHK.ping ? ` data-ts="${CHK.ping}"` : ''}></text>`;
  if (!speedHost) {
    svg += `<text class="net-foot" x="${NET.x - 46}" y="${NET.y + 48}" data-checkfoot="1" data-fmt="min"
      data-cmd="speedtest" data-freq="${netSpd.freq}"${netSpd.approx ? ' data-approx="1"' : ''}${CHK.speed ? ` data-ts="${CHK.speed}"` : ''}></text>`;
  }
  svg += `</g>`;

  // wi-fi coverage bubbles behind every node — an offline AP reads as a
  // pulsing hole in the coverage, not just one bad pill
  placed.forEach(p => {
    const cov = p.status === 'up' ? ((p.method === 'arp' || p.method === 'probe') ? 'silent' : 'up') : 'down';
    svg += `<circle class="cover ${cov}" cx="${p.x}" cy="${p.y}" r="${compact ? 42 : 62}"/>`;
  });
  if (gw) {
    svg += `<circle class="cover ${gw.status === 'up' ? 'main' : 'down'}" cx="${MAIN.x}" cy="${MAIN.y}" r="${compact ? 52 : 76}"/>`;
  }

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
    const R0 = compact ? 30 : 40, R1 = compact ? 78 : 120;
    svg += `<circle class="ripple" cx="${MAIN.x}" cy="${MAIN.y}" r="${R0}" stroke-width="1.5">
      <animate attributeName="r" from="${R0}" to="${R1}" dur="3.6s" repeatCount="indefinite"/>
      <animate attributeName="opacity" values="0.5;0" dur="3.6s" repeatCount="indefinite"/>
    </circle>`;
    svg += `<circle class="ripple" cx="${MAIN.x}" cy="${MAIN.y}" r="${R0}" stroke-width="1.5">
      <animate attributeName="r" from="${R0}" to="${R1}" begin="-1.8s" dur="3.6s" repeatCount="indefinite"/>
      <animate attributeName="opacity" values="0.5;0" begin="-1.8s" dur="3.6s" repeatCount="indefinite"/>
    </circle>`;
  }

  // router pills; the detailed cards render last (on top of everything)
  // and appear on hover/tap
  const hcs = [];
  // routers with a line in the per-router chart get a "chart ↓" link on
  // their hover card (the gateway rides the main latency chart instead)
  const chartNames = new Set(Object.keys(((DATA.routers_chart || {}).series) || {}));
  placed.forEach((p, i) => {
    svg += pill(p.x, p.y, p, 'hc-' + i);
    hcs.push(hovercard(p, { ...p, showSpeed: speedHost === 'router' && p.name === MONLOC,
                            chartable: chartNames.has(p.name) }, 'hc-' + i));
  });
  if (gw) {
    // Combo-box merge: gw.isp_name is the user's name for the ISP box the
    // Python side folded into this node — ride it on the IP line so the
    // box they configured is still visibly accounted for.
    const boxNm = gw.isp_name
      ? (gw.isp_name.length > 14 ? gw.isp_name.slice(0, 13) + '…' : gw.isp_name) : null;
    const opts = { main: true, name: 'Main Router', ip: gw.ip + (boxNm ? ' · ' + boxNm : ''),
                   status: gw.status, uptime_pct: gw.uptime_pct, avg_latency: gw.avg_latency,
                   last_check: gw.last_check, showSpeed: speedHost === 'main' };
    svg += pill(MAIN.x, MAIN.y, opts, 'hc-main');
    hcs.push(hovercard(MAIN, opts, 'hc-main'));
  }
  if (ISP) {
    // the wall box: utility-meter styling — a squared box (not a pill),
    // mounted where the fiber meets the house
    const w = compact ? 96 : 110, h = 48;
    const x0 = ISP.x - w / 2, y0 = ISP.y - h / 2;
    const nm = ispBox.name.length > 14 ? ispBox.name.slice(0, 13) + '…' : ispBox.name;
    svg += `<g class="pillgrp ${nodeCls(ispBox)}" data-hc="hc-isp">
      <rect class="pill-box" x="${x0}" y="${y0}" width="${w}" height="${h}" rx="8"/>
      <circle class="status-dot-svg" cx="${x0 + 14}" cy="${ISP.y - 8}" r="4"/>
      <text class="pill-name" x="${x0 + 24}" y="${ISP.y - 4}">${escapeHtml(nm)}</text>
      <text class="isp-sub" x="${x0 + 14}" y="${ISP.y + 14}">ISP box</text>
    </g>`;
    hcs.push(hovercard(ISP, { ...ispBox, showSpeed: speedHost === 'isp',
                              chartable: chartNames.has(ispBox.name) }, 'hc-isp'));
  }
  svg += hcs.join('');

  svg += `</svg>`;
  wrap.innerHTML = svg;
  // the SVG cadence footers were just (re)created empty — fill them now
  // rather than waiting for the next 10s tick
  tickCheckFoots();

  // hover on desktop, tap to toggle on touch. Hide runs on a short delay
  // and the shown card itself cancels it, so the mouse can cross the
  // pill→card gap to reach the "chart" link without the card vanishing.
  wrap.querySelectorAll('.pillgrp').forEach(g => {
    const hc = document.getElementById(g.dataset.hc);
    if (!hc) return;
    let hideT = null;
    const show = () => { clearTimeout(hideT); hc.classList.add('show'); };
    const hide = () => { hideT = setTimeout(() => hc.classList.remove('show'), 250); };
    g.addEventListener('mouseenter', show);
    g.addEventListener('mouseleave', hide);
    hc.addEventListener('mouseenter', show);
    hc.addEventListener('mouseleave', hide);
    g.addEventListener('click', () => { clearTimeout(hideT); hc.classList.toggle('show'); });
  });

  // "chart ↓" links on the hover cards → focus that router's line
  wrap.addEventListener('click', (ev) => {
    const ln = ev.target && ev.target.closest ? ev.target.closest('[data-chartlink]') : null;
    if (!ln) return;
    ev.stopPropagation();
    focusRouterChart(ln.getAttribute('data-chartlink'));
  });

  if (unplaced.length) {
    const n = document.getElementById('houseMapNote');
    n.style.display = 'block';
    n.textContent = 'Not on the map yet (no floor assigned): ' + unplaced.map(r => r.name).join(', ')
      + ' — pick each one’s floor in Settings → Routers.';
  }
}
safely('house map', renderHouseMap);
// rotate / window resize: re-render only when crossing the compact
// threshold — mid-drag renders are pointless, so debounce 200ms
safely('house map resize', function() {
  window.addEventListener('resize', () => {
    clearTimeout(window.__mapResizeT);
    window.__mapResizeT = setTimeout(() => {
      const w = document.getElementById('houseMapWrap');
      if (!w) return;
      const nowCompact = (w.clientWidth || 900) < 520;
      if (nowCompact !== renderHouseMap.lastCompact) safely('house map', renderHouseMap);
    }, 200);
  });
});

// ---------- double-NAT hint ----------
// Placed AFTER the house map block on purpose: the map renderer writes
// houseMapNote for unplaced routers, and this must append, not be
// overwritten by it.
safely('double NAT hint', function() {
  const t = DATA.topology;
  if (!t || !t.double_nat) return;
  const n = document.getElementById('houseMapNote');
  if (n) {
    const extra = 'Two routers are each doing NAT (double NAT). Consoles, VoIP and port '
      + 'forwarding may misbehave. Fix: bridge/modem mode on the ISP box — or set its DMZ '
      + 'to your main router, which mostly mitigates it.';
    n.textContent = (n.style.display === 'block' && n.textContent) ? n.textContent + ' · ' + extra : extra;
    n.style.display = 'block';
  }
  const pip = document.getElementById('publicIpStable');
  if (pip) {
    pip.innerHTML += '<br><span style="color:var(--status-warning); font-weight:600;">⚠ double NAT detected — see the house map note</span>';
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

function baseTicks() { return { color: tickColor(), font: { family: monoFont(), size: 11 } }; }
function timeScale(hours) {
  return { type: 'time', time: { unit: hours <= 24 ? 'hour' : 'day' },
    grid: { color: gridColor() }, border: { display: false }, ticks: baseTicks() };
}
function fixedTimeScale(unit) {
  return { type: 'time', time: { unit: unit },
    grid: { color: gridColor() }, border: { display: false }, ticks: baseTicks() };
}
function yScale(titleText, extra) {
  return Object.assign({
    title: { display: true, text: titleText, color: tickColor(), font: { family: monoFont(), size: 11 } },
    grid: { color: gridColor() }, border: { display: false }, ticks: baseTicks(), beginAtZero: true,
  }, extra || {});
}
// Tooltips render as a compact readout strip pinned to the TOP of the
// chart card instead of Chart.js's floating box — with 10 router lines
// the box covered half the plot right where the cursor was pointing.
// The strip reuses the tooltip MODEL (so per-chart label callbacks still
// apply); only the drawing moved out of the canvas.
function externalTip(context) {
  const tooltip = context.tooltip;
  const canvas = context.chart.canvas;
  const cardEl = canvas && canvas.closest ? canvas.closest('.chart-card') : null;
  if (!cardEl) return;
  let tip = cardEl.__chartTip;
  if (!tip) {
    tip = document.createElement('div');
    tip.className = 'chart-tip';
    cardEl.appendChild(tip);
    cardEl.__chartTip = tip;
  }
  if (!tooltip || tooltip.opacity === 0) { tip.classList.remove('show'); return; }
  const title = (tooltip.title || []).join(' ');
  const items = (tooltip.body || []).map((b, i) => {
    const col = (tooltip.labelColors && tooltip.labelColors[i]) || {};
    const sw = col.borderColor || col.backgroundColor || 'var(--muted)';
    return '<span class="tip-item"><i style="background:' + sw + '"></i>' + escapeHtml(b.lines.join(' ')) + '</span>';
  }).join('');
  tip.innerHTML = '<span class="tip-time">' + escapeHtml(title) + '</span>' + items;
  tip.classList.add('show');
  // x follows the cursor (clamped inside the card); y pins to just under
  // the plot box so the readout never covers the lines being read
  const box = canvas.closest('.chart-box');
  const cardR = cardEl.getBoundingClientRect ? cardEl.getBoundingClientRect() : null;
  const boxR = box && box.getBoundingClientRect ? box.getBoundingClientRect() : null;
  if (cardR && boxR) {
    const half = (tip.offsetWidth || 150) / 2;
    let x = (boxR.left - cardR.left) + (tooltip.caretX || 0);
    x = Math.max(half + 6, Math.min(x, cardEl.clientWidth - half - 6));
    tip.style.left = x + 'px';
    tip.style.top = (boxR.bottom - cardR.top + 6) + 'px';
  }
}
function tooltipBase() {
  return {
    enabled: false,
    external: externalTip,
    titleFont: { family: monoFont(), size: 12 },
    bodyFont: { family: monoFont(), size: 12 },
    displayColors: true,
  };
}
function legendOpts(show) {
  return { display: show, labels: { color: legendColor(), usePointStyle: true, pointStyle: 'line', font: { size: 12 } } };
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
      ctx.font = '10px ' + (cssVar('--font-mono') || 'ui-monospace, monospace');
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

// Each chart owns its 3h/24h/7d range (mini toggle in the card corner);
// the topbar's global toggle sets them all at once. Keys must match the
// data-chart attrs in the markup AND the RANGE_CHARTS registry below.
// The load-time default comes from config default_range_hours (Settings
// → General), falling back to 3h.
const DEFAULT_RANGE = [3, 24, 168].indexOf(DATA.default_range_hours) !== -1
  ? DATA.default_range_hours : 3;
const chartRange = { latency: DEFAULT_RANGE, loss: DEFAULT_RANGE, routers: DEFAULT_RANGE,
                     targets: DEFAULT_RANGE, speed: DEFAULT_RANGE, loaded: DEFAULT_RANGE,
                     devcount: DEFAULT_RANGE };
function rangeFor(key) { return chartRange[key] || DEFAULT_RANGE; }

function renderLatencyChart() {
  const hours = rangeFor('latency');
  const series = filterByRange(DATA.latency_series, hours);
  const jitter = filterByRange(DATA.jitter_series || [], hours);
  const dns = filterByRange(DATA.dns_series || [], hours);
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
      scales: { x: timeScale(hours), y: yScale('ms') },
      plugins: {
        legend: legendOpts(datasets.length > 1),
        tooltip: { ...tooltipBase(), callbacks: { label: (c) => c.dataset.label + ': ' + (c.parsed.y == null ? 'no data' : c.parsed.y + ' ms') } },
      },
    },
  });
}

function renderLossChart() {
  const hours = rangeFor('loss');
  const series = filterByRange(DATA.loss_series, hours);
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
      scales: { x: timeScale(hours), y: yScale('%', { suggestedMax: 10 }) },
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
  const hours = rangeFor('routers');
  const cutoff = Date.now() - hours * 3600 * 1000;
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
      scales: { x: timeScale(hours), y: yScale('ms') },
      plugins: {
        legend: legendOpts(true),
        tooltip: { ...tooltipBase(), callbacks: { label: (c) => c.dataset.label + ': ' + (c.parsed.y == null ? 'no data' : c.parsed.y + ' ms') } },
      },
    },
  });
  applyRouterFocus();   // keep a map-link focus across range/theme re-renders
}

// ---------- map → chart focus ----------
// A "chart ↓" link on a router's map hover card narrows the per-router
// chart to that line (others dimmed) and scrolls it into view; the
// "Focused: X ✕" chip in the chart label clears it.
let routerFocusName = null;
function applyRouterFocus() {
  const ch = chartInstances.routers;
  const chip = document.getElementById('routerFocus');
  if (!ch || !ch.data || !ch.data.datasets) return;
  const has = routerFocusName && ch.data.datasets.some(d => d.label === routerFocusName);
  ch.data.datasets.forEach((d, i) => {
    const col = catColor(i);
    if (has && d.label !== routerFocusName) {
      const dim = hexToRgba(col, 0.14);
      d.borderColor = dim; d.backgroundColor = dim; d.borderWidth = 1;
    } else {
      d.borderColor = col; d.backgroundColor = col;
      d.borderWidth = has ? 2.5 : 1.5;
    }
  });
  ch.update();
  if (chip) {
    if (has) { chip.style.display = ''; chip.textContent = 'Focused: ' + routerFocusName + ' ✕'; }
    else { chip.style.display = 'none'; }
  }
}
function focusRouterChart(name) {
  routerFocusName = name;
  applyRouterFocus();
  const cardEl = document.getElementById('routersCard');
  if (cardEl && cardEl.scrollIntoView) cardEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
}
safely('router focus chip', function() {
  const chip = document.getElementById('routerFocus');
  if (chip) chip.addEventListener('click', () => { routerFocusName = null; applyRouterFocus(); });
});

// ---------- custom-target latency chart (only when targets exist) ----------
function renderTargetsChart() {
  const tc = DATA.targets_chart;
  const names = tc ? Object.keys(tc.series) : [];
  const card = document.getElementById('targetsCard');
  if (!tc || !tc.buckets.length || names.length === 0) { card.style.display = 'none'; return; }
  card.style.display = '';
  const hours = rangeFor('targets');
  const cutoff = Date.now() - hours * 3600 * 1000;
  const keep = tc.buckets.map(t => new Date(t).getTime() >= cutoff);
  const labels = tc.buckets.filter((_, i) => keep[i]).map(t => new Date(t));
  const datasets = names.map((name, i) => {
    const col = catColor(i + 3);   // offset so targets don't mirror the first routers' colors
    return {
      label: name,
      data: tc.series[name].filter((_, j) => keep[j]),
      borderColor: col, backgroundColor: col,
      borderWidth: 1.5, pointRadius: 0, pointHoverRadius: 4, tension: 0.2, spanGaps: false,
    };
  });
  if (chartInstances.targets) chartInstances.targets.destroy();
  chartInstances.targets = new Chart(document.getElementById('targetsChart'), {
    type: 'line',
    data: { labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      scales: { x: timeScale(hours), y: yScale('ms') },
      plugins: {
        legend: legendOpts(true),
        tooltip: { ...tooltipBase(), callbacks: { label: (c) => c.dataset.label + ': ' + (c.parsed.y == null ? 'no data' : c.parsed.y + ' ms') } },
      },
    },
  });
}

// Per-chart ranges: each card's mini toggle re-renders only its own chart;
// the topbar's global toggle sets every chart at once. ONE registry keyed
// the same as chartRange — it also feeds rerenderCharts, so a new chart
// added here is automatically range-aware AND theme-re-rendered (the two
// duplicate hand-maintained lists this replaces once let a chart render
// blank on first load).
const RANGE_CHARTS = {
  latency: renderLatencyChart, loss: renderLossChart, routers: renderRoutersChart,
  targets: renderTargetsChart, speed: renderSpeedChart, loaded: renderLoadedLatencyChart,
  devcount: renderDevCountChart,
};
function syncRangeToggles() {
  document.querySelectorAll('.range-toggle.mini').forEach(tg => {
    const k = tg.dataset.chart;
    tg.querySelectorAll('button').forEach(b =>
      b.classList.toggle('active', parseInt(b.dataset.range, 10) === rangeFor(k)));
  });
  // the global toggle only lights up while every chart agrees
  const vals = Object.keys(RANGE_CHARTS).map(rangeFor);
  const uniform = vals.every(v => v === vals[0]) ? vals[0] : null;
  document.querySelectorAll('#globalRange button').forEach(b =>
    b.classList.toggle('active', uniform !== null && parseInt(b.dataset.range, 10) === uniform));
}
safely('range toggles', function() {
  document.querySelectorAll('.range-toggle.mini').forEach(tg => {
    tg.addEventListener('click', (e) => {
      const btn = e.target.closest('button');
      if (!btn) return;
      const k = tg.dataset.chart;
      chartRange[k] = parseInt(btn.dataset.range, 10);
      if (RANGE_CHARTS[k]) safely('range ' + k, RANGE_CHARTS[k]);
      syncRangeToggles();
    });
  });
  const g = document.getElementById('globalRange');
  if (g) g.addEventListener('click', (e) => {
    const btn = e.target.closest('button');
    if (!btn) return;
    const hours = parseInt(btn.dataset.range, 10);
    Object.keys(RANGE_CHARTS).forEach(k => { chartRange[k] = hours; });
    Object.keys(RANGE_CHARTS).forEach(k => safely('range ' + k, RANGE_CHARTS[k]));
    syncRangeToggles();
  });
  syncRangeToggles();  // reflect the defaults on every toggle at load
});

// ---------- speed test chart (range-aware) ----------
function rangeWord(hours) { return hours <= 3 ? '3 hours' : hours <= 24 ? '24 hours' : '7 days'; }

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
  const hours = rangeFor('speed');
  const series = filterByRange(DATA.speed_series, hours);
  if (series.length === 0) return showEmpty('No speed tests in the last ' + rangeWord(hours) + '.');
  canvas.style.display = ''; emptyEl.style.display = 'none';
  const blue = catColor(0), aqua = catColor(4);
  if (chartInstances.speed) chartInstances.speed.destroy();
  const plan = DATA.plan || {};
  // Failed test runs render as red × marks pinned at y=0 — a test that
  // couldn't finish during congestion is itself a data point.
  const failures = filterByRange(DATA.speed_failures || [], hours);
  const failColor = cssVar('--status-critical');
  const datasets = [
    { label: 'Download (Mbps)', data: series.map(p => p.down), borderColor: blue, backgroundColor: blue, borderWidth: 2, pointRadius: 3, pointHoverRadius: 6, pointBackgroundColor: blue, pointBorderColor: surfaceColor(), pointBorderWidth: 2, tension: 0.2 },
    { label: 'Upload (Mbps)', data: series.map(p => p.up), borderColor: aqua, backgroundColor: aqua, borderWidth: 2, pointRadius: 3, pointHoverRadius: 6, pointBackgroundColor: aqua, pointBorderColor: surfaceColor(), pointBorderWidth: 2, tension: 0.2 },
  ];
  if (failures.length) {
    // Purely visual markers: pointHitRadius 0 keeps them out of hover and
    // tooltips entirely. (They used to be hoverable via interaction mode
    // 'x', but that mode merges ADJACENT test runs sharing an x pixel
    // into one tooltip — two downloads + two uploads at "the same time".
    // Error text lives in the note under the chart instead.)
    datasets.push({
      type: 'scatter', label: 'Failed test',
      data: failures.map(f => ({ x: new Date(f.t), y: 0 })),
      pointStyle: 'crossRot', pointRadius: 6, pointHitRadius: 0, borderWidth: 2,
      borderColor: failColor, backgroundColor: failColor,
    });
  }
  // most-recent-test-failed note (also the home of the error detail)
  const noteEl = document.getElementById('speedFailNote');
  if (noteEl) {
    const lastOk = series.length ? series[series.length - 1].t : null;
    const lastFail = failures.length ? failures[failures.length - 1] : null;
    if (lastFail && (!lastOk || Date.parse(lastFail.t) > Date.parse(lastOk))) {
      noteEl.style.display = '';
      noteEl.textContent = 'Last speed test failed (' + new Date(lastFail.t).toLocaleString() + '): ' + lastFail.error;
    } else if (lastFail) {
      noteEl.style.display = '';
      noteEl.textContent = failures.length + ' failed test' + (failures.length === 1 ? '' : 's')
        + ' in this window (red × marks) — latest error: ' + lastFail.error;
    } else {
      noteEl.style.display = 'none';
    }
  }
  // belt-and-braces: index mode can still enumerate the scatter dataset at
  // a coincident data index — keep it out of tooltip rows altogether
  const tt = tooltipBase();
  tt.filter = (item) => item.dataset.label !== 'Failed test';
  chartInstances.speed = new Chart(canvas, {
    type: 'line',
    plugins: [ refLines([
      { value: plan.down_mbps != null ? plan.down_mbps : null, color: cssVar('--cat-1'), label: 'plan ↓ ' + plan.down_mbps },
      { value: plan.up_mbps != null ? plan.up_mbps : null, color: cssVar('--cat-5'), label: 'plan ↑ ' + plan.up_mbps },
    ]) ],
    data: {
      labels: series.map(p => new Date(p.t)),
      datasets: datasets,
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      // 'index' = one tooltip per test run (a download + its upload).
      interaction: { mode: 'index', intersect: false },
      // Anchor the axis at 0 and keep headroom above the plan lines, so
      // the "what am I paying for" reference is always on screen even
      // when measured speeds sit well below it. suggestedMax (not max)
      // still lets the axis grow if a test ever exceeds plan + 100.
      scales: { x: timeScale(hours), y: Object.assign(yScale('Mbps'), { min: 0 },
        (plan.down_mbps != null || plan.up_mbps != null)
          ? { suggestedMax: Math.max(plan.down_mbps || 0, plan.up_mbps || 0) + 100 } : {}) },
      plugins: { legend: legendOpts(true), tooltip: tt },
    },
  });
}

// ---------- latency under load / bufferbloat (range-aware) ----------
function renderLoadedLatencyChart() {
  const canvas = document.getElementById('loadedLatencyChart');
  const emptyEl = document.getElementById('loadedLatencyEmpty');
  const showEmpty = (msg) => {
    if (chartInstances.loadedLat) { chartInstances.loadedLat.destroy(); chartInstances.loadedLat = null; }
    canvas.style.display = 'none'; emptyEl.style.display = 'block'; emptyEl.textContent = msg;
  };
  const all = (DATA.speed_series || []).filter(s => s.lat_down != null || s.lat_up != null);
  if (all.length === 0) {
    return showEmpty('No latency-under-load data yet — it needs the official Ookla speedtest CLI (see README) and appears after the next test.');
  }
  const hours = rangeFor('loaded');
  const series = filterByRange(all, hours);
  if (series.length === 0) return showEmpty('No loaded-latency data in the last ' + rangeWord(hours) + '.');
  canvas.style.display = ''; emptyEl.style.display = 'none';
  const idle = catColor(2), down = catColor(0), up = catColor(4);
  if (chartInstances.loadedLat) chartInstances.loadedLat.destroy();
  chartInstances.loadedLat = new Chart(canvas, {
    type: 'line',
    data: {
      labels: series.map(p => new Date(p.t)),
      datasets: [
        { label: 'Idle ping', data: series.map(p => p.ping), borderColor: idle, backgroundColor: idle, borderWidth: 2, pointRadius: 2, pointHoverRadius: 5, borderDash: [5, 4], tension: 0.2 },
        { label: 'While downloading', data: series.map(p => p.lat_down), borderColor: down, backgroundColor: down, borderWidth: 2, pointRadius: 2, pointHoverRadius: 5, tension: 0.2 },
        { label: 'While uploading', data: series.map(p => p.lat_up), borderColor: up, backgroundColor: up, borderWidth: 2, pointRadius: 2, pointHoverRadius: 5, tension: 0.2 },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      scales: { x: timeScale(hours), y: yScale('ms') },
      plugins: { legend: legendOpts(true), tooltip: tooltipBase() },
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
  const hours = rangeFor('devcount');
  const series = filterByRange(all, hours);
  if (series.length < 2) return showEmpty('Not enough scans in the last ' + rangeWord(hours) + '.');
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
      scales: { x: timeScale(hours), y: yScale('devices', { ticks: Object.assign(baseTicks(), { precision: 0 }) }) },
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
  Object.keys(RANGE_CHARTS).forEach(k => safely(k + ' chart', RANGE_CHARTS[k]));
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

// ---------- "Test now" button ----------
// Talks to the LAN-carved-out /api/test/* endpoints (serve.py). Opening
// dashboard.html via file:// has no server at all, so the button only
// appears once a probe of the status endpoint answers.
safely('test now', function() {
  if (location.protocol === 'file:') return;
  const topBtn = document.getElementById('testNowBtn');
  const out = document.getElementById('testNowResult');
  // per-chart "Check now" buttons ride the same one-test-at-a-time rail:
  // data-checknow = 'quick' (ping+DNS, ~10s) or 'speed' (full test).
  // Results are ALSO written to the DB by the monitor, so they land on
  // the charts on the next regen — the inline note is just instant.
  const cardBtns = Array.from(document.querySelectorAll('button[data-checknow]'));
  const allBtns = [topBtn].concat(cardBtns).filter(Boolean);
  let polling = null;
  let active = null;   // the button that started (or adopted) the run

  function noteFor(btn) {
    if (!btn || btn === topBtn) return out;
    return document.getElementById('ck-' + btn.dataset.checknote) || out;
  }
  function setBusy(phase) {
    allBtns.forEach(b => { b.disabled = true; });
    (active || topBtn).textContent = 'Testing: ' + (phase || '…');
  }
  function setIdle() {
    allBtns.forEach(b => { b.disabled = false; });
    if (topBtn) topBtn.textContent = 'Test now';
    cardBtns.forEach(b => { b.textContent = 'Check now'; });
    if (polling) { clearInterval(polling); polling = null; }
  }
  function showNote(txt) {
    const el = noteFor(active);
    el.style.display = '';
    el.textContent = txt;
  }
  function showResult(res) {
    if (!res) return;
    const bits = [];
    if (res.gateway) bits.push('router ' + (res.gateway.ok ? (res.gateway.avg_ms != null ? res.gateway.avg_ms + 'ms' : 'ok') : ('FAIL ' + res.gateway.ok + '/5')));
    if (res.external) bits.push('internet ' + (res.external.ok ? (res.external.avg_ms != null ? res.external.avg_ms + 'ms' : 'ok') : 'FAIL'));
    if (res.dns) bits.push('DNS ' + (res.dns.ok ? (res.dns.ms != null ? Math.round(res.dns.ms) + 'ms' : 'ok') : 'FAIL'));
    if (res.speedtest) bits.push(res.speedtest.error ? 'speed test failed' : ('↓' + Math.round(res.speedtest.down) + ' ↑' + Math.round(res.speedtest.up) + ' Mbps'));
    if (res.devices_found != null) bits.push(res.devices_found + ' devices answered a scan');
    showNote(bits.join(' · ') + ' — on the charts after the next refresh (~1 min)');
  }
  function poll(sinceLoad) {
    let waited = 0;
    polling = setInterval(() => {
      waited += 1.5;
      fetch('/api/test/status').then(r => r.json()).then(st => {
        if (st.state === 'running') { setBusy(st.phase); }
        else if (st.state === 'done') { setIdle(); showResult(st.results); }
        else if (st.state === 'error') { setIdle(); showNote('test failed: ' + (st.error || 'unknown')); }
        else if (waited > 10 && !sinceLoad) { setIdle(); showNote('no response — is the monitor service running?'); }
      }).catch(() => {});
      if (waited > 180) setIdle();   // hard stop: never spin forever
    }, 1500);
  }

  function start(btn) {
    active = btn;
    const el = noteFor(btn);
    el.style.display = 'none';
    setBusy('starting');
    const body = btn.dataset && btn.dataset.checknow === 'speed' ? '{"speedtest": true}' : '{}';
    fetch('/api/test/run', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: body })
      .then(r => {
        if (r.status === 202) { poll(false); }
        else if (r.status === 409) { setIdle(); poll(true); }  // someone else started one — watch it
        else if (r.status === 429) { setIdle(); showNote('please wait a minute between tests'); }
        else { setIdle(); showNote('test unavailable (' + r.status + ')'); }
      })
      .catch(() => { setIdle(); showNote('could not reach the server'); });
  }
  allBtns.forEach(b => b.addEventListener('click', () => start(b)));

  // only show the buttons where the endpoint actually answers (from LAN or
  // localhost via serve.py); resume watching a test already in flight if
  // the 60s auto-refresh reloaded the page mid-run
  fetch('/api/test/status').then(r => {
    if (!r.ok) return;
    allBtns.forEach(b => { b.style.display = ''; });
    const dsb = document.getElementById('devScanBtn');
    if (dsb) dsb.style.display = '';
    return r.json().then(st => { if (st.state === 'running') { active = topBtn; setBusy(st.phase); poll(true); } });
  }).catch(() => {});
});

// ---------- devices scan-now ----------
// Same command rail as Check now, but its own small state machine — a
// device sweep isn't a connectivity test and reports a device count.
safely('devices scan now', function() {
  if (location.protocol === 'file:') return;
  const btn = document.getElementById('devScanBtn');
  const note = document.getElementById('devScanNote');
  if (!btn || !note) return;
  let timer = null;
  const idle = () => { btn.disabled = false; btn.textContent = 'Scan now'; if (timer) { clearInterval(timer); timer = null; } };
  const say = (txt) => { note.style.display = ''; note.textContent = txt; };
  btn.addEventListener('click', () => {
    note.style.display = 'none';
    btn.disabled = true;
    btn.textContent = 'Scanning…';
    fetch('/api/devices/scan', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })
      .then(r => {
        if (r.status === 202) {
          let waited = 0;
          timer = setInterval(() => {
            waited += 2;
            fetch('/api/test/status').then(x => x.json()).then(st => {
              if (st.state === 'done' && st.results && st.results.devices_found != null) {
                idle(); say(st.results.devices_found + ' devices answered — table refreshes with the next regen (~1 min)');
              } else if (st.state === 'error') { idle(); say('scan failed: ' + (st.error || 'unknown')); }
            }).catch(() => {});
            if (waited > 120) { idle(); say('no result after 2 minutes — check the monitor service'); }
          }, 2000);
        }
        else if (r.status === 429) { idle(); say('please wait a minute between scans'); }
        else if (r.status === 409) { idle(); say('a test or scan is already running — try again shortly'); }
        else { idle(); say('scan unavailable (' + r.status + ')'); }
      })
      .catch(() => { idle(); say('could not reach the server'); });
  });
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


# ---------------------------------------------------------------------------
# ISP evidence report
# ---------------------------------------------------------------------------

def generate_report(conn, site_config):
    """Query 90 days of outages/speeds and write report.html — a printable
    evidence document for ISP complaints. Reuses main()'s open connection."""
    now = datetime.now(timezone.utc)
    since = iso(now - timedelta(days=REPORT_WINDOW_DAYS))
    try:
        # end_ts clauses catch events that started before the window edge
        # and events still open (end_ts IS NULL).
        events = q(conn,
                   "SELECT start_ts, end_ts, kind, scope, note, router_name FROM events"
                   " WHERE kind IN ('outage','degraded')"
                   # a dead camera/printer is not ISP evidence — keep IoT
                   # outages out of the complaint document entirely
                   " AND COALESCE(scope,'') != 'iot'"
                   " AND (start_ts >= ? OR end_ts >= ? OR end_ts IS NULL)"
                   " ORDER BY start_ts DESC", (since, since))
        speedtests = q(conn,
                       "SELECT ts, download_mbps, upload_mbps, ping_ms FROM speedtests"
                       " WHERE ts >= ? AND download_mbps IS NOT NULL ORDER BY ts", (since,))
        # Measured hours per month = the uptime denominator. Counting
        # distinct hour-buckets of actual ping rows automatically excludes
        # time the monitor was off (asleep PC ≠ downtime). Keyed by UTC
        # month while the page buckets in local time — a few hours of
        # month-edge drift, negligible against a ~720-hour month.
        measured = q(conn,
                     "SELECT substr(ts,1,7) AS month, COUNT(DISTINCT substr(ts,1,13)) AS hours"
                     " FROM pings WHERE ts >= ? GROUP BY month", (since,))
    except sqlite3.OperationalError as e:
        # A pre-upgrade DB missing a table yields an empty (but valid) report.
        print(f"report queries degraded: {e}")
        events, speedtests, measured = [], [], []

    report = {
        "generated_at": now.isoformat(),
        "title": (site_config.get("title") or "Home Network Monitor") if isinstance(site_config, dict) else "Home Network Monitor",
        "version": __version__,
        "window_days": REPORT_WINDOW_DAYS,
        "plan": {"down_mbps": site_config.get("plan_down_mbps"), "up_mbps": site_config.get("plan_up_mbps")},
        # below-plan cutoff (% of advertised) — thresholds.plan_pct.fair,
        # same knob as the dashboard's Speed card rating
        "plan_pct_fair": (((site_config.get("thresholds") or {}).get("plan_pct") or {}).get("fair")
                          if isinstance(site_config.get("thresholds"), dict) else None) or 80,
        "monitor_location": (site_config.get("monitor_location") or "").strip() or None,
        "events": events,
        "speedtests": speedtests,
        "measured_hours": {m["month"]: m["hours"] for m in measured},
    }
    with open(REPORT_OUT_PATH, "w", encoding="utf-8") as f:
        f.write(build_report_html(report))
    print(f"wrote {REPORT_OUT_PATH}")


def build_report_html(report):
    """Standalone, print-first page. Same pipeline shape as build_html():
    one template, data injected as JSON, rendering client-side — so the
    file also works opened directly via file:// with no server."""
    report_json = json.dumps(report)
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__ — ISP evidence report</title>
<style>
  /* Print-first: black-on-white, quiet chrome. This page is meant to be
     handed to an ISP, not admired on a NOC wall. */
  * { box-sizing: border-box; }
  body { font-family: -apple-system, "Segoe UI", Roboto, Arial, sans-serif; color: #1a1f2b;
    background: #fff; margin: 0; padding: 32px 24px; line-height: 1.45; }
  .sheet { max-width: 860px; margin: 0 auto; }
  h1 { font-size: 22px; margin: 0 0 2px; }
  .sub { color: #5a6474; font-size: 13px; margin-bottom: 20px; }
  h2 { font-size: 15px; margin: 26px 0 8px; border-bottom: 2px solid #1a1f2b; padding-bottom: 4px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; font-size: 11px; text-transform: uppercase; letter-spacing: .06em;
    color: #5a6474; border-bottom: 1px solid #c9cedb; padding: 6px 8px; }
  td { padding: 6px 8px; border-bottom: 1px solid #e4e7ef; vertical-align: top; }
  tr { page-break-inside: avoid; }
  .totals { display: flex; gap: 28px; flex-wrap: wrap; margin: 14px 0 4px; }
  .tot .v { font-size: 24px; font-weight: 700; }
  .tot .k { font-size: 11px; text-transform: uppercase; letter-spacing: .06em; color: #5a6474; }
  .muted { color: #5a6474; }
  .bad { color: #b3261e; font-weight: 600; }
  .controls { display: flex; gap: 8px; align-items: center; margin: 0 0 18px; }
  .controls button { font: inherit; font-size: 13px; padding: 6px 14px; border: 1px solid #c9cedb;
    background: #fff; border-radius: 7px; cursor: pointer; }
  .controls button.active { background: #1a1f2b; color: #fff; border-color: #1a1f2b; }
  .controls .print-btn { margin-left: auto; font-weight: 600; }
  .footnote { font-size: 11.5px; color: #5a6474; margin-top: 6px; }
  .empty-good { padding: 14px; background: #eef6ee; border: 1px solid #b9d8b9; border-radius: 8px;
    color: #1e5c1e; font-size: 14px; }
  footer { margin-top: 30px; font-size: 11px; color: #5a6474; border-top: 1px solid #e4e7ef; padding-top: 10px; }
  a.backlink { color: #35508a; font-size: 13px; }
  @media print {
    .no-print { display: none !important; }
    body { padding: 0; }
  }
</style>
</head>
<body>
<div class="sheet">
  <div class="no-print" style="margin-bottom:14px;"><a class="backlink" href="dashboard.html">&larr; back to the dashboard</a></div>
  <h1>__TITLE__ — internet reliability report</h1>
  <div class="sub" id="subline"></div>
  <div class="controls no-print">
    <button data-days="7">7 days</button>
    <button data-days="30" class="active">30 days</button>
    <button data-days="90">90 days</button>
    <button class="print-btn" onclick="window.print()">Print / save as PDF</button>
  </div>

  <h2>Summary</h2>
  <div class="totals" id="totals"></div>
  <div class="footnote" id="totalsNote"></div>

  <h2>Monthly reliability</h2>
  <div id="monthlyWrap"></div>

  <h2 id="outageHead">Outages</h2>
  <div id="outagesWrap"></div>

  <h2 id="speedHead">Speed tests below plan</h2>
  <div id="speedWrap"></div>

  <footer id="footer"></footer>
</div>

<script>
const R = __REPORT_JSON__;

function esc(s) {
  if (s == null) return '';
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function fmtDur(secs) {
  secs = Math.max(0, Math.round(secs));
  if (secs < 60) return secs + 's';
  if (secs < 3600) return Math.round(secs / 60) + 'm';
  if (secs < 86400) return Math.floor(secs / 3600) + 'h ' + Math.round((secs % 3600) / 60) + 'm';
  return Math.floor(secs / 86400) + 'd ' + Math.round((secs % 86400) / 3600) + 'h';
}
function local(ts) { return ts ? new Date(ts).toLocaleString() : ''; }
const SCOPE_LABEL = {
  internet: 'Internet down (ISP)', gateway: 'Router/gateway down',
  dns: 'DNS failure', router: 'Access point down',
};

function render(days) {
  const now = Date.now();
  const cut = now - days * 86400000;
  const evs = R.events.map(e => {
    const s = Date.parse(e.start_ts), en = e.end_ts ? Date.parse(e.end_ts) : now;
    return { ...e, s, en, ongoing: !e.end_ts, dur: (en - s) / 1000 };
  }).filter(e => e.en >= cut);

  // hard connectivity outages = the ISP-relevant ones
  const hard = evs.filter(e => e.kind === 'outage' && (e.scope === 'internet' || e.scope === 'gateway'));
  const slow = evs.filter(e => e.kind === 'degraded');
  const other = evs.filter(e => e.kind === 'outage' && e.scope !== 'internet' && e.scope !== 'gateway');
  // clamp downtime to the window so an outage straddling the edge doesn't overcount
  const downSecs = hard.reduce((a, e) => a + Math.max(0, (Math.min(e.en, now) - Math.max(e.s, cut)) / 1000), 0);

  document.getElementById('subline').textContent =
    'Prepared ' + local(R.generated_at) + ' · window: last ' + days + ' days'
    + (R.plan.down_mbps ? ' · plan: ' + R.plan.down_mbps + ' Mbps down / ' + (R.plan.up_mbps || '?') + ' Mbps up' : '');

  document.getElementById('totals').innerHTML =
    '<div class="tot"><div class="v' + (hard.length ? ' bad' : '') + '">' + hard.length + '</div><div class="k">internet/router outages</div></div>'
    + '<div class="tot"><div class="v' + (downSecs ? ' bad' : '') + '">' + fmtDur(downSecs) + '</div><div class="k">total downtime</div></div>'
    + '<div class="tot"><div class="v">' + slow.length + '</div><div class="k">slow/degraded spells</div></div>'
    + '<div class="tot"><div class="v">' + other.length + '</div><div class="k">DNS / access-point incidents</div></div>';
  document.getElementById('totalsNote').textContent = hard.length
    ? 'Times are local (' + Intl.DateTimeFormat().resolvedOptions().timeZone + '). Downtime counts internet and router/gateway outages measured by 15-second connectivity checks.'
    : 'No internet or router outages recorded in this window by 15-second connectivity checks.';

  // ---- monthly table (local-time months) ----
  const months = {};
  hard.forEach(e => {
    // attribute each outage to the month it started in (local time)
    const d = new Date(Math.max(e.s, cut));
    const key = d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0');
    if (!months[key]) months[key] = { n: 0, down: 0 };
    months[key].n += 1;
    months[key].down += Math.max(0, (Math.min(e.en, now) - Math.max(e.s, cut)) / 1000);
  });
  // ensure months with measurement but no outages also appear (uptime 100%)
  Object.keys(R.measured_hours || {}).forEach(key => {
    const monthEnd = new Date(key + '-01T00:00:00');
    monthEnd.setMonth(monthEnd.getMonth() + 1);
    if (monthEnd.getTime() >= cut && !months[key]) months[key] = { n: 0, down: 0 };
  });
  const monthKeys = Object.keys(months).sort().reverse();
  if (monthKeys.length) {
    let rows = monthKeys.map(key => {
      const m = months[key];
      const measuredH = (R.measured_hours || {})[key];
      const label = new Date(key + '-15T00:00:00').toLocaleDateString(undefined, { month: 'long', year: 'numeric' });
      let uptime = '—', note = '';
      if (measuredH) {
        uptime = Math.max(0, 100 * (1 - m.down / (measuredH * 3600))).toFixed(3).replace(/\\.?0+$/, '') + '%';
        const wallHours = 24 * new Date(new Date(key + '-01').getFullYear(), new Date(key + '-01').getMonth() + 1, 0).getDate();
        if (measuredH < wallHours * 0.9) note = 'monitoring covered ~' + Math.round(measuredH) + 'h of this month';
      }
      return '<tr><td>' + esc(label) + '</td><td>' + m.n + '</td><td>' + (m.down ? fmtDur(m.down) : '—')
        + '</td><td>' + uptime + '</td><td class="muted">' + esc(note) + '</td></tr>';
    }).join('');
    document.getElementById('monthlyWrap').innerHTML =
      '<table><thead><tr><th>Month</th><th>Outages</th><th>Downtime</th><th>Measured uptime</th><th></th></tr></thead><tbody>' + rows + '</tbody></table>';
  } else {
    document.getElementById('monthlyWrap').innerHTML = '<div class="muted">No measurements in this window yet.</div>';
  }

  // ---- outage table ----
  const listed = evs.filter(e => e.kind === 'outage');
  if (listed.length) {
    const rows = listed.map(e =>
      '<tr><td>' + local(e.start_ts) + '</td><td>' + (e.ongoing ? '<span class="bad">ongoing</span>' : local(e.end_ts))
      + '</td><td>' + fmtDur(e.dur) + '</td><td>' + esc(SCOPE_LABEL[e.scope] || e.scope)
      + (e.router_name ? ' — ' + esc(e.router_name) : '') + '</td></tr>').join('');
    document.getElementById('outagesWrap').innerHTML =
      '<table><thead><tr><th>Started</th><th>Ended</th><th>Duration</th><th>What went down</th></tr></thead><tbody>' + rows + '</tbody></table>';
  } else {
    document.getElementById('outagesWrap').innerHTML = '<div class="empty-good">No outages recorded in this window — 100% of measured time. Good news.</div>';
  }

  // ---- below-plan speed tests ----
  const plan = R.plan.down_mbps;
  if (!plan) {
    document.getElementById('speedWrap').innerHTML =
      '<div class="muted">Set your plan speed in Settings (plan_down_mbps) to flag speed tests that under-deliver.</div>';
  } else {
    const tests = R.speedtests.filter(t => Date.parse(t.ts) >= cut);
    // below-plan bar: thresholds.plan_pct.fair (default 80% of advertised,
    // a common informal bar for "not delivering the plan")
    const cutPct = R.plan_pct_fair || 80;
    const below = tests.filter(t => t.download_mbps < (cutPct / 100) * plan).sort((a, b) => a.download_mbps - b.download_mbps);
    const vantage = R.monitor_location
      ? ' Tests run from the monitor PC via ' + R.monitor_location + ' — they measure that path, not the ISP line directly.'
      : '';
    const head = tests.length
      ? below.length + ' of ' + tests.length + ' tests (' + Math.round(100 * below.length / tests.length) + '%) measured under ' + cutPct + '% of the ' + plan + ' Mbps plan.' + vantage
      : 'No speed tests in this window.';
    let html = '<div style="margin-bottom:8px;">' + esc(head) + '</div>';
    if (below.length) {
      html += '<table><thead><tr><th>When</th><th>Download</th><th>Upload</th><th>% of plan</th></tr></thead><tbody>'
        + below.slice(0, 10).map(t => '<tr><td>' + local(t.ts) + '</td><td>' + t.download_mbps + ' Mbps</td><td>'
          + (t.upload_mbps != null ? t.upload_mbps + ' Mbps' : '—') + '</td><td class="bad">'
          + Math.round(100 * t.download_mbps / plan) + '%</td></tr>').join('')
        + '</tbody></table>';
      if (below.length > 10) html += '<div class="footnote">Worst 10 shown of ' + below.length + '.</div>';
    }
    document.getElementById('speedWrap').innerHTML = html;
  }

  document.getElementById('footer').textContent =
    'Generated by Home Network Monitor v' + R.version + ' — connectivity measured every 15 seconds, speed tested every 30 minutes, from this household\\u2019s own connection.';
}

document.querySelectorAll('.controls button[data-days]').forEach(b => {
  b.addEventListener('click', () => {
    document.querySelectorAll('.controls button[data-days]').forEach(x => x.classList.toggle('active', x === b));
    render(parseInt(b.dataset.days, 10));
  });
});
render(30);
</script>
</body>
</html>
""".replace("__REPORT_JSON__", report_json).replace(
        "__TITLE__", html_escape(report.get("title") or "Home Network Monitor"))


if __name__ == "__main__":
    if "--version" in sys.argv:
        print(__version__)
        sys.exit(0)
    main()
