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

# Domains used to test DNS resolution (rotated one per check). Large,
# always-resolvable names — if these fail, DNS is broken for everything.
DNS_TEST_DOMAINS = ["apple.com", "google.com", "cloudflare.com"]

# Ports tried when a router doesn't answer ping — most routers serve their
# admin page on 80/443 even when they're configured to ignore ICMP.
ROUTER_TCP_PORTS = (80, 443)

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


def load_device_names():
    """Optional user-editable devices.json: a plain {"mac": "Friendly name"}
    mapping used to label devices on the dashboard and in new-device
    events. Returns {} if missing/malformed — names are nice-to-have."""
    if not os.path.exists(DEVICE_NAMES_PATH):
        return {}
    try:
        with open(DEVICE_NAMES_PATH, encoding="utf-8") as f:
            raw = json.load(f)
        return {
            _normalize_mac(str(mac).strip().lower()): str(name).strip()
            for mac, name in raw.items()
            if str(name).strip()
        }
    except Exception as e:
        log_error(f"failed to load devices.json ({e}) — friendly device names disabled")
        return {}


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


def arp_alive(ip):
    """Last-resort liveness check: a host that answers ARP is physically on
    the network even if it filters ping and has no web ports open (some
    old APs/bridges are exactly this antisocial). The preceding ping
    attempt forces an ARP resolution, so a fresh non-incomplete entry
    means the host answered. Caveat: ARP entries linger for a few minutes
    after a device actually goes down, so 'down' detection via this
    method lags by up to ~20 min — still far better than showing a
    healthy device as permanently offline."""
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


def check_router(ip):
    """Ping first; then a TCP web-port probe; then ARP liveness.
    Returns (success, latency_ms, method): 'icmp', 'tcp', or 'arp'."""
    ok, latency = ping_once(ip)
    if ok:
        return True, latency, "icmp"
    ok, latency = tcp_check(ip)
    if ok:
        return True, latency, "tcp"
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


def dns_loop(conn):
    """DNS is the classic 'internet feels broken but pings are fine'
    failure: 1.1.1.1 answers ping while name resolution is dead, so
    browsing breaks with no outage on the chart. Check resolution health
    every minute and log an event when it's down."""
    consecutive_fail = 0
    open_event_id = None
    failed_domains = []   # domains tried during the current failure streak
    i = 0
    while True:
        domain = DNS_TEST_DOMAINS[i % len(DNS_TEST_DOMAINS)]
        i += 1
        ok, latency = check_dns(domain)
        ts = now_iso()
        db_execute(
            conn,
            "INSERT INTO dns_checks (ts, domain, success, latency_ms) VALUES (?,?,?,?)",
            (ts, domain, int(ok), latency),
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
            if consecutive_fail >= OUTAGE_FAILURE_THRESHOLD and open_event_id is None:
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
        time.sleep(DNS_CHECK_INTERVAL_SEC)


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
    recent_external = []  # list of (success, latency)

    while True:
        ts = now_iso()

        gw_ok, gw_latency = (False, None)
        if gateway:
            gw_ok, gw_latency = ping_once(gateway)
            db_execute(
                conn,
                "INSERT INTO pings (ts, target, target_type, success, latency_ms) VALUES (?,?,?,?,?)",
                (ts, gateway, "gateway", int(gw_ok), gw_latency),
            )

        ext_results = []
        for target in EXTERNAL_TARGETS:
            ok, latency = ping_once(target)
            ext_results.append((ok, latency))
            db_execute(
                conn,
                "INSERT INTO pings (ts, target, target_type, success, latency_ms) VALUES (?,?,?,?,?)",
                (ts, target, "external", int(ok), latency),
            )

        any_external_ok = any(ok for ok, _ in ext_results)
        best_latency = min((l for ok, l in ext_results if ok and l is not None), default=None)

        recent_external.append((any_external_ok, best_latency))
        recent_external = recent_external[-ROLLING_WINDOW:]

        # --- Outage detection ---
        if gateway and not gw_ok:
            consecutive_gateway_fail += 1
        else:
            consecutive_gateway_fail = 0

        if not any_external_ok:
            consecutive_external_fail += 1
        else:
            consecutive_external_fail = 0

        # Gateway (local wifi/router) outage takes priority in diagnosis: if the
        # gateway itself is unreachable, that's almost certainly the cause of any
        # external failures too.
        if gateway and consecutive_gateway_fail >= OUTAGE_FAILURE_THRESHOLD:
            if open_event["gateway"] is None:
                cur = db_execute(
                    conn,
                    "INSERT INTO events (start_ts, kind, scope, note) VALUES (?,?,?,?)",
                    (ts, "outage", "gateway", "Router/gateway unreachable — local Wi-Fi or router issue"),
                )
                open_event["gateway"] = cur.lastrowid
        elif open_event["gateway"] is not None:
            db_execute(conn, "UPDATE events SET end_ts=? WHERE id=?", (ts, open_event["gateway"]))
            open_event["gateway"] = None

        if consecutive_external_fail >= OUTAGE_FAILURE_THRESHOLD:
            # Only log an "internet" outage if the gateway is fine (otherwise it's
            # a gateway outage, already captured above).
            if open_event["internet"] is None and consecutive_gateway_fail < OUTAGE_FAILURE_THRESHOLD:
                cur = db_execute(
                    conn,
                    "INSERT INTO events (start_ts, kind, scope, note) VALUES (?,?,?,?)",
                    (ts, "outage", "internet", "Gateway reachable but internet targets unreachable — likely ISP outage"),
                )
                open_event["internet"] = cur.lastrowid
        elif open_event["internet"] is not None:
            db_execute(conn, "UPDATE events SET end_ts=? WHERE id=?", (ts, open_event["internet"]))
            open_event["internet"] = None

        # --- Degradation detection (slow, not down) ---
        if len(recent_external) >= 5:
            successes = [r for r in recent_external if r[0]]
            loss_pct = 100.0 * (1 - len(successes) / len(recent_external))
            latencies = [l for ok, l in recent_external if ok and l is not None]
            avg_latency = sum(latencies) / len(latencies) if latencies else None

            is_degraded = (loss_pct > DEGRADED_LOSS_PCT) or (avg_latency and avg_latency > DEGRADED_LATENCY_MS)
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
            elif open_event.get("degraded") is not None:
                db_execute(conn, "UPDATE events SET end_ts=? WHERE id=?", (ts, open_event["degraded"]))
                open_event["degraded"] = None

        time.sleep(PING_INTERVAL_SEC)


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
    cycle = 0
    scan_logged_empty = False
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
        # hourly neighbor scan (every 12th 5-minute cycle), only while the
        # machine actually has Wi-Fi in play
        if snap and cycle % 12 == 0:
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
        cycle += 1
        time.sleep(WIFI_SNAPSHOT_INTERVAL_SEC)


def device_loop(conn):
    warned_no_ip = False

    # --- new-device detection state ---
    # Baseline = every MAC ever recorded. A MAC outside the baseline is a
    # brand-new device on the network, which gets logged as an event —
    # useful both for "whose phone is that?" and as a light security signal.
    known_macs = set()
    try:
        with _lock:
            known_macs = {row[0] for row in conn.execute(
                "SELECT DISTINCT mac FROM devices WHERE mac IS NOT NULL")}
    except Exception as e:
        log_error(f"couldn't seed known-device baseline: {e!r}")
    first_cycle = True
    last_sweep_mode = None

    while True:
        # Re-detect every cycle: launchd starts this before Wi-Fi is up at
        # boot, and the Mac can move between networks without a restart.
        own_ip, prefix = get_own_ip_and_prefix()
        if not own_ip and not warned_no_ip:
            log("warning: couldn't determine this Mac's IP/subnet — "
                "device scan will fall back to whatever's already in the ARP cache "
                "(may miss devices that don't talk to this Mac directly). "
                "Will keep retrying every cycle.")
            warned_no_ip = True

        # Populate ARP entries for every responsive device. Prefer nmap
        # when installed (checked every cycle, so installing it later
        # gets picked up without a restart); fall back to the built-in
        # ping sweep otherwise.
        nmap_bin = find_nmap_binary()
        mode = "nmap" if nmap_bin else "ping sweep"
        if mode != last_sweep_mode:
            log(f"device discovery via {mode}")
            last_sweep_mode = mode
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
        unseen = [d for d in devices if d["mac"] not in known_macs]
        if unseen:
            if first_cycle:
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
            known_macs.update(d["mac"] for d in unseen)
        first_cycle = False
        time.sleep(DEVICE_SCAN_INTERVAL_SEC)


def speedtest_loop(conn):
    while True:
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
        time.sleep(SPEEDTEST_INTERVAL_SEC)


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

        time.sleep(PUBLIC_IP_CHECK_INTERVAL_SEC)


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


def router_loop(conn, routers):
    """Ping each configured router/access point independently, and track
    outages per-router the same way the main gateway is tracked — so a dead
    node in one room shows up as "Kitchen AP down" rather than just a vague
    slowdown.

    routers.json is hot-reloaded (same idea as devices.json in device_loop):
    when the file changes — e.g. saved from the settings UI — the new list
    takes effect within one 15s cycle, no restart needed."""
    state = {r["name"]: {"consecutive_fail": 0, "open_event_id": None} for r in routers}
    stamp = _routers_file_stamp()

    while True:
        new_stamp = _routers_file_stamp()
        if new_stamp != stamp:
            stamp = new_stamp
            new_routers = load_routers()
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
            time.sleep(ROUTER_PING_INTERVAL_SEC)
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
                if st["consecutive_fail"] >= OUTAGE_FAILURE_THRESHOLD and st["open_event_id"] is None:
                    note = f"{name} ({ip}) unreachable"
                    cur = db_execute(
                        conn,
                        "INSERT INTO events (start_ts, kind, scope, note, router_name) VALUES (?,?,?,?,?)",
                        (ts, "outage", "router", note, name),
                    )
                    st["open_event_id"] = cur.lastrowid

        time.sleep(ROUTER_PING_INTERVAL_SEC)


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
        except Exception as e:
            log_error(f"command loop error: {e!r}")
            _set_test_status(state="error", error=str(e)[:200])


# ---------------------------------------------------------------------------
# Topology / double-NAT check
# ---------------------------------------------------------------------------

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
    "events": {"outage": True, "degraded": False, "new_device": True, "ip_change": False},
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
    msg["To"] = cfg.get("to") or ""
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
                "dns": "DNS is failing — websites won't load by name"}.get(
                    scope, f'Access point "{rn or "?"}" is down')
    if kind == "degraded":
        return "Connection is up but degraded (high latency or packet loss)"
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

    def enabled_for(kind):
        return cfg.get("enabled") and cfg.get("events", {}).get(kind, False)

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
                    if enabled_for(row["kind"]):
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
                    if (not st["open_alerted"] and enabled_for(ev["kind"])
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
                    if st["open_alerted"] and enabled_for(ev["kind"]):
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
    tables = ["pings", "router_pings", "wifi", "devices", "public_ip", "dns_checks", "wifi_scan"]
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

    threads = [
        threading.Thread(target=ping_loop, args=(conn, gateway), daemon=True),
        threading.Thread(target=wifi_loop, args=(conn,), daemon=True),
        threading.Thread(target=device_loop, args=(conn,), daemon=True),
        threading.Thread(target=speedtest_loop, args=(conn,), daemon=True),
        threading.Thread(target=public_ip_loop, args=(conn,), daemon=True),
        threading.Thread(target=router_loop, args=(conn, routers), daemon=True),
        threading.Thread(target=dns_loop, args=(conn,), daemon=True),
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
