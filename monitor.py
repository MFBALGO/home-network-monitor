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
        """
    )
    # Migrations for databases created before these columns existed
    # (CREATE TABLE IF NOT EXISTS above only applies to brand-new databases).
    for migration in (
        "ALTER TABLE events ADD COLUMN router_name TEXT",
        "ALTER TABLE router_pings ADD COLUMN method TEXT",  # 'icmp' or 'tcp'
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
            if open_event_id is not None:
                db_execute(conn, "UPDATE events SET end_ts=? WHERE id=?", (ts, open_event_id))
                open_event_id = None
        else:
            consecutive_fail += 1
            if consecutive_fail >= OUTAGE_FAILURE_THRESHOLD and open_event_id is None:
                cur = db_execute(
                    conn,
                    "INSERT INTO events (start_ts, kind, scope, note) VALUES (?,?,?,?)",
                    (ts, "outage", "dns",
                     "DNS lookups failing — websites won't load by name even if pings still work"),
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
        return {"ssid": ssid, "rssi_dbm": rssi, "noise_dbm": None,
                "channel": channel, "tx_rate_mbps": tx_rate}
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
                    return {
                        "ssid": current.get("_name"),
                        "rssi_dbm": current.get("spairport_signal_noise", "").split(" / ")[0].replace(" dBm", "").strip() or None,
                        "noise_dbm": current.get("spairport_signal_noise", "").split(" / ")[-1].replace(" dBm", "").strip() or None,
                        "channel": current.get("spairport_network_channel"),
                        "tx_rate_mbps": current.get("spairport_network_rate"),
                    }
    except Exception:
        pass
    return None


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
            return {
                "download_mbps": round(data["download"]["bandwidth"] * 8 / 1_000_000, 2),
                "upload_mbps": round(data["upload"]["bandwidth"] * 8 / 1_000_000, 2),
                "ping_ms": round(data["ping"]["latency"], 1),
                "server": data.get("server", {}).get("name"),
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
    while True:
        snap = get_wifi_snapshot()
        if snap:
            db_execute(
                conn,
                "INSERT INTO wifi (ts, ssid, rssi_dbm, noise_dbm, channel, tx_rate_mbps) VALUES (?,?,?,?,?,?)",
                (now_iso(), snap.get("ssid"), snap.get("rssi_dbm"), snap.get("noise_dbm"),
                 snap.get("channel"), snap.get("tx_rate_mbps")),
            )
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
                "INSERT INTO speedtests (ts, download_mbps, upload_mbps, ping_ms, server) VALUES (?,?,?,?,?)",
                (ts, result.get("download_mbps"), result.get("upload_mbps"), result.get("ping_ms"), result.get("server")),
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


def retention_loop(conn):
    """Once a day, prune history older than RETENTION_DAYS from the
    high-volume tables. At 4 pings every 15s the pings table alone grows by
    ~23k rows/day (~8M rows/year) — without pruning the DB grows without
    bound and dashboard queries slowly degrade. Events and speedtests are
    small and kept forever."""
    tables = ["pings", "router_pings", "wifi", "devices", "public_ip", "dns_checks"]
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
