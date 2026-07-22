#!/usr/bin/env python3
"""
Home network monitor.

Runs continuously in the background and logs, to a local SQLite database:
  - ping results to your router (gateway) and to external internet targets
  - outage / degradation events, tagged as "gateway/wifi" vs "isp/internet"
    so you can tell WHERE a problem is happening
  - periodic Wi-Fi signal snapshots for this Mac
  - periodic LAN device scans (arp cache) so you can see what's connected
  - periodic speed tests (download/upload/ping), if the `speedtest` CLI or
    the `speedtest-cli` python package is available
  - periodic public IP checks, logging an event whenever it changes (a
    common correlate of brief ISP-side disconnects/reconnects)

Designed to be started once (via launchd on macOS — see setup.sh / the
.plist files — or Task Scheduler on Windows, see setup.ps1) and left
running indefinitely. Safe to Ctrl-C and restart; it resumes logging
into the same database.

No third-party dependencies are required for the core monitoring loop
(ping/arp/wifi all shell out to built-in OS tools on macOS, Windows, and
Linux). Speed tests are optional and degrade gracefully if no speed test
tool is installed.
"""

import concurrent.futures
import ipaddress
import json
import os
import platform
import re
import shutil
import socket
import sqlite3
import struct
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

try:
    from version import __version__
except ImportError:  # partially-copied install (e.g. just this file on a Pi)
    __version__ = "0.0.0"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data", "network_monitor.db")
ROUTERS_CONFIG_PATH = os.path.join(BASE_DIR, "routers.json")
DEVICE_NAMES_PATH = os.path.join(BASE_DIR, "devices.json")

# External targets used to determine "is the internet actually reachable".
# A mix of anycast resolvers so a single provider outage doesn't look like
# your internet is down.
EXTERNAL_TARGETS = ["1.1.1.1", "8.8.8.8", "9.9.9.9"]

PING_INTERVAL_SEC = 15          # how often to ping gateway + external targets
ROUTER_PING_INTERVAL_SEC = 15   # how often to ping each router in routers.json
WIFI_SNAPSHOT_INTERVAL_SEC = 5 * 60   # Wi-Fi signal snapshot cadence
DEVICE_SCAN_INTERVAL_SEC = 5 * 60     # LAN device scan cadence
SPEEDTEST_INTERVAL_SEC = 30 * 60      # speed test cadence
PUBLIC_IP_CHECK_INTERVAL_SEC = 10 * 60  # public IP check cadence
DNS_CHECK_INTERVAL_SEC = 60             # DNS resolution health check cadence

# The constants above are the DEFAULTS; config.json may override any of
# them via an "intervals" block ({"ping": 30, "speedtest": 3600, ...}),
# editable from the Settings UI and hot-reloaded — each loop re-reads its
# interval before sleeping, so a change applies after at most one old
# cycle, no restart. Values are clamped to these bounds so a typo can't
# hammer the LAN (too low) or effectively disable a check (too high).
# NOTE: these are SLEEP times — the real cadence is sleep + work. With
# several ARP-only routers the router loop spends ~15s in timeouts, so a
# 15s setting yields ~30s between checks (the dashboard footers show the
# measured cadence for exactly this reason).
# settings_api.py validates against the same bounds, and dashboard.py
# mirrors defaults+bounds for the cadence footers — update all three.
INTERVAL_DEFAULTS = {
    "ping": PING_INTERVAL_SEC,
    "router": ROUTER_PING_INTERVAL_SEC,
    "wifi": WIFI_SNAPSHOT_INTERVAL_SEC,
    "devices": DEVICE_SCAN_INTERVAL_SEC,
    "speedtest": SPEEDTEST_INTERVAL_SEC,
    "public_ip": PUBLIC_IP_CHECK_INTERVAL_SEC,
    "dns": DNS_CHECK_INTERVAL_SEC,
    "iot": 30,                      # watched IoT device liveness cadence
}
INTERVAL_BOUNDS = {         # seconds: (min, max)
    "ping": (5, 300),
    "router": (10, 600),
    "wifi": (60, 3600),
    "devices": (60, 3600),
    "speedtest": (600, 24 * 3600),  # a test moves real data — don't spam
    "public_ip": (120, 3600),
    "dns": (15, 900),
    "iot": (10, 600),
}

# Domains used to test DNS resolution (rotated one per check). Large,
# always-resolvable names — if these fail, DNS is broken for everything.
# www.speedtest.net is deliberately in the rotation: the Ookla CLI's own
# config-host lookup is the most common speed-test failure ("Couldn't
# resolve host name"), so when a test fails there's a same-minute DNS
# data point for exactly that name.
DNS_TEST_DOMAINS = ["apple.com", "google.com", "cloudflare.com", "www.speedtest.net"]

# Cap on user-defined ping targets (config.json "custom_targets") — each
# one costs a ping burst per ping cycle.
MAX_CUSTOM_TARGETS = 5

# Ports tried when a router doesn't answer ping — most routers serve their
# admin page on 80/443 even when they're configured to ignore ICMP.
ROUTER_TCP_PORTS = (80, 443)

# Closed-port liveness probe for routers that ignore ping AND run no web
# server (old APs/bridges): a live host answers a SYN on a closed port
# with RST ("connection refused"), which proves it's on the wire RIGHT
# NOW — unlike the ARP cache, whose entries linger ~20 minutes after a
# device dies. Port 9 (discard) is essentially never open on home gear.
ROUTER_PROBE_PORT = 9

# Plain-text "what's my IP" endpoints, tried in order until one works. All
# three are simple, free, no-signup services widely used for this exact
# purpose (no request body, no auth, just your IP echoed back as text).
PUBLIC_IP_SERVICES = [
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
    "https://icanhazip.com",
]

OUTAGE_FAILURE_THRESHOLD = 3    # consecutive failed checks before declaring an outage
DEGRADED_LATENCY_MS = 150       # avg latency above this over the rolling window = degraded
DEGRADED_LOSS_PCT = 20          # packet loss % above this over the rolling window = degraded
ROLLING_WINDOW = 20             # number of recent external pings considered for degradation

# The three trigger constants above are DEFAULTS — config.json may carry a
# "detection" block ({"outage_fails": 3, "degraded_latency_ms": 150,
# "degraded_loss_pct": 20}, editable from Settings → General) that
# overrides them, hot-reloaded via detection() below. Display ratings
# (dashboard "thresholds") and event triggers used to be separate worlds:
# a household could rate 100ms as HIGH on the cards while no degraded
# event fired until 150ms, and nothing explained the disagreement.
# Bounds keep a typo from making detection hair-trigger or comatose;
# settings_api's validator mirrors them — update both together.
DETECTION_DEFAULTS = {
    "outage_fails": OUTAGE_FAILURE_THRESHOLD,
    "degraded_latency_ms": DEGRADED_LATENCY_MS,
    "degraded_loss_pct": DEGRADED_LOSS_PCT,
}
DETECTION_BOUNDS = {            # (min, max)
    "outage_fails": (2, 10),
    "degraded_latency_ms": (50, 1000),
    "degraded_loss_pct": (5, 80),
}
PING_BURST_COUNT = 3            # pings per target per check (real per-check loss, see ping_burst)

# Micro-outage ("blip") flap detection: failed runs shorter than
# OUTAGE_FAILURE_THRESHOLD recover before an outage is declared, so they
# used to vanish entirely — yet "drops for 20 seconds, several times an
# hour" is precisely the intermittent-line signature ISPs ask about.
# Each blip is logged to the blips table; this many within the window
# raises a kind='instability' event (closed after a blip-free window).
INSTABILITY_BLIP_COUNT = 4
INSTABILITY_WINDOW_SEC = 3600

IS_MACOS = platform.system() == "Darwin"
IS_WINDOWS = platform.system() == "Windows"

# On Windows, every subprocess would otherwise flash a console window
# (ping runs every 15 seconds — that's a strobe light). CREATE_NO_WINDOW
# suppresses them; on macOS/Linux this is an empty dict and changes nothing.
SUBPROCESS_EXTRA = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}

# How long to keep high-volume history rows (pings, router_pings, wifi,
# devices, public_ip). Events and speed tests are tiny and kept forever.
RETENTION_DAYS = 90

_lock = threading.Lock()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def log(msg):
    print(f"[{now_iso()}] {msg}", flush=True)


def log_error(msg):
    """Errors go to stderr so they land in logs/monitor.err.log — a bare
    `except: pass` was previously hiding real failures (see scan_devices)."""
    print(f"[{now_iso()}] ERROR: {msg}", file=sys.stderr, flush=True)


# Under pythonw.exe (how the Windows scheduled task runs this script) there
# is no console: sys.stdout/sys.stderr are None and any print() would crash.
# Send output to log files instead — the same role launchd's
# StandardOutPath/StandardErrorPath plays on macOS.
if os.name == "nt" and (sys.stdout is None or sys.stderr is None):
    _log_dir = os.path.join(BASE_DIR, "logs")
    os.makedirs(_log_dir, exist_ok=True)
    if sys.stdout is None:
        sys.stdout = open(os.path.join(_log_dir, "monitor.out.log"), "a", buffering=1, encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.path.join(_log_dir, "monitor.err.log"), "a", buffering=1, encoding="utf-8")


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    # WAL lets dashboard.py read while the monitor threads write, without
    # either side hitting "database is locked"; busy_timeout covers the rest.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS pings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            target TEXT NOT NULL,
            target_type TEXT NOT NULL,   -- 'gateway' or 'external'
            success INTEGER NOT NULL,
            latency_ms REAL
        );
        CREATE INDEX IF NOT EXISTS idx_pings_ts ON pings(ts);

        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            start_ts TEXT NOT NULL,
            end_ts TEXT,
            kind TEXT NOT NULL,          -- 'outage', 'degraded', or 'ip_change'
            scope TEXT NOT NULL,         -- 'internet' (isp), 'gateway' (local wifi/router), or 'router' (a router from routers.json)
            note TEXT,
            router_name TEXT             -- set when scope='router': which router from routers.json
        );

        CREATE TABLE IF NOT EXISTS router_pings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            name TEXT NOT NULL,
            ip TEXT NOT NULL,
            success INTEGER NOT NULL,
            latency_ms REAL
        );
        CREATE INDEX IF NOT EXISTS idx_router_pings_ts ON router_pings(ts);

        CREATE TABLE IF NOT EXISTS public_ip (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            ip TEXT,
            error TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_public_ip_ts ON public_ip(ts);

        CREATE TABLE IF NOT EXISTS speedtests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            download_mbps REAL,
            upload_mbps REAL,
            ping_ms REAL,
            server TEXT,
            error TEXT
        );

        CREATE TABLE IF NOT EXISTS devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            ip TEXT,
            mac TEXT,
            hostname TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_devices_ts ON devices(ts);
        -- iot_loop resolves each watched MAC's latest IP every ~30s; without
        -- this the MAX(id) GROUP BY mac subquery scans the whole snapshot log
        CREATE INDEX IF NOT EXISTS idx_devices_mac ON devices(mac);

        CREATE TABLE IF NOT EXISTS wifi (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            ssid TEXT,
            rssi_dbm REAL,
            noise_dbm REAL,
            channel TEXT,
            tx_rate_mbps REAL
        );
        CREATE INDEX IF NOT EXISTS idx_wifi_ts ON wifi(ts);

        CREATE TABLE IF NOT EXISTS dns_checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            domain TEXT NOT NULL,
            success INTEGER NOT NULL,
            latency_ms REAL
        );
        CREATE INDEX IF NOT EXISTS idx_dns_checks_ts ON dns_checks(ts);

        -- hourly neighbor-network snapshot: one row per visible BSSID,
        -- rows of one scan share a ts (channel-congestion advice)
        CREATE TABLE IF NOT EXISTS wifi_scan (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            ssid TEXT,
            bssid TEXT,
            channel TEXT,
            band TEXT,
            signal_pct REAL
        );
        CREATE INDEX IF NOT EXISTS idx_wifi_scan_ts ON wifi_scan(ts);

        -- micro-outages: failed-ping runs that recovered BEFORE the outage
        -- threshold. Individually invisible in the events log, but a burst
        -- of them is the "line is flapping" signature ISPs actually act on.
        CREATE TABLE IF NOT EXISTS blips (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,            -- start of the failed run
            end_ts TEXT NOT NULL,        -- first successful check after it
            target_class TEXT NOT NULL,  -- 'gateway' or 'internet'
            failed_checks INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_blips_ts ON blips(ts);

        -- watched-IoT-device liveness samples (devices.json entries with
        -- "watch": true). Keyed by MAC — the stable identity — with the
        -- IP resolved per pass, so DHCP drift doesn't fork history.
        -- Deliberately NOT router_pings: that table is name-keyed and the
        -- dashboard derives a fallback router list from its history, so
        -- IoT rows there would resurrect as phantom routers.
        CREATE TABLE IF NOT EXISTS iot_pings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            mac TEXT NOT NULL,
            name TEXT,
            ip TEXT,
            success INTEGER NOT NULL,
            latency_ms REAL,
            method TEXT              -- icmp/tcp/probe/arp, like router_pings
        );
        CREATE INDEX IF NOT EXISTS idx_iot_pings_ts ON iot_pings(ts);

        -- daily traceroute-based double-NAT check (~1 row/day, kept forever)
        CREATE TABLE IF NOT EXISTS topology_checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            private_hops INTEGER,
            hop_ips TEXT,          -- JSON array of the first hops
            double_nat INTEGER,    -- 1/0, NULL = inconclusive
            cgnat INTEGER,         -- 1 when a 100.64/10 hop was seen
            error TEXT
        );
        """
    )
    # Migrations for databases created before these columns existed
    # (CREATE TABLE IF NOT EXISTS above only applies to brand-new databases).
    for migration in (
        "ALTER TABLE events ADD COLUMN router_name TEXT",
        "ALTER TABLE router_pings ADD COLUMN method TEXT",  # 'icmp' or 'tcp'
        # Bufferbloat columns: the Ookla CLI reports latency measured
        # *while* the line is saturated — the number that explains "speed
        # tests look fine but calls stutter". NULL = old row / old CLI /
        # python-fallback tester (none of which measure it).
        "ALTER TABLE speedtests ADD COLUMN jitter_ms REAL",
        "ALTER TABLE speedtests ADD COLUMN loaded_latency_down_ms REAL",
        "ALTER TABLE speedtests ADD COLUMN loaded_latency_up_ms REAL",
        "ALTER TABLE speedtests ADD COLUMN packet_loss_pct REAL",
        # which AP the monitor PC is associated to (roaming detection) and
        # which band; macOS redacts BSSID without location permission -> NULL
        "ALTER TABLE wifi ADD COLUMN bssid TEXT",
        "ALTER TABLE wifi ADD COLUMN band TEXT",
        # Ping bursts: sent/received per check give REAL per-check packet
        # loss instead of inferring loss from whole checks failing.
        # NULL = old single-ping rows.
        "ALTER TABLE pings ADD COLUMN sent INTEGER",
        "ALTER TABLE pings ADD COLUMN received INTEGER",
        # Which resolver answered: 'system' (the OS path every app uses),
        # 'gateway' (the router's DNS proxy, queried directly), or a public
        # resolver IP. NULL = old rows (treated as 'system').
        "ALTER TABLE dns_checks ADD COLUMN resolver TEXT",
        # Flight-recorder snapshot (JSON: traceroute, per-resolver DNS, ARP,
        # router liveness) captured in the first seconds of an outage —
        # the "where did the path die" evidence that's gone after recovery.
        "ALTER TABLE events ADD COLUMN evidence TEXT",
    ):
        try:
            conn.execute(migration)
        except sqlite3.OperationalError:
            pass  # column already exists
    conn.commit()
    return conn


def db_execute(conn, sql, params=()):
    with _lock:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur


def db_query(conn, sql, params=()):
    """Read under the same lock as writes — the threads share ONE sqlite
    connection, and interleaving a read into another thread's write is
    undefined even with check_same_thread=False."""
    with _lock:
        return conn.execute(sql, params).fetchall()


def close_dangling_events(conn):
    """Open events ('Ongoing' in the dashboard) are tracked by row id in
    memory only — if the monitor restarts mid-outage, the old row's end_ts
    stays NULL forever and the dashboard shows a permanent "Ongoing" outage.
    On startup, close anything left open by a previous run; if the outage is
    actually still happening, the fresh run re-detects and re-logs it within
    ~45 seconds anyway."""
    cur = db_execute(
        conn,
        "UPDATE events SET end_ts = ?, "
        "note = COALESCE(note, '') || ' [auto-closed: monitor restarted, true end time unknown]' "
        "WHERE end_ts IS NULL",
        (now_iso(),),
    )
    if cur.rowcount:
        log(f"closed {cur.rowcount} event(s) left open by a previous run")


# ---------------------------------------------------------------------------
# Extra routers (routers.json)
# ---------------------------------------------------------------------------

def load_routers():
    """Load the optional list of extra routers/access points to monitor from
    routers.json (see README for the format). Returns [] if the file is
    missing, empty, or malformed — router monitoring is entirely optional."""
    if not os.path.exists(ROUTERS_CONFIG_PATH):
        return []
    try:
        with open(ROUTERS_CONFIG_PATH, encoding="utf-8") as f:
            raw = json.load(f)
        routers = []
        seen_names = set()
        for item in raw:
            name = (item.get("name") or "").strip()
            ip = (item.get("ip") or "").strip()
            if not name or not ip:
                continue
            if name in seen_names:
                log(f"warning: duplicate router name '{name}' in routers.json, skipping")
                continue
            seen_names.add(name)
            routers.append({"name": name, "ip": ip})
        return routers
    except Exception as e:
        log_error(f"failed to load routers.json ({e}) — router monitoring disabled")
        return []


# devices.json values are either a plain friendly-name string (the original
# format) or an object {"name": ..., "type": "camera", "watch": true} for
# IoT categorization / active watching. This normalizer is mirrored in
# dashboard.py and settings_api.py (the three deliberately don't import
# each other — partial installs run any one alone); update all together.
IOT_TYPES = ("camera", "intercom", "printer", "light", "plug", "speaker", "tv", "other")


def _device_meta_from_value(value):
    """One devices.json value -> {"name", "type", "watch"} or None."""
    if isinstance(value, str):
        name = value.strip()
        return {"name": name, "type": None, "watch": False} if name else None
    if isinstance(value, dict):
        name = str(value.get("name") or "").strip()
        if not name:
            return None
        typ = str(value.get("type") or "").strip().lower() or None
        return {"name": name,
                "type": typ if typ in IOT_TYPES else None,
                "watch": bool(value.get("watch"))}
    return None


def load_device_meta():
    """Optional user-editable devices.json, normalized to
    {mac: {"name", "type", "watch"}}. Returns {} if missing/malformed —
    names/types are nice-to-have, and iot_loop treats {} as 'watch nothing'."""
    if not os.path.exists(DEVICE_NAMES_PATH):
        return {}
    try:
        with open(DEVICE_NAMES_PATH, encoding="utf-8") as f:
            raw = json.load(f)
        meta = {}
        for mac, value in raw.items():
            m = _device_meta_from_value(value)
            if m:
                meta[_normalize_mac(str(mac).strip().lower())] = m
        return meta
    except Exception as e:
        log_error(f"failed to load devices.json ({e}) — friendly device names disabled")
        return {}


def load_device_names():
    """Name-only view over load_device_meta() — keeps new-device event
    labels working unchanged (a raw dict value must never reach a note as
    its Python repr)."""
    return {mac: m["name"] for mac, m in load_device_meta().items()}


# ---------------------------------------------------------------------------
# Ping
# ---------------------------------------------------------------------------

def get_default_gateway():
    """Best-effort default gateway detection for macOS/Windows/Linux."""
    try:
        if IS_MACOS:
            out = subprocess.run(
                ["route", "-n", "get", "default"],
                capture_output=True, text=True, timeout=5, **SUBPROCESS_EXTRA,
            ).stdout
            m = re.search(r"gateway:\s*(\S+)", out)
            if m:
                return m.group(1)
        elif IS_WINDOWS:
            # `route print -4`: the 0.0.0.0/0 row's third column is the
            # gateway. Parsed by number patterns only, so localized column
            # headers don't matter.
            out = subprocess.run(
                ["route", "print", "-4"],
                capture_output=True, text=True, timeout=10, **SUBPROCESS_EXTRA,
            ).stdout
            m = re.search(r"^\s*0\.0\.0\.0\s+0\.0\.0\.0\s+(\d+\.\d+\.\d+\.\d+)", out, re.M)
            if m:
                return m.group(1)
            # fallback: PowerShell's route table (slower, but locale-proof)
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue | "
                 "Sort-Object RouteMetric | Select-Object -First 1).NextHop"],
                capture_output=True, text=True, timeout=15, **SUBPROCESS_EXTRA,
            ).stdout.strip()
            if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", out):
                return out
        else:
            out = subprocess.run(
                ["ip", "route", "show", "default"],
                capture_output=True, text=True, timeout=5, **SUBPROCESS_EXTRA,
            ).stdout
            m = re.search(r"default via (\S+)", out)
            if m:
                return m.group(1)
    except Exception:
        pass
    return None


def get_own_ip_and_prefix():
    """Best-effort: this machine's IPv4 address and subnet prefix length —
    used to ping-sweep the local subnet before reading the ARP cache."""
    ip = None
    try:
        if IS_MACOS:
            ip = subprocess.run(["ipconfig", "getifaddr", "en0"], capture_output=True, text=True, timeout=5, **SUBPROCESS_EXTRA).stdout.strip()
            if not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", ip or ""):
                ip = None
    except Exception:
        pass
    if not ip:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))  # UDP "connect" — no packet actually sent
            ip = s.getsockname()[0]
            s.close()
        except Exception:
            pass
    if not ip:
        return None, None

    prefix = 24  # sane default for home networks
    try:
        if IS_MACOS:
            out = subprocess.run(["ifconfig", "en0"], capture_output=True, text=True, timeout=5, **SUBPROCESS_EXTRA).stdout
            m = re.search(r"netmask (0x[0-9a-fA-F]+)", out)
            if m:
                prefix = bin(int(m.group(1), 16)).count("1")
        elif IS_WINDOWS:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-NetIPAddress -AddressFamily IPv4 -IPAddress '{ip}' -ErrorAction SilentlyContinue).PrefixLength"],
                capture_output=True, text=True, timeout=15, **SUBPROCESS_EXTRA,
            ).stdout
            m = re.search(r"\b(\d{1,2})\b", out)
            if m and 8 <= int(m.group(1)) <= 30:
                prefix = int(m.group(1))
    except Exception:
        pass
    return ip, prefix


# nmap is an optional upgrade for the device sweep — launchd's minimal
# PATH misses Homebrew locations, same story as the speedtest CLI.
KNOWN_NMAP_PATHS = [
    "/opt/homebrew/bin/nmap",   # Homebrew on Apple Silicon
    "/usr/local/bin/nmap",      # Homebrew on Intel
    "/opt/local/bin/nmap",      # MacPorts
]
if IS_WINDOWS:
    KNOWN_NMAP_PATHS = [
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Nmap", "nmap.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "Nmap", "nmap.exe"),
    ]


def find_nmap_binary():
    found = shutil.which("nmap")
    if found:
        return found
    for path in KNOWN_NMAP_PATHS:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def nmap_sweep_subnet(own_ip, prefix, nmap_bin):
    """Host-discovery sweep via nmap (-sn: no port scan). Serves the same
    purpose as ping_sweep_subnet — populating the ARP cache before it's
    read — but probes each host several ways (ICMP echo + TCP 80/443),
    so devices that filter ping get caught more reliably, and nmap's
    parallelism finishes the /24 faster. Unprivileged mode is fine for
    this; no root needed. Returns True if the sweep ran."""
    if not own_ip:
        return False
    try:
        network = ipaddress.ip_network(f"{own_ip}/{prefix}", strict=False)
    except Exception:
        return False
    try:
        subprocess.run(
            [nmap_bin, "-sn", "-T4", "--max-retries", "1", str(network)],
            capture_output=True, text=True, timeout=180, **SUBPROCESS_EXTRA,
        )
        return True
    except subprocess.TimeoutExpired:
        log_error("nmap sweep timed out after 180s — using built-in ping sweep this cycle")
    except Exception as e:
        log_error(f"nmap sweep failed ({e!r}) — using built-in ping sweep this cycle")
    return False


def ping_sweep_subnet(own_ip, prefix, max_workers=64):
    """Actively ping every address on the local /24 (or whatever the actual
    prefix is) so each responsive device's ARP entry gets populated. Without
    this, `arp -a` only reflects devices this Mac has recently talked to
    *directly* — a guest's laptop that only ever talks to the router/internet
    can otherwise sit on the network invisibly, never showing up in the scan.
    This adds a burst of ~254 lightweight pings every device-scan cycle
    (default every 5 minutes); devices with a firewall that blocks ICMP echo
    (e.g. macOS "stealth mode") still won't respond to this, so it's a real
    improvement but not a 100% guarantee."""
    if not own_ip:
        return
    try:
        network = ipaddress.ip_network(f"{own_ip}/{prefix}", strict=False)
        hosts = list(network.hosts())
    except Exception:
        return
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        list(pool.map(lambda h: ping_once(str(h), timeout_sec=1), hosts))


def ping_once(target, timeout_sec=2):
    """Ping a single host once. Returns (success, latency_ms)."""
    try:
        if IS_MACOS:
            cmd = ["ping", "-c", "1", "-t", str(timeout_sec), target]
        elif IS_WINDOWS:
            # -n count, -w timeout in MILLISECONDS
            cmd = ["ping", "-n", "1", "-w", str(int(timeout_sec * 1000)), target]
        else:
            cmd = ["ping", "-c", "1", "-W", str(timeout_sec), target]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec + 2, **SUBPROCESS_EXTRA)
        if result.returncode != 0:
            return False, None
        if IS_WINDOWS:
            # Windows ping exits 0 even for "Destination host unreachable"
            # replies (the reply came from a router, not the target). A real
            # echo reply always carries a TTL field — and "TTL" isn't
            # localized, unlike the rest of ping's output.
            if "ttl=" not in result.stdout.lower():
                return False, None
            # "time=23ms" / "time<1ms" — but the word "time" IS localized
            # (temps=, Zeit=, tiempo=...), so match the =/< number ms shape.
            m = re.search(r"[=<]\s*(\d+(?:[.,]\d+)?)\s*ms", result.stdout)
            if m:
                return True, float(m.group(1).replace(",", "."))
            return True, None
        m = re.search(r"time[=<]([\d.]+)", result.stdout)
        if m:
            return True, float(m.group(1))
        return True, None
    except Exception:
        return False, None


def ping_burst(target, count=3, timeout_sec=2):
    """Ping a host `count` times in one subprocess call. Returns
    (received, sent, avg_latency_ms). One packet per check can't tell
    "5% loss" from "perfect" — a burst measures real per-check loss, which
    is what the degradation detector actually wants to know.

    Parsing counts per-reply lines rather than trusting the summary line:
    the summary is localized ("Packets"/"Paquets"/...), but a real echo
    reply always carries TTL (Windows) / time= (unix), same trick as
    ping_once. Windows quirk: "Destination host unreachable" replies come
    from a router, exit 0, and have no TTL — counting TTL lines handles
    that for free."""
    try:
        if IS_MACOS:
            cmd = ["ping", "-c", str(count), "-t", str(timeout_sec), target]
        elif IS_WINDOWS:
            cmd = ["ping", "-n", str(count), "-w", str(int(timeout_sec * 1000)), target]
        else:
            cmd = ["ping", "-c", str(count), "-W", str(timeout_sec), target]
        # worst case: every packet times out, plus interpacket gaps
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=count * (timeout_sec + 1) + 3, **SUBPROCESS_EXTRA)
        out = result.stdout
        if IS_WINDOWS:
            received = out.lower().count("ttl=")
            times = [float(m.replace(",", ".")) for m in
                     re.findall(r"[=<]\s*(\d+(?:[.,]\d+)?)\s*ms", out)]
        else:
            received = len(re.findall(r"ttl=\d+", out, re.I))
            times = [float(m) for m in re.findall(r"time[=<]([\d.]+)", out)]
        # keep exactly one latency sample per received reply (Windows also
        # prints Minimum/Maximum/Average lines that match the ms regex)
        times = times[:received]
        avg = round(sum(times) / len(times), 3) if times else None
        return received, count, avg
    except Exception:
        return 0, count, None


def tcp_check(ip, ports=ROUTER_TCP_PORTS, timeout_sec=1.5):
    """Fallback reachability probe for hosts that ignore ping: try a TCP
    connection to their web-admin ports. Returns (success, connect_ms).
    A refused connection still proves the host is alive, but in practice
    routers accept on 80/443, so treat only a completed connect as up."""
    for port in ports:
        try:
            t0 = time.perf_counter()
            with socket.create_connection((ip, port), timeout=timeout_sec):
                return True, round((time.perf_counter() - t0) * 1000, 3)
        except OSError:
            continue
    return False, None


def rst_probe(ip, port=ROUTER_PROBE_PORT, timeout_sec=3):
    """Liveness probe for ping-deaf, portless routers: connect() to a
    closed port. ConnectionRefusedError means the host sent back an RST —
    it is alive this instant, and the refusal round-trip is a real latency
    sample. Timeout / host-unreachable means ARP itself got no answer (or
    a firewall silently ate the SYN — the caller falls through to the
    neighbor-state check for that case, so a paranoid-but-alive host
    can't regress to "down"). Returns (alive, latency_ms).
    NB the 3s default: Windows retries the SYN a couple of times after an
    RST before surfacing WSAECONNREFUSED, so a "refused" verdict takes
    ~2.1s there — a 2s timeout would race it and lose (measured on the
    live install; unix refusals return in milliseconds)."""
    t0 = time.perf_counter()
    try:
        with socket.create_connection((ip, port), timeout=timeout_sec):
            pass
        # port 9 actually open — odd, but an answer is an answer
        return True, round((time.perf_counter() - t0) * 1000, 3)
    except ConnectionRefusedError:
        return True, round((time.perf_counter() - t0) * 1000, 3)
    except OSError:
        return False, None


def _neighbor_state_windows(ip):
    """The neighbor-cache STATE for one IP via netsh, lowercased
    ('reachable', 'stale', 'probe', 'unreachable', ...). The state is
    what presence can't tell you: 'reachable' means an ARP reply arrived
    within the last ~30s, while a months-dead device can still SIT in the
    table with a cached MAC. Returns '' when the IP has no entry at all,
    None when netsh itself failed (caller falls back to presence)."""
    try:
        out = subprocess.run(["netsh", "interface", "ipv4", "show", "neighbors"],
                             capture_output=True, text=True, timeout=5, **SUBPROCESS_EXTRA).stdout
    except Exception:
        return None
    for line in out.splitlines():
        m = re.match(rf"^\s*{re.escape(ip)}\s+\S+\s+(\S+)\s*$", line)
        if m:
            return m.group(1).strip().lower()
    return ""


def _arp_presence(ip):
    """The old presence-only check: a valid MAC in the ARP table. Kept as
    the unix path and the Windows fallback — its entries linger ~20 min
    after a device dies, so it says "was here recently", not "is here"."""
    try:
        if IS_WINDOWS:
            # `arp -a <ip>` prints the entry (dash-separated MAC) or an
            # error. Match the MAC shape; reject broadcast/zero entries.
            out = subprocess.run(["arp", "-a", ip], capture_output=True, text=True, timeout=5, **SUBPROCESS_EXTRA).stdout
            m = re.search(r"(([0-9a-fA-F]{2}-){5}[0-9a-fA-F]{2})", out)
            return bool(m) and m.group(1).lower() not in ("ff-ff-ff-ff-ff-ff", "00-00-00-00-00-00")
        out = subprocess.run(["arp", "-n", ip], capture_output=True, text=True, timeout=5, **SUBPROCESS_EXTRA).stdout
        return bool(re.search(r"at\s+[0-9a-fA-F]{1,2}:", out)) and "incomplete" not in out.lower()
    except Exception:
        return False


def arp_alive(ip):
    """Last-resort liveness check: does the host answer ARP? A host that
    does is physically on the network even if it filters ping, has no web
    ports, and drops closed-port SYNs (this network's Buffalo APs do all
    three). The preceding ping/TCP/RST attempts each forced an ARP
    resolution, so the OS has JUST tried to verify this neighbor.

    Windows: read the neighbor STATE instead of trusting cache presence —
    'reachable' = answered an ARP probe seconds ago (alive), while a dead
    device's entry decays Stale→Probe→Unreachable within ~10s of our
    traffic. This cuts silent-router down-detection from ~20 min (cache
    linger — the old presence check's blind spot) to a couple of check
    cycles. Transient states (stale/delay/probe = verification in flight)
    get two short re-reads; if the verdict still hasn't landed — or netsh
    speaks a language we don't recognize — fall back to the presence
    check, so this tier can only ever get MORE accurate, never become a
    new source of false downs. macOS/Linux keep the presence check (BSD
    arp exposes no state column)."""
    if IS_WINDOWS:
        for attempt in range(3):
            state = _neighbor_state_windows(ip)
            if state is None:
                return _arp_presence(ip)          # netsh failed
            if state in ("", "unreachable", "incomplete"):
                return False
            if state in ("reachable", "permanent"):
                return True
            if attempt < 2:
                time.sleep(3)   # stale/delay/probe: NUD verdict lands in seconds
        return _arp_presence(ip)                   # unresolved (or localized)
    return _arp_presence(ip)


def check_router(ip):
    """Ping first; then a TCP web-port probe; then a closed-port RST
    probe; then the ARP cache as a last resort. Returns (success,
    latency_ms, method): 'icmp', 'tcp', 'probe', or 'arp'. 'probe'
    proves liveness THIS cycle — silent-router down-detection drops from
    ~20 min (ARP-cache linger) to ~3 checks. 'arp' remains only for
    hosts whose firewall drops the probe SYN."""
    ok, latency = ping_once(ip)
    if ok:
        return True, latency, "icmp"
    ok, latency = tcp_check(ip)
    if ok:
        return True, latency, "tcp"
    ok, latency = rst_probe(ip)
    if ok:
        # Windows surfaces "refused" only after ~2s of SYN retries, so the
        # measured time is retry ceremony, not network RTT — recording it
        # would put a fake 2000ms line on the per-router chart. Unix
        # refusals return in real round-trip time and are worth keeping.
        return True, (None if IS_WINDOWS else latency), "probe"
    if arp_alive(ip):
        return True, None, "arp"
    return False, None, "icmp"


# ---------------------------------------------------------------------------
# DNS health
# ---------------------------------------------------------------------------

def check_dns(domain):
    """Time one DNS resolution using the system resolver (same path every
    app on this Mac uses). Returns (success, latency_ms)."""
    try:
        t0 = time.perf_counter()
        socket.getaddrinfo(domain, 80, type=socket.SOCK_STREAM)
        return True, round((time.perf_counter() - t0) * 1000, 1)
    except socket.gaierror:
        return False, None
    except Exception as e:
        log_error(f"dns check for {domain} failed unexpectedly: {e!r}")
        return False, None


def dns_query_direct(server_ip, domain, timeout_sec=2):
    """One A-record query sent straight to a specific resolver over UDP —
    bypassing the OS resolver entirely (getaddrinfo can't target a server,
    and its cache would make the timing a lie anyway). Hand-rolled packet
    because stdlib has no DNS client: 12-byte header (RD set), one
    question, A/IN. Success = matching id, RCODE 0, at least one answer.
    Returns (success, latency_ms)."""
    qid = int.from_bytes(os.urandom(2), "big")
    header = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    try:
        qname = b"".join(bytes([len(p)]) + p.encode("ascii") for p in domain.split("."))
    except (UnicodeEncodeError, ValueError):
        return False, None
    query = header + qname + b"\x00" + struct.pack(">HH", 1, 1)  # QTYPE=A, QCLASS=IN
    t0 = time.perf_counter()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(timeout_sec)
            s.sendto(query, (server_ip, 53))
            data, _addr = s.recvfrom(512)
        ms = round((time.perf_counter() - t0) * 1000, 1)
        if len(data) < 12 or data[:2] != query[:2]:
            return False, None
        flags, = struct.unpack(">H", data[2:4])
        ancount, = struct.unpack(">H", data[6:8])
        return (flags & 0x000F) == 0 and ancount > 0, ms
    except OSError:
        return False, None


def dns_loop(conn, gateway):
    """DNS is the classic 'internet feels broken but pings are fine'
    failure: 1.1.1.1 answers ping while name resolution is dead, so
    browsing breaks with no outage on the chart. Check resolution health
    every minute and log an event when it's down.

    Each cycle checks the SYSTEM resolver (the path every app actually
    uses — this one drives outage events) plus each resolver in the
    comparison set directly. The split is the diagnosis: system+gateway
    failing while 1.1.1.1/8.8.8.8 answer = the router's DNS proxy is
    wedged (reboot it); everything failing = upstream/line problem."""
    consecutive_fail = 0
    open_event_id = None
    failed_domains = []   # domains tried during the current failure streak
    i = 0
    # Direct-comparison resolvers: the gateway (= the router's DNS proxy,
    # which is what DHCP points most home clients at) and two majors that
    # are also ping targets — so "pingable but not answering DNS" becomes
    # a visible, distinct state.
    direct_resolvers = ([("gateway", gateway)] if gateway else []) + \
                       [("1.1.1.1", "1.1.1.1"), ("8.8.8.8", "8.8.8.8")]
    while True:
        domain = DNS_TEST_DOMAINS[i % len(DNS_TEST_DOMAINS)]
        i += 1
        ok, latency = check_dns(domain)
        ts = now_iso()
        db_execute(
            conn,
            "INSERT INTO dns_checks (ts, domain, success, latency_ms, resolver) VALUES (?,?,?,?,?)",
            (ts, domain, int(ok), latency, "system"),
        )
        for label, server_ip in direct_resolvers:
            d_ok, d_ms = dns_query_direct(server_ip, domain)
            db_execute(
                conn,
                "INSERT INTO dns_checks (ts, domain, success, latency_ms, resolver) VALUES (?,?,?,?,?)",
                (ts, domain, int(d_ok), d_ms, label),
            )
        if ok:
            consecutive_fail = 0
            failed_domains = []
            if open_event_id is not None:
                db_execute(conn, "UPDATE events SET end_ts=? WHERE id=?", (ts, open_event_id))
                open_event_id = None
        else:
            consecutive_fail += 1
            if domain not in failed_domains:
                failed_domains.append(domain)
            if consecutive_fail >= detection("outage_fails") and open_event_id is None:
                # Name the domains that failed: "it wasn't just one weird
                # site" is the difference between a DNS outage and a CDN blip
                # when reading the log later.
                tried = ", ".join(failed_domains[:4])
                cur = db_execute(
                    conn,
                    "INSERT INTO events (start_ts, kind, scope, note) VALUES (?,?,?,?)",
                    (ts, "outage", "dns",
                     f"DNS lookups failing (tried: {tried}) — websites won't load by name even if pings still work"),
                )
                open_event_id = cur.lastrowid
                start_evidence_capture(conn, cur.lastrowid, gateway, "outage/dns")
        time.sleep(check_interval("dns"))


# ---------------------------------------------------------------------------
# Wi-Fi signal (macOS: system_profiler; Windows: netsh wlan)
# ---------------------------------------------------------------------------

def _wifi_snapshot_windows():
    """`netsh wlan show interfaces` — parsed leniently because its field
    labels are localized. SSID/BSSID keep their names across locales; the
    signal percentage is found by its % shape. netsh reports quality %
    rather than dBm, so convert with the common approximation
    dBm ≈ (quality / 2) - 100 — good enough to chart trends."""
    try:
        out = subprocess.run(
            ["netsh", "wlan", "show", "interfaces"],
            capture_output=True, text=True, timeout=10, **SUBPROCESS_EXTRA,
        ).stdout
        # first SSID line that isn't the BSSID line
        ssid = None
        m = re.search(r"^\s*SSID\s*:\s*(.+?)\s*$", out, re.M)
        if m:
            ssid = m.group(1)
        if not ssid:
            return None  # not connected over Wi-Fi (or no Wi-Fi adapter)
        rssi = None
        m = re.search(r":\s*(\d{1,3})\s*%", out)
        if m:
            rssi = round(int(m.group(1)) / 2 - 100)
        channel = None
        m = re.search(r"^\s*(?:Channel|Kanal|Canal)\s*:\s*(\d+)", out, re.M | re.I)
        if m:
            channel = m.group(1)
        tx_rate = None
        m = re.search(r"\(M?bit/s\)\s*:\s*([\d.,]+)|\(Mbps\)\s*:\s*([\d.,]+)", out)
        if m:
            tx_rate = (m.group(1) or m.group(2)).replace(",", ".")
        # BSSID = which AP we're associated to; the label isn't localized.
        bssid = None
        m = re.search(r"^\s*BSSID\s*:\s*([0-9A-Fa-f:]{17})", out, re.M)
        if m:
            bssid = m.group(1).lower()
        # Band: match the "5 GHz" VALUE shape (Win11 has a Band line whose
        # label localizes, but "GHz" doesn't); else derive from channel —
        # honest caveat: 6 GHz channel numbers overlap 2.4/5 GHz ones, so
        # the fallback can mislabel 6E networks.
        band = None
        m = re.search(r":\s*([\d.,]+)\s*GHz", out)
        if m:
            band = m.group(1).replace(",", ".")
        elif channel is not None:
            band = "2.4" if int(channel) <= 14 else "5"
        return {"ssid": ssid, "rssi_dbm": rssi, "noise_dbm": None,
                "channel": channel, "tx_rate_mbps": tx_rate,
                "bssid": bssid, "band": band}
    except Exception:
        return None


def get_wifi_snapshot():
    """Return dict with ssid/rssi/noise/channel/tx_rate for this machine's
    Wi-Fi, or None where unsupported (Linux) / not connected via Wi-Fi."""
    if IS_WINDOWS:
        return _wifi_snapshot_windows()
    if not IS_MACOS:
        return None
    try:
        out = subprocess.run(
            ["system_profiler", "SPAirPortDataType", "-json"],
            capture_output=True, text=True, timeout=10, **SUBPROCESS_EXTRA,
        ).stdout
        data = json.loads(out)
        for item in data.get("SPAirPortDataType", []):
            for iface in item.get("spairport_airport_interfaces", []):
                current = iface.get("spairport_current_network_information")
                if current:
                    # channel string looks like "36 (5GHz, 80MHz)" — band is
                    # inside it. BSSID: modern macOS redacts it without
                    # location permission, so roaming detection is
                    # Windows-only and we store NULL here.
                    chan = current.get("spairport_network_channel")
                    band = None
                    m = re.search(r"([\d.]+)\s*GHz", str(chan or ""))
                    if m:
                        band = m.group(1)
                    return {
                        "ssid": current.get("_name"),
                        "rssi_dbm": current.get("spairport_signal_noise", "").split(" / ")[0].replace(" dBm", "").strip() or None,
                        "noise_dbm": current.get("spairport_signal_noise", "").split(" / ")[-1].replace(" dBm", "").strip() or None,
                        "channel": chan,
                        "tx_rate_mbps": current.get("spairport_network_rate"),
                        "bssid": None,
                        "band": band,
                    }
    except Exception:
        pass
    return None


def scan_wifi_networks():
    """List neighboring Wi-Fi networks (one dict per visible BSSID) for
    channel-congestion advice. Run hourly, NOT per-snapshot: asking the
    adapter for a scan can briefly disturb our own Wi-Fi.

    Windows 11 note: without Location permission for desktop apps,
    `netsh wlan show networks` returns an empty list — treated as
    'no data', never as an error."""
    nets = []
    try:
        if IS_WINDOWS:
            out = subprocess.run(
                ["netsh", "wlan", "show", "networks", "mode=bssid"],
                capture_output=True, text=True, timeout=20, **SUBPROCESS_EXTRA,
            ).stdout
            # Blocks look like:  SSID 3 : name / BSSID 1 : aa:bb:.. /
            # Signal : 88% / Channel : 6  — labels localize except
            # SSID/BSSID, so signal/channel are matched by value shape.
            ssid = None
            cur = None
            for line in out.splitlines():
                m = re.match(r"^SSID\s+\d+\s*:\s*(.*)$", line.strip())
                if m:
                    ssid = m.group(1).strip() or None
                    continue
                m = re.match(r"^\s*BSSID\s+\d+\s*:\s*([0-9A-Fa-f:]{17})", line)
                if m:
                    cur = {"ssid": ssid, "bssid": m.group(1).lower(),
                           "channel": None, "band": None, "signal_pct": None}
                    nets.append(cur)
                    continue
                if cur is not None:
                    m = re.search(r":\s*(\d{1,3})\s*%", line)
                    if m and cur["signal_pct"] is None:
                        cur["signal_pct"] = int(m.group(1))
                        continue
                    m = re.search(r"^\s*[^:]+:\s*(\d{1,3})\s*$", line)
                    if m and cur["channel"] is None and int(m.group(1)) <= 196:
                        cur["channel"] = m.group(1)
                        cur["band"] = "2.4" if int(m.group(1)) <= 14 else "5"
        elif IS_MACOS:
            # same system_profiler call the snapshot uses — other networks
            # ride along for free
            out = subprocess.run(
                ["system_profiler", "SPAirPortDataType", "-json"],
                capture_output=True, text=True, timeout=15, **SUBPROCESS_EXTRA,
            ).stdout
            data = json.loads(out)
            for item in data.get("SPAirPortDataType", []):
                for iface in item.get("spairport_airport_interfaces", []):
                    for net in iface.get("spairport_airport_other_local_wireless_networks", []) or []:
                        chan = str(net.get("spairport_network_channel") or "")
                        m = re.match(r"(\d+)", chan)
                        b = re.search(r"([\d.]+)\s*GHz", chan)
                        nets.append({"ssid": net.get("_name"), "bssid": None,
                                     "channel": m.group(1) if m else None,
                                     "band": b.group(1) if b else None,
                                     "signal_pct": None})
    except Exception as e:
        log_error(f"wifi neighbor scan failed: {e!r}")
    return nets


# ---------------------------------------------------------------------------
# LAN device scan (arp cache)
# ---------------------------------------------------------------------------

def _is_multicast_or_broadcast(ip, mac):
    """arp -a lists IGMP/mDNS multicast group memberships (224.0.0.0/4,
    239.x for SSDP/UPnP, etc.) and the broadcast address alongside real
    devices. Those aren't physical hardware on your LAN — filter them out."""
    try:
        first_octet = int(ip.split(".")[0])
        if 224 <= first_octet <= 239:  # IPv4 multicast range
            return True
    except (ValueError, IndexError):
        pass
    if ip == "255.255.255.255":
        return True
    mac_norm = mac.lower()
    if mac_norm in ("ff:ff:ff:ff:ff:ff", "(incomplete)"):
        return True
    if mac_norm.startswith("1:0:5e") or mac_norm.startswith("01:00:5e"):  # IPv4 multicast MAC prefix
        return True
    if mac_norm.startswith("33:33"):  # IPv6 multicast MAC prefix
        return True
    return False


def _normalize_mac(mac):
    """arp sometimes omits leading zeros per octet (e.g. '1:0:5e:0:0:fb');
    pad each octet to 2 hex digits for consistent display/storage."""
    parts = mac.split(":")
    if len(parts) == 6 and all(re.fullmatch(r"[0-9a-fA-F]{1,2}", p) for p in parts):
        return ":".join(p.zfill(2) for p in parts).lower()
    return mac


def _resolve_hostnames(ips, max_workers=16, overall_timeout_sec=15):
    """Reverse-DNS a batch of IPs concurrently, with a hard overall time
    budget. Returns {ip: hostname or None}. Unresolved/slow entries just
    come back None — hostnames are nice-to-have, never worth blocking the
    scan for."""
    results = {ip: None for ip in ips}
    if not ips:
        return results

    def lookup(ip):
        try:
            return ip, socket.gethostbyaddr(ip)[0]
        except (socket.herror, socket.gaierror, OSError):
            return ip, None

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
    try:
        futures = [pool.submit(lookup, ip) for ip in ips]
        done, _not_done = concurrent.futures.wait(futures, timeout=overall_timeout_sec)
        for f in done:
            try:
                ip, name = f.result()
                if name:
                    results[ip] = name
            except Exception:
                pass
    finally:
        # Don't wait for stragglers stuck in slow resolver timeouts.
        pool.shutdown(wait=False, cancel_futures=True)
    return results


def scan_devices():
    """Read the ARP cache and return the real devices in it.

    Uses `arp -an` (numeric) rather than `arp -a`: plain `arp -a` does a
    reverse-DNS lookup for every single entry, and once ping_sweep_subnet()
    populates ~190 entries that can take well over 10 seconds — which blew
    the subprocess timeout and (because the exception was silently
    swallowed) made this function return 0 devices while a shell `arp -a`
    looked perfectly fine. `-n` skips DNS entirely, so it returns in
    milliseconds regardless of table size; hostnames are then resolved
    separately with a bounded time budget in _resolve_hostnames().

    On Windows, `arp -a` never does reverse-DNS (so it's already fast) and
    prints dash-separated MACs in an 'IP  MAC  type' table — parsed here by
    the IP/MAC shapes alone, so localized column headers don't matter."""
    devices = []
    arp_cmd = ["arp", "-a"] if IS_WINDOWS else ["arp", "-an"]
    try:
        result = subprocess.run(arp_cmd, capture_output=True, text=True, timeout=30, **SUBPROCESS_EXTRA)
        if result.returncode != 0:
            log_error(f"{' '.join(arp_cmd)} exited {result.returncode}: {result.stderr.strip()[:200]}")
        for line in result.stdout.splitlines():
            if IS_WINDOWS:
                # format: "  192.168.1.1     aa-bb-cc-dd-ee-ff     dynamic"
                m = re.match(r"\s*(\d+\.\d+\.\d+\.\d+)\s+(([0-9a-fA-F]{2}-){5}[0-9a-fA-F]{2})\s", line)
                if m:
                    ip, mac = m.group(1), m.group(2).replace("-", ":")
                    if _is_multicast_or_broadcast(ip, mac):
                        continue
                    devices.append({"ip": ip, "mac": _normalize_mac(mac), "hostname": None})
                continue
            # macOS/Linux format: hostname (ip) at mac [ether] on iface ...
            m = re.match(r"(\S+)?\s*\(([\d.]+)\)\s+at\s+([0-9a-fA-F:]+)", line)
            if m:
                hostname, ip, mac = m.group(1), m.group(2), m.group(3)
                if _is_multicast_or_broadcast(ip, mac):
                    continue
                devices.append({"ip": ip, "mac": _normalize_mac(mac), "hostname": hostname if hostname != "?" else None})
    except subprocess.TimeoutExpired:
        log_error(f"{' '.join(arp_cmd)} timed out after 30s — ARP cache scan skipped this cycle")
        return devices
    except Exception as e:
        log_error(f"scan_devices failed: {e!r}")
        return devices

    # arp -an never returns names, so fill them in ourselves (bounded).
    try:
        hostnames = _resolve_hostnames([d["ip"] for d in devices if not d["hostname"]])
        for d in devices:
            if not d["hostname"]:
                d["hostname"] = hostnames.get(d["ip"])
    except Exception as e:
        log_error(f"hostname resolution failed (device list still returned): {e!r}")
    return devices


def arp_table():
    """Quick {mac: ip} snapshot of the live ARP cache — the same parsing as
    scan_devices() minus hostname resolution, cheap enough to run on demand.
    Used by iot_loop to catch DHCP drift: a watched MAC that stopped
    answering at its last-known IP may simply have leased a new one."""
    table = {}
    arp_cmd = ["arp", "-a"] if IS_WINDOWS else ["arp", "-an"]
    try:
        result = subprocess.run(arp_cmd, capture_output=True, text=True, timeout=15, **SUBPROCESS_EXTRA)
        for line in result.stdout.splitlines():
            if IS_WINDOWS:
                m = re.match(r"\s*(\d+\.\d+\.\d+\.\d+)\s+(([0-9a-fA-F]{2}-){5}[0-9a-fA-F]{2})\s", line)
                if m:
                    ip, mac = m.group(1), m.group(2).replace("-", ":")
                    if not _is_multicast_or_broadcast(ip, mac):
                        table[_normalize_mac(mac)] = ip
                continue
            m = re.match(r"(\S+)?\s*\(([\d.]+)\)\s+at\s+([0-9a-fA-F:]+)", line)
            if m:
                ip, mac = m.group(2), m.group(3)
                if not _is_multicast_or_broadcast(ip, mac):
                    table[_normalize_mac(mac)] = ip
    except Exception as e:
        log_error(f"arp_table failed: {e!r}")
    return table


# ---------------------------------------------------------------------------
# Speed test (optional)
# ---------------------------------------------------------------------------

# Background services started via launchd get a bare-bones PATH that doesn't
# include Homebrew's install locations (/opt/homebrew on Apple Silicon,
# /usr/local on Intel) — shutil.which("speedtest") alone can miss a tool
# that's clearly installed and visible from an interactive Terminal. Check
# the common install locations directly as a fallback.
KNOWN_SPEEDTEST_PATHS = [
    "/opt/homebrew/bin/speedtest",   # Homebrew on Apple Silicon
    "/usr/local/bin/speedtest",      # Homebrew on Intel
    "/opt/local/bin/speedtest",      # MacPorts
]
if IS_WINDOWS:
    KNOWN_SPEEDTEST_PATHS = [
        os.path.expandvars(r"%ProgramData%\chocolatey\bin\speedtest.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WindowsApps\speedtest.exe"),  # winget
    ]


def find_speedtest_binary():
    found = shutil.which("speedtest")
    if found:
        return found
    for path in KNOWN_SPEEDTEST_PATHS:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def is_ookla_cli(path):
    """The Homebrew formula named "speedtest-cli" is an old, unmaintained
    community tool with a completely different (and incompatible) CLI —
    it happens to also install a binary literally named `speedtest`, so a
    path/name check alone can't tell them apart. --version output can:
    the real one prints "Speedtest by Ookla"."""
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10, **SUBPROCESS_EXTRA).stdout
        return "ookla" in out.lower()
    except Exception:
        return False


def run_speedtest():
    """Try Ookla's official `speedtest` CLI first, then the speedtest-cli
    python package. Returns dict or dict with 'error' key."""
    speedtest_bin = find_speedtest_binary()
    if speedtest_bin and not is_ookla_cli(speedtest_bin):
        return {
            "error": (
                f"found a 'speedtest' command at {speedtest_bin} but it's not the "
                "official Ookla CLI (likely the old community speedtest-cli tool, "
                "which uses different flags). Fix: brew uninstall speedtest-cli && "
                "brew tap teamookla/speedtest && brew install speedtest — or just "
                "re-run setup.sh, which now does this automatically."
            )
        }
    if speedtest_bin:
        result = None
        try:
            # Explicitly pass HOME — launchd's environment for background
            # services doesn't always set it, and the Ookla CLI needs it to
            # read/write its license-acceptance config.
            env = dict(os.environ)
            env.setdefault("HOME", os.path.expanduser("~"))
            result = subprocess.run(
                [speedtest_bin, "--accept-license", "--accept-gdpr", "-f", "json"],
                capture_output=True, text=True, timeout=90, env=env, **SUBPROCESS_EXTRA,
            )
            data = json.loads(result.stdout)
            # Bufferbloat data: download/upload.latency.iqm is the latency
            # measured WHILE the pipe is saturated. A big jump over the idle
            # ping is why "speed tests look fine but calls stutter". Older
            # CLI builds omit these (and packetLoss needs a loss-capable
            # server), hence the guarded .get() chains → None when absent.
            def _r(v, nd=1):
                return round(v, nd) if isinstance(v, (int, float)) else None
            return {
                "download_mbps": round(data["download"]["bandwidth"] * 8 / 1_000_000, 2),
                "upload_mbps": round(data["upload"]["bandwidth"] * 8 / 1_000_000, 2),
                "ping_ms": round(data["ping"]["latency"], 1),
                "server": data.get("server", {}).get("name"),
                "jitter_ms": _r((data.get("ping") or {}).get("jitter")),
                "loaded_latency_down_ms": _r(((data.get("download") or {}).get("latency") or {}).get("iqm")),
                "loaded_latency_up_ms": _r(((data.get("upload") or {}).get("latency") or {}).get("iqm")),
                "packet_loss_pct": _r(data.get("packetLoss"), 2),
            }
        except Exception as e:
            detail = f"speedtest CLI failed: {e}"
            if result is not None:
                stderr_snip = (result.stderr or "").strip()[:300]
                stdout_snip = (result.stdout or "").strip()[:300]
                detail += f" | rc={result.returncode} stderr={stderr_snip!r} stdout={stdout_snip!r}"
            return {"error": detail}

    try:
        import speedtest  # speedtest-cli package
        st = speedtest.Speedtest()
        st.get_best_server()
        download = st.download() / 1_000_000
        upload = st.upload() / 1_000_000
        return {
            "download_mbps": round(download, 2),
            "upload_mbps": round(upload, 2),
            "ping_ms": round(st.results.ping, 1),
            "server": st.results.server.get("host"),
        }
    except ImportError:
        return {"error": "no speed test tool installed (see README)"}
    except Exception as e:
        return {"error": f"speedtest-cli failed: {e}"}


# ---------------------------------------------------------------------------
# Public IP
# ---------------------------------------------------------------------------

def get_public_ip():
    """Try each service in PUBLIC_IP_SERVICES until one returns a plausible
    IPv4/IPv6 address. Returns (ip, error) — exactly one is None."""
    import urllib.request
    import urllib.error

    for url in PUBLIC_IP_SERVICES:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": f"home-network-monitor/{__version__}"})
            with urllib.request.urlopen(req, timeout=6) as resp:
                text = resp.read().decode("utf-8", errors="ignore").strip()
                # sanity check: looks like an IP, not an error page
                if re.fullmatch(r"[0-9a-fA-F:.]+", text) and len(text) <= 45:
                    return text, None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            continue
    return None, "all public IP services unreachable"


# ---------------------------------------------------------------------------
# Monitoring loops
# ---------------------------------------------------------------------------

def ping_loop(conn, gateway):
    consecutive_external_fail = 0
    consecutive_gateway_fail = 0
    open_event = {"internet": None, "gateway": None}
    recent_external = []  # list of (success, best_latency, sent, received)
    # blip run tracking: start ts of the current failed run per class, plus
    # whether the gateway was also down during the internet run (if so the
    # "internet blip" is really the gateway's fault — don't double-log it).
    fail_run = {"gateway": None, "internet": None}   # class -> start ts
    internet_run_had_gw_fail = False
    custom_state = {}   # target name -> {"fails": n, "open_id": event id}

    def record_blip(target_class, start_ts, end_ts, n_checks):
        """A failed run recovered before reaching the outage threshold:
        log it, then see whether blips are now frequent enough to call the
        line unstable (flapping)."""
        db_execute(
            conn,
            "INSERT INTO blips (ts, end_ts, target_class, failed_checks) VALUES (?,?,?,?)",
            (start_ts, end_ts, target_class, n_checks),
        )
        window_start = (datetime.now(timezone.utc)
                        - timedelta(seconds=INSTABILITY_WINDOW_SEC)).isoformat()
        rows = db_query(conn, "SELECT COUNT(*) FROM blips WHERE ts >= ?", (window_start,))
        n_recent = rows[0][0] if rows else 0
        if n_recent >= INSTABILITY_BLIP_COUNT and open_event.get("instability") is None:
            note = (f"Connection flapping — {n_recent} brief drops within an hour, "
                    f"each too short to register as an outage")
            cur = db_execute(
                conn,
                "INSERT INTO events (start_ts, kind, scope, note) VALUES (?,?,?,?)",
                (end_ts, "instability", target_class, note),
            )
            open_event["instability"] = cur.lastrowid

    while True:
        ts = now_iso()
        # trigger thresholds re-read each pass (hot-reloaded from config)
        fails_needed = detection("outage_fails")

        gw_ok, gw_latency = (False, None)
        if gateway:
            gw_recv, gw_sent, gw_latency = ping_burst(gateway, PING_BURST_COUNT)
            gw_ok = gw_recv > 0
            db_execute(
                conn,
                "INSERT INTO pings (ts, target, target_type, success, latency_ms, sent, received) VALUES (?,?,?,?,?,?,?)",
                (ts, gateway, "gateway", int(gw_ok), gw_latency, gw_sent, gw_recv),
            )

        ext_results = []   # (ok, avg_latency, sent, received) per target
        for target in EXTERNAL_TARGETS:
            recv, sent, latency = ping_burst(target, PING_BURST_COUNT)
            ok = recv > 0
            ext_results.append((ok, latency, sent, recv))
            db_execute(
                conn,
                "INSERT INTO pings (ts, target, target_type, success, latency_ms, sent, received) VALUES (?,?,?,?,?,?,?)",
                (ts, target, "external", int(ok), latency, sent, recv),
            )

        any_external_ok = any(ok for ok, _l, _s, _r in ext_results)
        best_latency = min((l for ok, l, _s, _r in ext_results if ok and l is not None), default=None)
        cycle_sent = sum(s for _o, _l, s, _r in ext_results)
        cycle_recv = sum(r for _o, _l, _s, r in ext_results)

        recent_external.append((any_external_ok, best_latency, cycle_sent, cycle_recv))
        recent_external = recent_external[-ROLLING_WINDOW:]

        # --- Outage detection (with blip tracking on the side) ---
        # A "run" of failed checks either grows into an outage (>= threshold)
        # or recovers early — in which case it becomes a blip. Runs that DID
        # become outages are already in the events log; logging them as blips
        # too would double-count.
        if gateway and not gw_ok:
            if consecutive_gateway_fail == 0:
                fail_run["gateway"] = ts
            consecutive_gateway_fail += 1
        else:
            if consecutive_gateway_fail and consecutive_gateway_fail < fails_needed:
                record_blip("gateway", fail_run["gateway"], ts, consecutive_gateway_fail)
            consecutive_gateway_fail = 0
            fail_run["gateway"] = None

        if not any_external_ok:
            if consecutive_external_fail == 0:
                fail_run["internet"] = ts
                internet_run_had_gw_fail = False
            if gateway and not gw_ok:
                internet_run_had_gw_fail = True
            consecutive_external_fail += 1
        else:
            if (consecutive_external_fail
                    and consecutive_external_fail < fails_needed
                    and not internet_run_had_gw_fail):
                # gateway was fine the whole run -> genuinely the line's fault
                record_blip("internet", fail_run["internet"], ts, consecutive_external_fail)
            consecutive_external_fail = 0
            fail_run["internet"] = None

        # Instability closes after a full blip-free window: flapping that
        # stopped an hour ago is over, even if no outage ever opened.
        if open_event.get("instability") is not None:
            window_start = (datetime.now(timezone.utc)
                            - timedelta(seconds=INSTABILITY_WINDOW_SEC)).isoformat()
            rows = db_query(conn, "SELECT COUNT(*) FROM blips WHERE ts >= ?", (window_start,))
            if not rows or rows[0][0] == 0:
                db_execute(conn, "UPDATE events SET end_ts=? WHERE id=?", (ts, open_event["instability"]))
                open_event["instability"] = None

        # Gateway (local wifi/router) outage takes priority in diagnosis: if the
        # gateway itself is unreachable, that's almost certainly the cause of any
        # external failures too.
        if gateway and consecutive_gateway_fail >= fails_needed:
            if open_event["gateway"] is None:
                cur = db_execute(
                    conn,
                    "INSERT INTO events (start_ts, kind, scope, note) VALUES (?,?,?,?)",
                    (ts, "outage", "gateway", "Router/gateway unreachable — local Wi-Fi or router issue"),
                )
                open_event["gateway"] = cur.lastrowid
                start_evidence_capture(conn, cur.lastrowid, gateway, "outage/gateway")
        elif open_event["gateway"] is not None:
            db_execute(conn, "UPDATE events SET end_ts=? WHERE id=?", (ts, open_event["gateway"]))
            open_event["gateway"] = None

        if consecutive_external_fail >= fails_needed:
            # Only log an "internet" outage if the gateway is fine (otherwise it's
            # a gateway outage, already captured above).
            if open_event["internet"] is None and consecutive_gateway_fail < fails_needed:
                cur = db_execute(
                    conn,
                    "INSERT INTO events (start_ts, kind, scope, note) VALUES (?,?,?,?)",
                    (ts, "outage", "internet", "Gateway reachable but internet targets unreachable — likely ISP outage"),
                )
                open_event["internet"] = cur.lastrowid
                start_evidence_capture(conn, cur.lastrowid, gateway, "outage/internet")
        elif open_event["internet"] is not None:
            db_execute(conn, "UPDATE events SET end_ts=? WHERE id=?", (ts, open_event["internet"]))
            open_event["internet"] = None

        # --- Degradation detection (slow, not down) ---
        if len(recent_external) >= 5:
            # Real packet loss across the window (bursts made this honest:
            # 1 lost packet in 9 is 11%, where whole-check granularity could
            # only ever say 0% or 100%).
            total_sent = sum(s for _o, _l, s, _r in recent_external)
            total_recv = sum(r for _o, _l, _s, r in recent_external)
            loss_pct = 100.0 * (1 - total_recv / total_sent) if total_sent else 0.0
            latencies = [l for ok, l, _s, _r in recent_external if ok and l is not None]
            avg_latency = sum(latencies) / len(latencies) if latencies else None

            is_degraded = (loss_pct > detection("degraded_loss_pct")
                           or (avg_latency and avg_latency > detection("degraded_latency_ms")))
            # Don't double-count a degradation event during an active outage.
            active_outage = open_event["internet"] is not None or open_event["gateway"] is not None

            if is_degraded and not active_outage:
                if open_event.get("degraded") is None:
                    note = f"loss={loss_pct:.0f}% avg_latency={avg_latency}"
                    cur = db_execute(
                        conn,
                        "INSERT INTO events (start_ts, kind, scope, note) VALUES (?,?,?,?)",
                        (ts, "degraded", "internet", note),
                    )
                    open_event["degraded"] = cur.lastrowid
                    start_evidence_capture(conn, cur.lastrowid, gateway, "degraded/internet")
            elif open_event.get("degraded") is not None:
                db_execute(conn, "UPDATE events SET end_ts=? WHERE id=?", (ts, open_event["degraded"]))
                open_event["degraded"] = None

        # --- user-defined custom targets (config.json custom_targets) ---
        # Pinged every cycle, stored under target_type='custom' keyed by the
        # target NAME (rename the host, keep the history). A target failing
        # while the INTERNET is fine opens its own scope='target' outage
        # event — "their end or the route there", not the house — so this
        # runs AFTER outage detection, when consecutive_external_fail is
        # current for the suppression check. Targets deleted from config
        # get their open events closed, mirroring the router hot reload.
        targets = custom_targets()
        active_names = set()
        for t in targets:
            active_names.add(t["name"])
            st = custom_state.setdefault(t["name"], {"fails": 0, "open_id": None})
            recv, sent, latency = ping_burst(t["host"], PING_BURST_COUNT)
            ok = recv > 0
            db_execute(
                conn,
                "INSERT INTO pings (ts, target, target_type, success, latency_ms, sent, received) VALUES (?,?,?,?,?,?,?)",
                (ts, t["name"], "custom", int(ok), latency, sent, recv),
            )
            if ok:
                st["fails"] = 0
                if st["open_id"] is not None:
                    db_execute(conn, "UPDATE events SET end_ts=? WHERE id=?", (ts, st["open_id"]))
                    st["open_id"] = None
            else:
                st["fails"] += 1
                if (st["fails"] >= fails_needed and st["open_id"] is None
                        and consecutive_external_fail < fails_needed):
                    cur = db_execute(
                        conn,
                        "INSERT INTO events (start_ts, kind, scope, note, router_name) VALUES (?,?,?,?,?)",
                        (ts, "outage", "target",
                         f'"{t["name"]}" ({t["host"]}) is unreachable while the internet is fine — '
                         "the problem is at their end or on the route there",
                         t["name"]),
                    )
                    st["open_id"] = cur.lastrowid
        for name in list(custom_state):
            if name not in active_names:
                if custom_state[name]["open_id"] is not None:
                    db_execute(conn, "UPDATE events SET end_ts=? WHERE id=?",
                               (ts, custom_state[name]["open_id"]))
                custom_state.pop(name)

        time.sleep(check_interval("ping"))


def wifi_loop(conn):
    # Roaming detection: seed the last-known AP from the DB (same idea as
    # public_ip_loop's seeding) so a monitor restart doesn't fake a roam.
    last_bssid, last_ssid = None, None
    try:
        rows = db_query(conn, "SELECT bssid, ssid FROM wifi WHERE bssid IS NOT NULL ORDER BY ts DESC LIMIT 1")
        if rows:
            last_bssid, last_ssid = rows[0][0], rows[0][1]
    except sqlite3.Error:
        pass
    last_neighbor_scan = None  # monotonic; time-based so a user-configured
    scan_logged_empty = False  # snapshot interval doesn't change scan cadence
    while True:
        snap = get_wifi_snapshot()
        if snap:
            ts = now_iso()
            db_execute(
                conn,
                "INSERT INTO wifi (ts, ssid, rssi_dbm, noise_dbm, channel, tx_rate_mbps, bssid, band)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (ts, snap.get("ssid"), snap.get("rssi_dbm"), snap.get("noise_dbm"),
                 snap.get("channel"), snap.get("tx_rate_mbps"), snap.get("bssid"), snap.get("band")),
            )
            # BSSID changed within the same SSID = we silently hopped to a
            # different AP. An SSID change is a network switch, not a roam.
            bssid = snap.get("bssid")
            if (bssid and last_bssid and bssid != last_bssid
                    and snap.get("ssid") == last_ssid):
                db_execute(
                    conn,
                    "INSERT INTO events (start_ts, end_ts, kind, scope, note) VALUES (?,?,?,?,?)",
                    (ts, ts, "wifi_roam", "lan",
                     f"This PC's Wi-Fi hopped to a different access point ({last_bssid} → {bssid})"
                     f" on \"{snap.get('ssid')}\""),
                )
            if bssid:
                last_bssid, last_ssid = bssid, snap.get("ssid")
        # hourly neighbor scan, only while the machine actually has Wi-Fi
        # in play (wall-clock, not cycle-counted — the snapshot interval
        # is user-configurable now)
        if snap and (last_neighbor_scan is None or time.monotonic() - last_neighbor_scan >= 3600):
            last_neighbor_scan = time.monotonic()
            nets = scan_wifi_networks()
            if nets:
                scan_logged_empty = False
                ts = now_iso()
                for n in nets:
                    db_execute(
                        conn,
                        "INSERT INTO wifi_scan (ts, ssid, bssid, channel, band, signal_pct) VALUES (?,?,?,?,?,?)",
                        (ts, n.get("ssid"), n.get("bssid"), n.get("channel"), n.get("band"), n.get("signal_pct")),
                    )
            elif not scan_logged_empty:
                scan_logged_empty = True
                log("wifi neighbor scan returned nothing (on Windows 11 this "
                    "usually means Location permission is off for desktop apps)")
        time.sleep(check_interval("wifi"))


# Shared device-scan state: device_loop and the web UI's on-demand
# "scan_now" command both go through run_device_scan, so the new-device
# baseline / first-scan-absorbs behavior is identical no matter which
# path triggered the sweep. The lock stops the two from sweeping the
# subnet concurrently (the scheduled cycle just waits its turn).
_device_scan_state = {"known_macs": None, "first_cycle": True,
                      "last_sweep_mode": None, "warned_no_ip": False}
_device_scan_lock = threading.Lock()


def run_device_scan(conn):
    """One full device sweep: populate ARP (nmap or ping sweep), read the
    ARP table, insert the census snapshot, raise new-device events.
    Returns how many devices the sweep saw."""
    with _device_scan_lock:
        st = _device_scan_state
        if st["known_macs"] is None:
            # Baseline = every MAC ever recorded. A MAC outside the
            # baseline is a brand-new device on the network — useful both
            # for "whose phone is that?" and as a light security signal.
            st["known_macs"] = set()
            try:
                with _lock:
                    st["known_macs"] = {row[0] for row in conn.execute(
                        "SELECT DISTINCT mac FROM devices WHERE mac IS NOT NULL")}
            except Exception as e:
                log_error(f"couldn't seed known-device baseline: {e!r}")

        # Re-detect every cycle: launchd starts this before Wi-Fi is up at
        # boot, and the Mac can move between networks without a restart.
        own_ip, prefix = get_own_ip_and_prefix()
        if not own_ip and not st["warned_no_ip"]:
            log("warning: couldn't determine this Mac's IP/subnet — "
                "device scan will fall back to whatever's already in the ARP cache "
                "(may miss devices that don't talk to this Mac directly). "
                "Will keep retrying every cycle.")
            st["warned_no_ip"] = True

        # Populate ARP entries for every responsive device. Prefer nmap
        # when installed (checked every cycle, so installing it later
        # gets picked up without a restart); fall back to the built-in
        # ping sweep otherwise.
        nmap_bin = find_nmap_binary()
        mode = "nmap" if nmap_bin else "ping sweep"
        if mode != st["last_sweep_mode"]:
            log(f"device discovery via {mode}")
            st["last_sweep_mode"] = mode
        swept = nmap_sweep_subnet(own_ip, prefix, nmap_bin) if nmap_bin else False
        if not swept:
            ping_sweep_subnet(own_ip, prefix)
        ts = now_iso()
        devices = scan_devices()
        if not devices:
            log_error("device scan returned 0 devices — not writing an empty snapshot "
                      "(the ARP cache should never be truly empty; see stderr above for the cause)")
        for d in devices:
            db_execute(
                conn,
                "INSERT INTO devices (ts, ip, mac, hostname) VALUES (?,?,?,?)",
                (ts, d["ip"], d["mac"], d["hostname"]),
            )

        # --- new-device events ---
        unseen = [d for d in devices if d["mac"] not in st["known_macs"]]
        if unseen:
            if st["first_cycle"]:
                # The first scan after a (re)start absorbs unseen MACs
                # silently: after a code change or a long stretch of broken
                # scans, dozens of "new" devices would otherwise flood the
                # event log at once, drowning the real signal.
                log(f"device baseline: absorbed {len(unseen)} previously-unrecorded "
                    "device(s) without events (first scan after startup)")
            else:
                names = load_device_names()
                for d in unseen:
                    label = names.get(d["mac"]) or d["hostname"] or "unknown device"
                    note = f"New device joined the network: {label} — {d['ip']} ({d['mac']})"
                    db_execute(
                        conn,
                        "INSERT INTO events (start_ts, end_ts, kind, scope, note) VALUES (?,?,?,?,?)",
                        (ts, ts, "new_device", "lan", note),
                    )
                    log(note)
            st["known_macs"].update(d["mac"] for d in unseen)
        st["first_cycle"] = False
        return len(devices)


def device_loop(conn):
    while True:
        run_device_scan(conn)
        time.sleep(check_interval("devices"))


def speedtest_loop(conn):
    while True:
        result = run_speedtest()
        if "error" in result and "download_mbps" not in result:
            # Retry once after a minute: transient hiccups (the Ookla CLI's
            # own config-host DNS lookup is the repeat offender on this
            # class of failure) used to burn the whole 30-min slot and
            # paint a red × on the chart. Only the final outcome is
            # recorded — the transient goes to the log, not the DB.
            log(f"speed test failed ({str(result['error'])[:120]}) — retrying once in 60s")
            time.sleep(60)
            result = run_speedtest()
        ts = now_iso()
        if "error" in result and "download_mbps" not in result:
            db_execute(conn, "INSERT INTO speedtests (ts, error) VALUES (?,?)", (ts, result["error"]))
        else:
            db_execute(
                conn,
                "INSERT INTO speedtests (ts, download_mbps, upload_mbps, ping_ms, server,"
                " jitter_ms, loaded_latency_down_ms, loaded_latency_up_ms, packet_loss_pct)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (ts, result.get("download_mbps"), result.get("upload_mbps"), result.get("ping_ms"), result.get("server"),
                 result.get("jitter_ms"), result.get("loaded_latency_down_ms"),
                 result.get("loaded_latency_up_ms"), result.get("packet_loss_pct")),
            )
        time.sleep(check_interval("speedtest"))


def public_ip_loop(conn):
    last_ip = None
    # Seed last_ip from the most recent successful reading, so a restart
    # doesn't log a spurious "change" against a blank slate.
    try:
        row = conn.execute("SELECT ip FROM public_ip WHERE ip IS NOT NULL ORDER BY ts DESC LIMIT 1").fetchone()
        if row:
            last_ip = row[0]
    except Exception:
        pass

    while True:
        ip, error = get_public_ip()
        ts = now_iso()
        db_execute(conn, "INSERT INTO public_ip (ts, ip, error) VALUES (?,?,?)", (ts, ip, error))

        if ip and last_ip and ip != last_ip:
            note = f"Public IP changed: {last_ip} → {ip}"
            db_execute(
                conn,
                "INSERT INTO events (start_ts, end_ts, kind, scope, note) VALUES (?,?,?,?,?)",
                (ts, ts, "ip_change", "internet", note),
            )
        if ip:
            last_ip = ip

        time.sleep(check_interval("public_ip"))


def _routers_file_stamp():
    """(mtime, size) of routers.json, or None if it doesn't exist — cheap
    change detection for hot reloading. The settings UI writes the file
    atomically (tmp + rename), so a changed stamp always means a complete
    new file."""
    try:
        st = os.stat(ROUTERS_CONFIG_PATH)
        return (st.st_mtime, st.st_size)
    except OSError:
        return None


def drop_gateway_dupes(routers, gateway):
    """Combo-box installs: a routers.json entry — typically the role='isp'
    Internet box when the ISP modem IS the house router — can have the
    default gateway's IP. That device is already monitored by the ping
    thread as the gateway, at ping cadence with bursts; monitoring it here
    too would record every outage twice (scope='gateway' AND
    scope='router'), fire duplicate alerts, and make the dashboard draw
    one device as two map nodes. Skip it — the dashboard merges its name
    onto the Main Router node instead."""
    kept = []
    for r in routers:
        if gateway and r["ip"] == gateway:
            log(f"router '{r['name']}' ({r['ip']}) is the default gateway — "
                "already covered by the gateway ping thread, skipping duplicate monitoring")
        else:
            kept.append(r)
    return kept


def router_loop(conn, routers, gateway=None):
    """Ping each configured router/access point independently, and track
    outages per-router the same way the main gateway is tracked — so a dead
    node in one room shows up as "Kitchen AP down" rather than just a vague
    slowdown.

    routers.json is hot-reloaded (same idea as devices.json in device_loop):
    when the file changes — e.g. saved from the settings UI — the new list
    takes effect within one 15s cycle, no restart needed."""
    routers = drop_gateway_dupes(routers, gateway)
    state = {r["name"]: {"consecutive_fail": 0, "open_event_id": None} for r in routers}
    stamp = _routers_file_stamp()

    while True:
        new_stamp = _routers_file_stamp()
        if new_stamp != stamp:
            stamp = new_stamp
            new_routers = drop_gateway_dupes(load_routers(), gateway)
            new_names = {r["name"] for r in new_routers}
            # A router that was removed while it had an open outage event
            # would otherwise show as "Ongoing" on the dashboard forever —
            # close it out, noting why.
            for name, st in state.items():
                if name not in new_names and st["open_event_id"] is not None:
                    db_execute(
                        conn,
                        "UPDATE events SET end_ts=?, note = note || ' [router removed from routers.json]' WHERE id=?",
                        (now_iso(), st["open_event_id"]),
                    )
            # Keep warm state (fail counts, open events) for surviving names
            # so an in-progress outage isn't reset by an unrelated edit.
            state = {name: state.get(name, {"consecutive_fail": 0, "open_event_id": None})
                     for name in new_names}
            routers = new_routers
            desc = ", ".join(f"{r['name']}={r['ip']}" for r in routers) if routers else "none"
            log(f"routers.json changed — now monitoring: [{desc}]")

        if not routers:
            time.sleep(check_interval("router"))
            continue

        ts = now_iso()
        for r in routers:
            name, ip = r["name"], r["ip"]
            ok, latency, method = check_router(ip)
            db_execute(
                conn,
                "INSERT INTO router_pings (ts, name, ip, success, latency_ms, method) VALUES (?,?,?,?,?,?)",
                (ts, name, ip, int(ok), latency, method),
            )

            st = state[name]
            if ok:
                st["consecutive_fail"] = 0
                if st["open_event_id"] is not None:
                    db_execute(conn, "UPDATE events SET end_ts=? WHERE id=?", (ts, st["open_event_id"]))
                    st["open_event_id"] = None
            else:
                st["consecutive_fail"] += 1
                if st["consecutive_fail"] >= detection("outage_fails") and st["open_event_id"] is None:
                    note = f"{name} ({ip}) unreachable"
                    cur = db_execute(
                        conn,
                        "INSERT INTO events (start_ts, kind, scope, note, router_name) VALUES (?,?,?,?,?)",
                        (ts, "outage", "router", note, name),
                    )
                    st["open_event_id"] = cur.lastrowid

        time.sleep(check_interval("router"))


# ---------------------------------------------------------------------------
# Watched IoT devices (devices.json entries with "watch": true)
# ---------------------------------------------------------------------------

def _devices_file_stamp():
    """(mtime, size) of devices.json for hot reload — same idiom as
    _routers_file_stamp; the settings UI writes the file atomically."""
    try:
        st = os.stat(DEVICE_NAMES_PATH)
        return (st.st_mtime, st.st_size)
    except OSError:
        return None


def _watched_devices():
    """The devices.json entries to actively watch: {mac: meta}."""
    return {mac: m for mac, m in load_device_meta().items() if m["watch"]}


def iot_loop(conn, gateway=None):
    """Actively liveness-check watched IoT devices (cameras, intercoms, ...)
    the way router_loop checks APs: the same 4-tier icmp→tcp→probe→arp
    ladder (cameras are often ping-deaf but answer on their RTSP/web TCP
    ports), per-device consecutive-fail tracking, and outage events with
    scope='iot' (router_name carries the device's display name).

    Devices are keyed by MAC (the stable identity); the IP is resolved each
    pass from the latest device-scan snapshot, with a live ARP re-check on
    failure to catch DHCP drift before counting a real fail. A MAC that has
    never been seen anywhere is not probed and never events — a typo'd MAC
    must not page forever.

    False-positive guard: when a device crosses the open threshold, the
    gateway is pinged first — if the monitor PC's own link is down, every
    watched device "fails" at once, and that story belongs to the
    gateway-outage event, not N bogus IoT outages. Fails keep counting
    during the hold, so the event opens promptly once the gateway is back."""
    watched = _watched_devices()
    state = {}   # mac -> {"consecutive_fail", "open_event_id", "ip"}
    stamp = _devices_file_stamp()
    if watched:
        desc = ", ".join(f"{m['name']}={mac}" for mac, m in watched.items())
        log(f"iot watch starting: [{desc}]")

    while True:
        new_stamp = _devices_file_stamp()
        if new_stamp != stamp:
            stamp = new_stamp
            new_watched = _watched_devices()
            # Un-watching/deleting a device mid-outage would leave a
            # permanent "Ongoing" event — close it out, noting why.
            for mac, st in state.items():
                if mac not in new_watched and st["open_event_id"] is not None:
                    db_execute(
                        conn,
                        "UPDATE events SET end_ts=?, note = note || ' [watch removed]' WHERE id=?",
                        (now_iso(), st["open_event_id"]),
                    )
            state = {mac: state.get(mac, {"consecutive_fail": 0, "open_event_id": None, "ip": None})
                     for mac in new_watched}
            watched = new_watched
            desc = ", ".join(f"{m['name']}={mac}" for mac, m in watched.items()) if watched else "none"
            log(f"devices.json changed — iot watch now: [{desc}]")

        if not watched:
            time.sleep(check_interval("iot"))
            continue

        # Resolve MAC -> IP from the device-scan census (one query for all;
        # the mac IN filter inside the subquery keeps it on idx_devices_mac
        # instead of aggregating the whole 90-day snapshot log).
        placeholders = ",".join("?" * len(watched))
        macs = tuple(watched.keys())
        try:
            rows = db_query(
                conn,
                f"SELECT mac, ip FROM devices WHERE id IN "
                f"(SELECT MAX(id) FROM devices WHERE mac IN ({placeholders}) GROUP BY mac)",
                macs)
            known_ips = {r[0]: r[1] for r in rows if r[1]}
        except sqlite3.Error as e:
            log_error(f"iot ip resolution query failed: {e!r}")
            known_ips = {}

        # At most one live `arp -a` per pass, fetched lazily on first need.
        live_arp = None

        def _live_arp():
            nonlocal live_arp
            if live_arp is None:
                live_arp = arp_table()
            return live_arp

        for mac, meta in watched.items():
            st = state.setdefault(mac, {"consecutive_fail": 0, "open_event_id": None, "ip": None})
            ip = known_ips.get(mac) or st["ip"]
            if not ip:
                # Never seen by any scan yet — check the live ARP cache
                # (covers a brand-new device between sweeps), else skip:
                # no rows, no events, dashboard shows "Never seen".
                ip = _live_arp().get(mac)
                if not ip:
                    continue
            if gateway and ip == gateway:
                # Someone watch-flagged the house router itself — the
                # gateway ping thread already covers it (combo-box analog);
                # probing here would double every outage.
                continue
            ts = now_iso()
            ok, latency, method = check_router(ip)
            if not ok:
                # DHCP drift check: did the MAC move to a different IP?
                fresh_ip = _live_arp().get(mac)
                if fresh_ip and fresh_ip != ip:
                    ip = fresh_ip
                    ok, latency, method = check_router(ip)
            st["ip"] = ip
            db_execute(
                conn,
                "INSERT INTO iot_pings (ts, mac, name, ip, success, latency_ms, method) VALUES (?,?,?,?,?,?,?)",
                (ts, mac, meta["name"], ip, int(ok), latency, method),
            )

            if ok:
                st["consecutive_fail"] = 0
                if st["open_event_id"] is not None:
                    db_execute(conn, "UPDATE events SET end_ts=? WHERE id=?", (ts, st["open_event_id"]))
                    st["open_event_id"] = None
            else:
                st["consecutive_fail"] += 1
                if st["consecutive_fail"] >= detection("outage_fails") and st["open_event_id"] is None:
                    if gateway and not ping_once(gateway)[0]:
                        continue   # hold: our own link is down, not the device
                    note = f"{meta['name']} ({ip}, {mac}) unreachable"
                    cur = db_execute(
                        conn,
                        "INSERT INTO events (start_ts, kind, scope, note, router_name) VALUES (?,?,?,?,?)",
                        (ts, "outage", "iot", note, meta["name"]),
                    )
                    st["open_event_id"] = cur.lastrowid

        time.sleep(check_interval("iot"))


# ---------------------------------------------------------------------------
# Command rail (web -> monitor) and on-demand tests
#
# The web process (serve.py/settings_api) and this monitor share no memory
# and must not import each other (partial installs run either one alone).
# Their only channel is two one-way JSON files, each with exactly ONE
# writer — which is what makes the design race-free without locks:
#   data/commands.json     written by settings_api, read here
#   data/test_status.json  written here, read by settings_api
# Both are written atomically (tmp + os.replace), same idiom as the
# settings UI's config saves and the routers.json hot reload.
# ---------------------------------------------------------------------------

COMMANDS_PATH = os.path.join(BASE_DIR, "data", "commands.json")
TEST_STATUS_PATH = os.path.join(BASE_DIR, "data", "test_status.json")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
COMMAND_POLL_SEC = 2     # os.stat every 2s is effectively free
COMMAND_TTL_SEC = 120    # ignore commands older than this (stale after a restart)


def _file_stamp(path):
    """(mtime, size) or None — cheap change detection, same trick as
    _routers_file_stamp but for any file."""
    try:
        st = os.stat(path)
        return (st.st_mtime, st.st_size)
    except OSError:
        return None


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# Hot-reloaded check intervals: config.json's "intervals" block over
# INTERVAL_DEFAULTS, clamped to INTERVAL_BOUNDS. Stamp-cached so the
# per-loop calls cost an os.stat, not a JSON parse. Threads share the
# cache without a lock — worst case under the GIL is a redundant reload.
_intervals_cache = {"stamp": ("never",), "vals": dict(INTERVAL_DEFAULTS)}


def check_interval(name):
    stamp = _file_stamp(CONFIG_PATH)
    if stamp != _intervals_cache["stamp"]:
        vals = dict(INTERVAL_DEFAULTS)
        raw = (_read_json(CONFIG_PATH) or {}).get("intervals")
        if isinstance(raw, dict):
            for k, v in raw.items():
                if k in vals and isinstance(v, (int, float)) and not isinstance(v, bool):
                    lo, hi = INTERVAL_BOUNDS[k]
                    vals[k] = int(min(hi, max(lo, v)))
        _intervals_cache["vals"] = vals
        _intervals_cache["stamp"] = stamp
    return _intervals_cache["vals"][name]


# Hot-reloaded event-trigger thresholds, same stamp-cache idiom as
# check_interval above (one os.stat per call, JSON parse only on change).
_detection_cache = {"stamp": ("never",), "vals": dict(DETECTION_DEFAULTS)}


def detection(name):
    stamp = _file_stamp(CONFIG_PATH)
    if stamp != _detection_cache["stamp"]:
        vals = dict(DETECTION_DEFAULTS)
        raw = (_read_json(CONFIG_PATH) or {}).get("detection")
        if isinstance(raw, dict):
            for k, v in raw.items():
                if k in vals and isinstance(v, (int, float)) and not isinstance(v, bool):
                    lo, hi = DETECTION_BOUNDS[k]
                    vals[k] = type(DETECTION_DEFAULTS[k])(min(hi, max(lo, v)))
        _detection_cache["vals"] = vals
        _detection_cache["stamp"] = stamp
    return _detection_cache["vals"][name]


# Hot-reloaded user-defined ping targets, same stamp-cache idiom again.
_targets_cache = {"stamp": ("never",), "vals": []}


def custom_targets():
    """config.json "custom_targets": [{name, host}] — the user's OWN
    destinations (a game server, the work VPN, grandma's router), pinged
    alongside the anycast set. The anycast targets are the easiest hosts
    on the internet to reach; "internet fine but Valorant unplayable" is
    invisible without asking the actual destination. Invalid entries are
    dropped, names deduped, list capped at MAX_CUSTOM_TARGETS."""
    stamp = _file_stamp(CONFIG_PATH)
    if stamp != _targets_cache["stamp"]:
        vals, seen = [], set()
        raw = (_read_json(CONFIG_PATH) or {}).get("custom_targets")
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                host = str(item.get("host") or "").strip()
                if name and host and name not in seen:
                    seen.add(name)
                    vals.append({"name": name, "host": host})
                if len(vals) >= MAX_CUSTOM_TARGETS:
                    break
        _targets_cache["vals"] = vals
        _targets_cache["stamp"] = stamp
    return _targets_cache["vals"]


def _write_json_atomic(path, obj):
    """tmp + fsync + os.replace, with the same Windows sharing-violation
    retry as settings_api.save_json_atomic (deliberately duplicated — the
    monitor must not import settings_api)."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f)
        f.flush()
        os.fsync(f.fileno())
    for attempt in range(3):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.1 * (attempt + 1))
    os.replace(tmp, path)


def _set_test_status(**fields):
    try:
        _write_json_atomic(TEST_STATUS_PATH, fields)
    except OSError as e:
        log_error(f"couldn't write test status: {e!r}")


def run_on_demand_test(conn, want_speedtest):
    """The 'Test now' button: a quick ping burst + DNS check (+ optional
    speed test), with per-phase progress in test_status.json. Results are
    ALSO inserted into the normal tables with the normal target types, so
    the next dashboard regen folds them into the charts."""
    started = now_iso()

    def phase(name):
        _set_test_status(state="running", phase=name, started_ts=started)

    results = {}
    phase("ping")
    gateway = get_default_gateway()
    for target, ttype in ((gateway, "gateway"), ("1.1.1.1", "external")):
        if not target:
            continue
        oks, lats = 0, []
        for _ in range(5):
            ok, lat = ping_once(target)
            db_execute(conn, "INSERT INTO pings (ts, target, target_type, success, latency_ms) VALUES (?,?,?,?,?)",
                       (now_iso(), target, ttype, int(ok), lat))
            if ok:
                oks += 1
                if lat is not None:
                    lats.append(lat)
        results[ttype] = {"sent": 5, "ok": oks,
                          "avg_ms": round(sum(lats) / len(lats), 1) if lats else None}

    phase("dns")
    domain = DNS_TEST_DOMAINS[0]
    ok, lat = check_dns(domain)
    db_execute(conn, "INSERT INTO dns_checks (ts, domain, success, latency_ms) VALUES (?,?,?,?)",
               (now_iso(), domain, int(ok), lat))
    results["dns"] = {"ok": bool(ok), "ms": lat, "domain": domain}

    if want_speedtest:
        phase("speedtest")
        st = run_speedtest()
        ts = now_iso()
        if "error" in st:
            db_execute(conn, "INSERT INTO speedtests (ts, error) VALUES (?,?)", (ts, st["error"]))
            results["speedtest"] = {"error": st["error"][:200]}
        else:
            db_execute(
                conn,
                "INSERT INTO speedtests (ts, download_mbps, upload_mbps, ping_ms, server,"
                " jitter_ms, loaded_latency_down_ms, loaded_latency_up_ms, packet_loss_pct)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (ts, st.get("download_mbps"), st.get("upload_mbps"), st.get("ping_ms"), st.get("server"),
                 st.get("jitter_ms"), st.get("loaded_latency_down_ms"),
                 st.get("loaded_latency_up_ms"), st.get("packet_loss_pct")),
            )
            results["speedtest"] = {"down": st.get("download_mbps"), "up": st.get("upload_mbps"),
                                    "ping": st.get("ping_ms")}

    _set_test_status(state="done", started_ts=started, finished_ts=now_iso(), results=results)


def command_loop(conn):
    """Poll data/commands.json for actions from the web UI. A malformed or
    stale command must never kill this thread — everything is guarded."""
    stamp = _file_stamp(COMMANDS_PATH)
    last_seen_id = None
    while True:
        time.sleep(COMMAND_POLL_SEC)
        try:
            new_stamp = _file_stamp(COMMANDS_PATH)
            if new_stamp == stamp:
                continue
            stamp = new_stamp
            cmd = _read_json(COMMANDS_PATH)
            if not isinstance(cmd, dict) or cmd.get("id") == last_seen_id:
                continue
            last_seen_id = cmd.get("id")
            # A command written while the monitor was off shouldn't fire on
            # startup minutes later — the user's browser has long moved on.
            issued = cmd.get("issued_ts")
            try:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(issued)).total_seconds()
            except (TypeError, ValueError):
                age = None
            if age is None or age > COMMAND_TTL_SEC:
                continue
            action = cmd.get("action")
            if action == "test_now":
                run_on_demand_test(conn, bool(cmd.get("speedtest")))
            elif action == "test_alert":
                send_test_alert()
            elif action == "scan_now":
                # on-demand device sweep from Settings → Devices; shares
                # the test-status file so the page can poll for completion
                started = now_iso()
                _set_test_status(state="running", phase="device scan",
                                 started_ts=started)
                n = run_device_scan(conn)
                _set_test_status(state="done", started_ts=started,
                                 finished_ts=now_iso(),
                                 results={"devices_found": n})
        except Exception as e:
            log_error(f"command loop error: {e!r}")
            _set_test_status(state="error", error=str(e)[:200])


# ---------------------------------------------------------------------------
# Topology / double-NAT check
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Flight recorder: evidence snapshot at the moment an event opens
# ---------------------------------------------------------------------------
# "The internet died at 02:14" tells you WHEN; a traceroute taken AT 02:14
# tells you WHERE the path stopped — and that information is unrecoverable
# five minutes after the line comes back. Captured in a background thread
# (a tracert on a dead line takes ~30s; the ping loop must not stall),
# written into events.evidence as JSON when done.

EVIDENCE_MAX_BYTES = 16384
EVIDENCE_MIN_GAP_SEC = 60
_evidence_gate = threading.Lock()
_evidence_last_start = [0.0]   # monotonic; list so the thread can mutate it


def start_evidence_capture(conn, event_id, gateway, trigger):
    """Fire-and-forget evidence capture for a just-opened event. Throttled:
    one failure usually opens several events at once (a dead gateway drags
    DNS down with it) and one snapshot covers them all — parallel tracerts
    fighting over a dead line would only slow each other into timeouts."""
    with _evidence_gate:
        now = time.monotonic()
        if now - _evidence_last_start[0] < EVIDENCE_MIN_GAP_SEC:
            db_execute(conn, "UPDATE events SET evidence=? WHERE id=?",
                       (json.dumps({"skipped": "snapshot already captured for a concurrent event"}),
                        event_id))
            return
        _evidence_last_start[0] = now
    threading.Thread(target=_capture_evidence,
                     args=(conn, event_id, gateway, trigger), daemon=True).start()


def _capture_evidence(conn, event_id, gateway, trigger):
    """The capture itself. Quick probes first (the network's state is
    moving), the slow traceroute last. Every step is individually guarded:
    partial evidence beats none, and this thread must never take the
    monitor down with it."""
    t_start = time.perf_counter()
    ev = {"captured_ts": now_iso(), "trigger": trigger}
    try:
        # Per-resolver DNS: separates "router's DNS proxy wedged" from
        # "upstream resolver down" from "everything dead".
        dns = {}
        ok, ms = check_dns("google.com")
        dns["system"] = {"ok": bool(ok), "ms": ms}
        for label, server in ([("gateway", gateway)] if gateway else []) + \
                             [("1.1.1.1", "1.1.1.1"), ("8.8.8.8", "8.8.8.8")]:
            ok, ms = dns_query_direct(server, "google.com")
            dns[label] = {"ok": bool(ok), "ms": ms}
        ev["dns"] = dns

        # Gateway liveness at the failure moment.
        if gateway:
            recv, sent, ms = ping_burst(gateway, 3, timeout_sec=1)
            ev["gateway_ping"] = {"received": recv, "sent": sent, "avg_ms": ms}

        # Which configured routers/APs were alive — "everything but the
        # internet answers" vs "half the LAN is dark" are different faults.
        # Values: 'ping' / 'web' (TCP 80/443) / 'probe' (closed-port RST —
        # how silent APs prove themselves) / 'no-reply'. 'no-reply' means
        # "answered nothing in a hurry", deliberately NOT "down".
        routers = {}
        for r in load_routers()[:12]:
            ok, _ms = ping_once(r["ip"], timeout_sec=1)
            if ok:
                routers[r["name"]] = "ping"
                continue
            ok_tcp, _ = tcp_check(r["ip"], timeout_sec=1)
            if ok_tcp:
                routers[r["name"]] = "web"
                continue
            ok_rst, _ = rst_probe(r["ip"], timeout_sec=1)
            routers[r["name"]] = "probe" if ok_rst else "no-reply"
        if routers:
            ev["routers_alive"] = routers

        # ARP table: proof of what was physically on the LAN.
        try:
            arp_cmd = ["arp", "-a"] if IS_WINDOWS else ["arp", "-an"]
            out = subprocess.run(arp_cmd, capture_output=True, text=True,
                                 timeout=10, **SUBPROCESS_EXTRA).stdout
            ev["arp"] = out.strip()[:4000]
        except Exception:
            pass

        # Traceroute last (slowest): where the path stopped. On a dead
        # line every hop times out — that emptiness is itself evidence.
        try:
            if IS_WINDOWS:
                cmd = ["tracert", "-d", "-h", "8", "-w", "800", "8.8.8.8"]
            else:
                cmd = ["traceroute", "-n", "-m", "8", "-w", "1", "-q", "1", "8.8.8.8"]
            out = subprocess.run(cmd, capture_output=True, text=True,
                                 timeout=90, **SUBPROCESS_EXTRA).stdout
            hops = [ln.strip() for ln in out.splitlines() if re.match(r"^\s*\d{1,2}\s", ln)]
            ev["traceroute"] = hops[:12]
        except Exception as e:
            ev["traceroute_error"] = repr(e)

        ev["capture_secs"] = round(time.perf_counter() - t_start, 1)
    except Exception as e:
        ev["error"] = repr(e)

    # Size cap: shrink then drop the ARP dump (the only big field) — never
    # slice the JSON string itself, a truncated blob wouldn't parse.
    blob = json.dumps(ev)
    if len(blob) > EVIDENCE_MAX_BYTES:
        ev["arp"] = (ev.get("arp") or "")[:1000]
        blob = json.dumps(ev)
    if len(blob) > EVIDENCE_MAX_BYTES:
        ev.pop("arp", None)
        blob = json.dumps(ev)
    try:
        db_execute(conn, "UPDATE events SET evidence=? WHERE id=?", (blob, event_id))
    except Exception as e:
        log_error(f"evidence write for event {event_id} failed: {e!r}")


def check_double_nat(target="8.8.8.8"):
    """Count private hops on the way out. Two (or more) RFC1918 hops before
    the first public one = two routers are each doing NAT — the classic
    ISP-box-in-front-of-your-own-router setup that breaks consoles, VoIP
    and port forwarding. A 100.64/10 hop is carrier-grade NAT: also shared
    address space, but at the ISP — not something the user can bridge.

    Returns {private_hops, hop_ips, double_nat, cgnat, error}; double_nat
    is None when the trace was inconclusive (timeouts before any public
    hop) — an inconclusive run must never produce a warning."""
    if IS_WINDOWS:
        cmd = ["tracert", "-d", "-h", "4", "-w", "1000", target]
    else:
        cmd = ["traceroute", "-n", "-m", "4", "-w", "1", "-q", "1", target]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=45, **SUBPROCESS_EXTRA).stdout
    except FileNotFoundError:
        return {"private_hops": None, "hop_ips": [], "double_nat": None, "cgnat": 0,
                "error": "traceroute not installed"}
    except subprocess.TimeoutExpired:
        return {"private_hops": None, "hop_ips": [], "double_nat": None, "cgnat": 0,
                "error": "traceroute timed out"}

    hops = []   # one entry per hop line: an IP string, or None for '*'
    for line in out.splitlines():
        # hop lines start with the hop number; the tracert header repeats
        # the target IP but never matches this anchor
        m = re.match(r"^\s*(\d{1,2})\s", line)
        if not m:
            continue
        ips = re.findall(r"(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})", line)
        hops.append(ips[-1] if ips else None)

    # The verdict comes from hop 2's address class, NOT from counting
    # private hops until a public one appears: many ISPs run 10.x/172.16.x
    # internally for several hops past the customer edge (this very network
    # shows 10.188.x at hop 3 inside the ISP), which would make the "count
    # to first public" approach permanently inconclusive — or worse, call
    # the ISP's own routing double NAT.
    #   hop2 in 192.168/16 -> a second HOME router is NATing (ISP cores
    #                          don't use 192.168): double NAT, confidently.
    #   hop2 in 100.64/10  -> carrier-grade NAT: shared addressing at the
    #                          ISP, not fixable in the house.
    #   hop2 public        -> single NAT, all good.
    #   hop2 in 10/8 etc.  -> ambiguous (home OR ISP addressing): stay
    #                          inconclusive rather than risk a false alarm.
    cgnat = 0
    double_nat = None
    leading_private = 0
    for ip in hops:
        if ip is None:
            break
        try:
            if not ipaddress.ip_address(ip).is_private:
                break
        except ValueError:
            break
        leading_private += 1
    h2 = hops[1] if len(hops) > 1 else None
    if h2 is not None:
        try:
            addr2 = ipaddress.ip_address(h2)
            if addr2 in ipaddress.ip_network("100.64.0.0/10"):
                cgnat, double_nat = 1, False
            elif addr2 in ipaddress.ip_network("192.168.0.0/16"):
                double_nat = True
            elif not addr2.is_private:
                double_nat = False
        except ValueError:
            pass
    return {"private_hops": leading_private if double_nat is not None else None,
            "hop_ips": [h for h in hops if h][:4],
            "double_nat": double_nat, "cgnat": cgnat, "error": None}


def topology_loop(conn):
    """Once at startup (fresh data after any reboot/network change), then
    daily. ~1 tiny row per run, kept forever."""
    while True:
        try:
            r = check_double_nat()
            db_execute(
                conn,
                "INSERT INTO topology_checks (ts, private_hops, hop_ips, double_nat, cgnat, error)"
                " VALUES (?,?,?,?,?,?)",
                (now_iso(), r["private_hops"], json.dumps(r["hop_ips"]),
                 None if r["double_nat"] is None else int(r["double_nat"]),
                 r["cgnat"], r["error"]),
            )
            if r["double_nat"]:
                log(f"double NAT detected: {r['private_hops']} private hops {r['hop_ips']}")
        except Exception as e:
            log_error(f"topology check failed: {e!r}")
        time.sleep(24 * 3600)


# ---------------------------------------------------------------------------
# Alerting
#
# One notifier thread polls the events table for rows the other loops wrote
# (rather than instrumenting five writer loops). Toasts go out immediately
# (they're local); webhook/email deliveries queue and retry with backoff —
# which is exactly what an internet-down alert needs, since it can't leave
# the LAN until the internet is back anyway.
# ---------------------------------------------------------------------------

ALERT_POLL_SEC = 10
ALERT_DEFAULTS = {
    "enabled": False,
    "min_duration_sec": 60,     # blips shorter than this alert nothing
    "cooldown_minutes": 5,      # per-(kind,scope,router) repeat suppression
    "quiet_hours": None,        # {"start": "23:00", "end": "07:00"} local
    "events": {"outage": True, "degraded": False, "new_device": True, "ip_change": False,
               "instability": False, "iot_outage": False},
    "channels": {
        "toast": {"enabled": True},
        "webhook": {"enabled": False, "url": "", "format": "json"},
        "email": {"enabled": False, "host": "", "port": 587, "starttls": True,
                  "username": "", "password": "", "from": "", "to": ""},
    },
}


def load_alerts_config():
    """The 'alerts' block of config.json merged over ALERT_DEFAULTS.
    Tolerant of missing/partial config — alerting simply stays off."""
    cfg = _read_json(CONFIG_PATH)
    alerts = (cfg or {}).get("alerts")
    merged = json.loads(json.dumps(ALERT_DEFAULTS))  # deep copy
    if isinstance(alerts, dict):
        for k, v in alerts.items():
            if isinstance(v, dict) and isinstance(merged.get(k), dict):
                for k2, v2 in v.items():
                    if isinstance(v2, dict) and isinstance(merged[k].get(k2), dict):
                        merged[k][k2].update(v2)
                    else:
                        merged[k][k2] = v2
            else:
                merged[k] = v
    return merged


def in_quiet_hours(cfg, dt=None):
    """True while local time is inside the configured quiet window.
    Handles ranges that cross midnight ('23:00'-'07:00')."""
    qh = cfg.get("quiet_hours")
    if not isinstance(qh, dict) or not qh.get("start") or not qh.get("end"):
        return False
    try:
        now_hm = (dt or datetime.now()).strftime("%H:%M")
        start, end = qh["start"], qh["end"]
        if start <= end:
            return start <= now_hm < end
        return now_hm >= start or now_hm < end
    except Exception:
        return False


def send_toast(title, message):
    """Desktop notification. Windows: WinRT toast via PowerShell, using
    PowerShell's own AppUserModelID (an unregistered app id gets silently
    dropped on Win11). -EncodedCommand sidesteps all quoting issues.
    macOS: osascript. Both fire-and-forget with a short timeout."""
    try:
        if IS_WINDOWS:
            from html import escape as _xml_escape
            import base64
            script = (
                "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null;"
                "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType=WindowsRuntime] | Out-Null;"
                "$x = New-Object Windows.Data.Xml.Dom.XmlDocument;"
                "$x.LoadXml('<toast><visual><binding template=\"ToastGeneric\">"
                f"<text>{_xml_escape(title)}</text><text>{_xml_escape(message)}</text>"
                "</binding></visual></toast>');"
                "$t = New-Object Windows.UI.Notifications.ToastNotification($x);"
                "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
                "'{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe').Show($t)"
            )
            encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                           capture_output=True, timeout=15, **SUBPROCESS_EXTRA)
        elif IS_MACOS:
            osa = f'display notification "{message}" with title "{title}"'.replace('\\', '')
            subprocess.run(["osascript", "-e", osa], capture_output=True, timeout=10)
    except Exception as e:
        log_error(f"toast failed: {e!r}")


def send_webhook(cfg, title, message, meta):
    """POST to a user-supplied URL. format 'json' suits Slack/Discord-style
    receivers (route it yourself), 'ntfy' sets ntfy.sh's Title header with a
    plain-text body, 'text' is just the message. Raises on failure so the
    caller's retry queue can back off."""
    import urllib.request
    url = cfg.get("url") or ""
    if not url.startswith(("http://", "https://")):
        raise ValueError("webhook url must be http(s)")
    fmt = cfg.get("format") or "json"
    headers = {"User-Agent": f"home-network-monitor/{__version__}"}
    if fmt == "json":
        body = json.dumps({"title": title, "message": message, **meta}).encode()
        headers["Content-Type"] = "application/json"
    else:
        body = message.encode()
        headers["Content-Type"] = "text/plain; charset=utf-8"
        if fmt == "ntfy":
            headers["Title"] = title.encode("ascii", "replace").decode()
            if meta.get("severity") == "down":
                headers["Priority"] = "high"
    req = urllib.request.Request(url, data=body, headers=headers)
    urllib.request.urlopen(req, timeout=10).read(200)


def email_recipients(to):
    """Normalize the email 'to' config to a comma-joined address list for
    the To header. Accepts a plain string ("a@x.com" or comma/semicolon-
    separated "a@x.com, b@y.com") or a JSON array of strings — the header
    wants commas, and smtplib's send_message() delivers to every address
    it finds there, so multi-recipient comes free once normalized."""
    if isinstance(to, list):
        parts = [str(a).strip() for a in to]
    else:
        parts = re.split(r"[,;]", str(to or ""))
    return ", ".join(a.strip() for a in parts if a.strip())


def send_email(cfg, title, message):
    """Plain SMTP notification. Credentials live in config.json — which is
    gitignored and unreachable from the LAN, but still plaintext on disk:
    the settings UI tells users to prefer an app password. Raises on
    failure for the retry queue."""
    import smtplib
    from email.message import EmailMessage
    msg = EmailMessage()
    msg["Subject"] = title
    msg["From"] = cfg.get("from") or cfg.get("username") or "netmon@localhost"
    msg["To"] = email_recipients(cfg.get("to"))
    msg.set_content(message)
    with smtplib.SMTP(cfg.get("host") or "", int(cfg.get("port") or 587), timeout=15) as s:
        if cfg.get("starttls", True):
            s.starttls()
        if cfg.get("username"):
            s.login(cfg["username"], cfg.get("password") or "")
        s.send_message(msg)


def send_test_alert():
    """Settings-UI 'Send test alert' button, via the command rail. Goes to
    every enabled channel immediately (ignores quiet hours — the user is
    actively testing)."""
    cfg = load_alerts_config()
    title = "Home Network Monitor — test alert"
    message = "Alerting works. This is a test sent from the Settings page."
    ch = cfg.get("channels", {})
    if ch.get("toast", {}).get("enabled"):
        send_toast(title, message)
    for name, sender in (("webhook", lambda c: send_webhook(c, title, message, {"kind": "test"})),
                         ("email", lambda c: send_email(c, title, message))):
        c = ch.get(name, {})
        if c.get("enabled"):
            try:
                sender(c)
                log(f"test alert sent via {name}")
            except Exception as e:
                log_error(f"test alert via {name} failed: {e!r}")


def _event_headline(row):
    """Plain-language one-liner for an event row (dict-style access)."""
    kind, scope, rn = row["kind"], row["scope"], row["router_name"]
    if kind == "outage":
        return {"gateway": "Main router is unreachable — local problem, not the ISP",
                "internet": "Internet is DOWN (router is fine — ISP side)",
                "dns": "DNS is failing — websites won't load by name",
                "target": f'"{rn or "?"}" is unreachable (your custom target — their end, not your line)',
                "iot": f'IoT device "{rn or "?"}" is down'}.get(
                    scope, f'Access point "{rn or "?"}" is down')
    if kind == "degraded":
        return "Connection is up but degraded (high latency or packet loss)"
    if kind == "instability":
        return row["note"] or "Connection is flapping — repeated brief drops"
    if kind == "new_device":
        return row["note"] or "A never-seen device joined the network"
    if kind == "ip_change":
        return row["note"] or "Public IP address changed"
    return f"{kind}/{scope}"


def _fmt_secs(secs):
    secs = max(0, int(secs))
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    return f"{secs // 3600}h {(secs % 3600) // 60}m"


def alert_loop(conn):
    """Watch the events table and notify. Seeded at MAX(id) so a restart
    never re-alerts history (close_dangling_events has already sanitized
    stale opens by the time we run). All state is in memory: worst case
    after a crash is one missed alert, never a duplicate storm."""
    try:
        rows = db_query(conn, "SELECT COALESCE(MAX(id), 0) FROM events")
        last_max_id = rows[0][0] if rows else 0
    except sqlite3.Error:
        last_max_id = 0
    tracked = {}      # event id -> {"row": dict, "open_alerted": bool}
    cooldowns = {}    # (kind, scope, router_name) -> unix ts of last OPEN alert
    queue = []        # pending webhook/email: dicts with next_try/backoff
    cfg = load_alerts_config()
    cfg_stamp = _file_stamp(CONFIG_PATH)

    def enabled_for(row):
        """Takes the event row, not just the kind: IoT-device outages have
        kind='outage' but their own opt-in key, so a dead lightbulb can't
        ride the 'Outages' checkbox meant for the internet/APs."""
        key = "iot_outage" if row["kind"] == "outage" and row["scope"] == "iot" else row["kind"]
        return cfg.get("enabled") and cfg.get("events", {}).get(key, False)

    def notify(title, message, meta, event_id=None, kind="open"):
        quiet = in_quiet_hours(cfg)
        ch = cfg.get("channels", {})
        if ch.get("toast", {}).get("enabled") and not quiet:
            send_toast(title, message)
        now = time.time()
        for name in ("webhook", "email"):
            if ch.get(name, {}).get("enabled"):
                queue.append({"channel": name, "title": title, "message": message, "meta": meta,
                              "event_id": event_id, "kind": kind, "created": now,
                              "next_try": now, "backoff": 30})

    def flush_queue():
        if in_quiet_hours(cfg):
            return  # deferred: recovery messages carry the whole story later
        now = time.time()
        ch = cfg.get("channels", {})
        for item in list(queue):
            if item["next_try"] > now:
                continue
            if now - item["created"] > 24 * 3600:
                queue.remove(item)   # too old to be useful
                continue
            try:
                if item["channel"] == "webhook":
                    send_webhook(ch.get("webhook", {}), item["title"], item["message"], item["meta"])
                else:
                    send_email(ch.get("email", {}), item["title"], item["message"])
                queue.remove(item)
            except Exception as e:
                item["backoff"] = min(item["backoff"] * 2, 300)
                item["next_try"] = now + item["backoff"]
                log_error(f"alert via {item['channel']} failed (retrying in {item['backoff']}s): {e!r}")

    while True:
        time.sleep(ALERT_POLL_SEC)
        try:
            # hot-reload the alerts config with the usual stamp trick
            new_stamp = _file_stamp(CONFIG_PATH)
            if new_stamp != cfg_stamp:
                cfg_stamp = new_stamp
                cfg = load_alerts_config()
                log("alerts config reloaded")

            rows = db_query(
                conn,
                "SELECT id, start_ts, end_ts, kind, scope, note, router_name FROM events WHERE id > ?",
                (last_max_id,))
            new_devices = []
            for r in rows:
                row = {"id": r[0], "start_ts": r[1], "end_ts": r[2], "kind": r[3],
                       "scope": r[4], "note": r[5], "router_name": r[6]}
                last_max_id = max(last_max_id, row["id"])
                if row["kind"] in ("new_device", "ip_change", "wifi_roam"):
                    if enabled_for(row):
                        if row["kind"] == "new_device":
                            new_devices.append(row)   # batched below
                        else:
                            notify("Home Network Monitor", _event_headline(row),
                                   {"kind": row["kind"], "severity": "info"}, kind="info")
                else:
                    tracked[row["id"]] = {"row": row, "open_alerted": False}
            if new_devices:
                # a scan can find several unknowns at once — one message, not five
                msg = (_event_headline(new_devices[0]) if len(new_devices) == 1
                       else f"{len(new_devices)} never-seen devices joined the network (see dashboard)")
                notify("Home Network Monitor", msg, {"kind": "new_device", "severity": "info"}, kind="info")

            # open/close transitions for duration events
            now_utc = datetime.now(timezone.utc)
            for eid, st in list(tracked.items()):
                got = db_query(conn, "SELECT end_ts FROM events WHERE id = ?", (eid,))
                end_ts = got[0][0] if got else None
                ev = st["row"]
                start = datetime.fromisoformat(ev["start_ts"])
                age = (now_utc - start).total_seconds()
                key = (ev["kind"], ev["scope"], ev["router_name"])
                if end_ts is None:
                    # still open: alert once it has outlived min_duration
                    if (not st["open_alerted"] and enabled_for(ev)
                            and age >= cfg.get("min_duration_sec", 60)
                            and time.time() - cooldowns.get(key, 0) > cfg.get("cooldown_minutes", 5) * 60):
                        st["open_alerted"] = True
                        cooldowns[key] = time.time()
                        notify("Home Network Monitor", _event_headline(ev)
                               + f" — ongoing for {_fmt_secs(age)}",
                               {"kind": ev["kind"], "scope": ev["scope"], "router": ev["router_name"],
                                "start": ev["start_ts"], "severity": "down"},
                               event_id=eid, kind="open")
                else:
                    # closed: recovery message (with the full story), and drop
                    # any still-undelivered OPEN item so a stale "DOWN!" never
                    # lands after the fact
                    dur = (datetime.fromisoformat(end_ts) - start).total_seconds()
                    for item in list(queue):
                        if item["event_id"] == eid and item["kind"] == "open":
                            queue.remove(item)
                    if st["open_alerted"] and enabled_for(ev):
                        local_s = start.astimezone().strftime("%H:%M")
                        local_e = datetime.fromisoformat(end_ts).astimezone().strftime("%H:%M")
                        notify("Home Network Monitor — recovered",
                               # "is down/failing/unreachable" -> past tense for
                               # every headline shape (first " is " only)
                               _event_headline(ev).replace(" is DOWN", " was down").replace(" is ", " was ")
                               + f" — {local_s}–{local_e}, {_fmt_secs(dur)}. Back to normal.",
                               {"kind": ev["kind"], "scope": ev["scope"], "router": ev["router_name"],
                                "start": ev["start_ts"], "end": end_ts, "severity": "recovered"},
                               event_id=eid, kind="recovery")
                    del tracked[eid]

            flush_queue()
        except Exception as e:
            log_error(f"alert loop error: {e!r}")


def retention_loop(conn):
    """Once a day, prune history older than RETENTION_DAYS from the
    high-volume tables. At 4 pings every 15s the pings table alone grows by
    ~23k rows/day (~8M rows/year) — without pruning the DB grows without
    bound and dashboard queries slowly degrade. Events and speedtests are
    small and kept forever."""
    tables = ["pings", "router_pings", "wifi", "devices", "public_ip", "dns_checks", "wifi_scan", "blips", "iot_pings"]
    while True:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)).isoformat()
        for table in tables:
            try:
                cur = db_execute(conn, f"DELETE FROM {table} WHERE ts < ?", (cutoff,))
                if cur.rowcount:
                    log(f"retention: pruned {cur.rowcount} rows older than {RETENTION_DAYS}d from {table}")
            except Exception as e:
                log_error(f"retention prune of {table} failed: {e!r}")
        time.sleep(24 * 3600)


def main():
    conn = init_db()
    close_dangling_events(conn)
    gateway = get_default_gateway()
    routers = load_routers()
    router_desc = ", ".join(f"{r['name']}={r['ip']}" for r in routers) if routers else "none configured"
    log(f"network monitor starting. gateway={gateway} routers=[{router_desc}] db={DB_PATH}")

    # When the updater installs a new version, exit(1) so Task Scheduler /
    # launchd restarts us on the new code (see update.py for why nothing
    # ever kills or re-execs a service in place).
    try:
        import update as _update
        _update.install_restart_watcher(__version__, "the monitor", log)
    except Exception:
        pass  # partially-copied install without update.py - fine

    threads = [
        threading.Thread(target=ping_loop, args=(conn, gateway), daemon=True),
        threading.Thread(target=wifi_loop, args=(conn,), daemon=True),
        threading.Thread(target=device_loop, args=(conn,), daemon=True),
        threading.Thread(target=speedtest_loop, args=(conn,), daemon=True),
        threading.Thread(target=public_ip_loop, args=(conn,), daemon=True),
        threading.Thread(target=router_loop, args=(conn, routers, gateway), daemon=True),
        threading.Thread(target=iot_loop, args=(conn, gateway), daemon=True),
        threading.Thread(target=dns_loop, args=(conn, gateway), daemon=True),
        threading.Thread(target=retention_loop, args=(conn,), daemon=True),
        threading.Thread(target=command_loop, args=(conn,), daemon=True),
        threading.Thread(target=alert_loop, args=(conn,), daemon=True),
        threading.Thread(target=topology_loop, args=(conn,), daemon=True),
    ]
    for t in threads:
        t.start()

    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        print("stopping.", flush=True)
        sys.exit(0)


if __name__ == "__main__":
    if "--version" in sys.argv:
        print(__version__)
        sys.exit(0)
    main()
