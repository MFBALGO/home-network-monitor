#!/usr/bin/env python3
"""
One-off diagnostic: scan your LAN for devices with a web admin port open
(80/443) — routers, mesh nodes, and access points almost always have one,
since that's how you log into their settings page. Not exclusive to
routers (printers, NAS boxes, smart-home hubs, and some TVs/cameras also
run a small web server), but for each hit this grabs the page title and
HTTP Server header where possible, which is usually enough to tell a
router apart from a printer at a glance.

This is NOT part of the continuous monitor — it's a standalone tool you
run once (or whenever you add new hardware) to help you fill in
routers.json. Run it from Terminal (macOS/Linux):

    python3 scan_routers.py

or from a Windows command prompt:

    py scan_routers.py

No third-party dependencies — stdlib only, no pip installs needed.
"""

import concurrent.futures
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys

try:
    from version import __version__
except ImportError:  # partially-copied install
    __version__ = "0.0.0"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PORTS_TO_CHECK = [80, 443]
CONNECT_TIMEOUT = 0.4      # per port, seconds
HTTP_TIMEOUT = 1.5         # for the follow-up title/header fetch
MAX_WORKERS = 64


IS_MACOS = sys.platform == "darwin"
IS_WINDOWS = sys.platform == "win32"
# suppress console-window flashes when subprocesses run on Windows
SUBPROCESS_EXTRA = {"creationflags": subprocess.CREATE_NO_WINDOW} if IS_WINDOWS else {}


def get_own_ip_and_prefix():
    """Best-effort: this machine's IPv4 address and subnet prefix length."""
    ip = None
    try:
        if IS_MACOS:
            ip = subprocess.run(["ipconfig", "getifaddr", "en0"], capture_output=True, text=True, timeout=5).stdout.strip()
            if not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", ip or ""):
                ip = None
    except Exception:
        pass
    if not ip:
        # fallback: ask the OS what interface would be used to reach the internet,
        # without actually sending anything (UDP connect doesn't transmit packets)
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
        except Exception:
            pass
    if not ip:
        return None, None

    prefix = 24  # sane default for home networks
    try:
        if IS_MACOS:
            out = subprocess.run(["ifconfig", "en0"], capture_output=True, text=True, timeout=5).stdout
            m = re.search(r"netmask (0x[0-9a-fA-F]+)", out)
            if m:
                mask_int = int(m.group(1), 16)
                prefix = bin(mask_int).count("1")
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


def get_arp_table():
    """ip -> (mac, hostname) from the current ARP cache, for cross-reference."""
    table = {}
    try:
        out = subprocess.run(["arp", "-a"], capture_output=True, text=True, timeout=10, **SUBPROCESS_EXTRA).stdout
        for line in out.splitlines():
            if IS_WINDOWS:
                # "  192.168.1.1     68-7f-f0-2e-a2-00     dynamic"
                m = re.match(r"\s*(\d+\.\d+\.\d+\.\d+)\s+(([0-9a-fA-F]{2}-){5}[0-9a-fA-F]{2})\s", line)
                if m:
                    table[m.group(1)] = (m.group(2).replace("-", ":").lower(), None)
                continue
            m = re.match(r"(\S+)?\s*\(([\d.]+)\)\s+at\s+([0-9a-fA-F:]+)", line)
            if m:
                hostname, ip, mac = m.group(1), m.group(2), m.group(3)
                table[ip] = (mac, hostname if hostname != "?" else None)
    except Exception:
        pass
    return table


def check_ports(ip):
    open_ports = []
    for port in PORTS_TO_CHECK:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(CONNECT_TIMEOUT)
                if s.connect_ex((str(ip), port)) == 0:
                    open_ports.append(port)
        except Exception:
            pass
    return str(ip), open_ports


def ping_alive(ip, timeout_sec=1):
    """Is anything answering at all, regardless of open ports? Some mesh
    satellite nodes (Eero, Nest Wifi, some Deco models) run no local web
    server whatsoever — controlled entirely through a phone app talking to
    the cloud — so they'd never show up in the port scan above even when
    perfectly healthy. A ping sweep catches those too."""
    try:
        if IS_MACOS:
            cmd = ["ping", "-c", "1", "-t", str(timeout_sec), str(ip)]
        elif IS_WINDOWS:
            cmd = ["ping", "-n", "1", "-w", str(int(timeout_sec * 1000)), str(ip)]
        else:
            cmd = ["ping", "-c", "1", "-W", str(timeout_sec), str(ip)]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec + 1, **SUBPROCESS_EXTRA)
        ok = result.returncode == 0
        if ok and IS_WINDOWS and "ttl=" not in (result.stdout or "").lower():
            ok = False  # "Destination host unreachable" still exits 0 on Windows
        return str(ip), ok
    except Exception:
        return str(ip), False


def fetch_identity(ip, port):
    """Grab an HTML <title> and/or Server header — a quick fingerprint."""
    import urllib.request
    scheme = "https" if port == 443 else "http"
    url = f"{scheme}://{ip}:{port}/"
    try:
        ctx = None
        if scheme == "https":
            import ssl
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (router-scan)"})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=ctx) as resp:
            server = resp.headers.get("Server", "")
            body = resp.read(4096).decode("utf-8", errors="ignore")
            title_match = re.search(r"<title[^>]*>(.*?)</title>", body, re.IGNORECASE | re.DOTALL)
            title = re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else ""
            return title, server
    except Exception:
        return "", ""


class DiscoverError(Exception):
    """Discovery couldn't even start (no usable network interface)."""


def discover(progress=None, ip=None, prefix=None):
    """Full LAN discovery, importable — the setup wizard runs this in a
    background thread and the CLI below prints from its return value.

    progress: optional callable(phase, done, total) where phase is one of
    "port-scan" / "ping-sweep" / "identify"; exceptions in it are ignored.
    Returns {"own_ip", "network", "results": [{ip, ports, mac, hostname,
    title, server, ping_only}]} sorted web-admin hits first, then
    ping-only hosts, both in IP order. Raises DiscoverError when this
    machine's own IP can't be determined.
    """
    def report(phase, done, total):
        if progress:
            try:
                progress(phase, done, total)
            except Exception:
                pass

    if not ip:
        ip, prefix = get_own_ip_and_prefix()
    if not ip:
        raise DiscoverError("couldn't determine this machine's IP address")

    network = ipaddress.ip_network(f"{ip}/{prefix}", strict=False)
    hosts = list(network.hosts())
    arp_table = get_arp_table()

    hits = []
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for host_ip, open_ports in pool.map(check_ports, hosts):
            done += 1
            report("port-scan", done, len(hosts))
            if open_ports:
                hits.append((host_ip, open_ports))
    hits.sort(key=lambda h: tuple(int(p) for p in h[0].split(".")))

    alive_ips = set()
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for host_ip, is_alive in pool.map(ping_alive, hosts):
            done += 1
            report("ping-sweep", done, len(hosts))
            if is_alive:
                alive_ips.add(host_ip)

    results = []
    for i, (host_ip, open_ports) in enumerate(hits):
        report("identify", i + 1, len(hits))
        mac, hostname = arp_table.get(host_ip, (None, None))
        title, server = "", ""
        for port in open_ports:
            title, server = fetch_identity(host_ip, port)
            if title or server:
                break
        results.append({
            "ip": host_ip, "ports": open_ports, "mac": mac, "hostname": hostname,
            "title": title, "server": server, "ping_only": False,
        })

    hit_ips = {h[0] for h in hits}
    other_alive = sorted(alive_ips - hit_ips, key=lambda a: tuple(int(p) for p in a.split(".")))
    for host_ip in other_alive:
        mac, hostname = arp_table.get(host_ip, (None, None))
        results.append({"ip": host_ip, "ports": [], "mac": mac, "hostname": hostname,
                        "title": None, "server": None, "ping_only": True})

    return {"own_ip": ip, "network": str(network), "results": results}


def main():
    ip, prefix = get_own_ip_and_prefix()
    if not ip:
        print("Couldn't determine this machine's IP address. Are you connected to Wi-Fi/Ethernet?")
        sys.exit(1)

    network = ipaddress.ip_network(f"{ip}/{prefix}", strict=False)
    print(f"Scanning {network} ({len(list(network.hosts()))} addresses)...")
    print("This takes maybe 30-60 seconds depending on your network.\n")

    # Announce each pass once, when discover() first reports it.
    announced = set()
    def console_progress(phase, done, total):
        if phase in announced:
            return
        announced.add(phase)
        if phase == "port-scan":
            print(f"Pass 1/2: checking for open web admin ports {PORTS_TO_CHECK}...")
        elif phase == "ping-sweep":
            print("Pass 2/2: pinging every address to find devices with no web admin at all "
                  "(common for mesh satellite nodes controlled purely through a phone app)...")

    scan = discover(progress=console_progress, ip=ip, prefix=prefix)
    results = scan["results"]
    web_hits = [r for r in results if not r["ping_only"]]
    ping_only = [r for r in results if r["ping_only"]]
    print()

    if not results:
        print("No devices responded at all. Double check you're connected to your home "
              "network (not a VPN or a guest network on a different subnet).")
        return

    if web_hits:
        print(f"{'IP':<16} {'Ports':<10} {'MAC':<19} {'Hostname':<22} Title / Server")
        print("-" * 100)
        for r in web_hits:
            identity = r["title"] or r["server"] or ""
            print(f"{r['ip']:<16} {','.join(map(str, r['ports'])):<10} {r['mac'] or '':<19} {(r['hostname'] or '')[:22]:<22} {identity}")
    else:
        print("No devices found with a web admin port open.")

    # Live hosts the port scan missed entirely — likely candidates for
    # mesh nodes / devices with no local web UI.
    if ping_only:
        print(f"\nOther devices that responded to a ping but have no web admin port open ({len(ping_only)}):")
        print(f"{'IP':<16} {'MAC':<19} Hostname")
        print("-" * 60)
        for r in ping_only:
            print(f"{r['ip']:<16} {r['mac'] or '':<19} {r['hostname'] or ''}")
        print(
            "\nIf a mesh node is missing from the table above, it's probably in this "
            "list — many mesh satellite nodes have no local web UI at all (Eero, Nest "
            "Wifi, and some Deco models manage everything through the phone app "
            "instead). You can still add it to routers.json by IP for ping-based "
            "up/down monitoring — you just won't get a browser admin page at that "
            "address. Match it to a location by hostname/MAC, or by checking the "
            "mesh app for that node's IP."
        )

    out_path = os.path.join(BASE_DIR, "scan_results.json")
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nFull results saved to {out_path}")
    if web_hits:
        print(
            "\nTo add one of these to routers.json, copy its IP and give it a name, e.g.:\n"
            '  { "name": "Living Room AP", "ip": "' + web_hits[0]["ip"] + '" }'
        )
    print(
        "\nNote: the port-scan table finds ANY device with a web admin port open, not "
        "just routers — printers, NAS boxes, smart-home hubs, and some TVs/cameras show "
        "up too. The title/Server column is your best clue for telling them apart; open "
        "http://<ip>/ in a browser if you're still not sure."
    )


if __name__ == "__main__":
    if "--version" in sys.argv:
        print(__version__)
        sys.exit(0)
    main()
