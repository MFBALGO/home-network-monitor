#!/usr/bin/env python3
"""
Home Network Monitor - settings/wizard backend logic.

HTTP-agnostic on purpose: serve.py maps localhost-only /api/* requests to
handle_get()/handle_post() below, which take and return plain dicts, so
everything here can be tested standalone (python -c ...) without a server.

What lives here:
  - atomic, validated writes of the three user config files
    (config.json / routers.json / devices.json). The running monitor and
    dashboard pick changes up on their own (routers <=15s, devices <=5min,
    config on the next 60s dashboard regen) - no service restarts.
  - the LAN discovery job for the setup wizard: scan_routers.discover()
    run in a background thread, with a pollable status dict.

Stdlib only, like everything else in this toolkit.
"""

import ipaddress
import json
import os
import re
import threading
import time
from datetime import datetime, timezone

import scan_routers

try:
    from version import __version__
except ImportError:  # partially-copied install
    __version__ = "0.0.0"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
ROUTERS_CONFIG_PATH = os.path.join(BASE_DIR, "routers.json")
DEVICE_NAMES_PATH = os.path.join(BASE_DIR, "devices.json")

MAC_RE = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")
# "192.168.100." style prefixes for hide_ip_prefixes: 1-4 dotted number
# groups, optionally ending with a dot.
IP_PREFIX_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){0,3}\.?$")

# Wizard heuristics: which discovered devices get a pre-checked "monitor
# this" box. A web admin port plus a router-ish fingerprint suggests yes;
# an obviously-not-a-router fingerprint always wins and suggests no.
ROUTER_HINT_RE = re.compile(
    r"router|openwrt|tp-?link|archer|linksys|buffalo|asus|netgear|eero|deco|"
    r"orbi|ubiquiti|unifi|mikrotik|fritz|zyxel|d-?link|tenda|access.?point|"
    r"\bap\b|wireless|gateway", re.IGNORECASE)
NON_ROUTER_HINT_RE = re.compile(
    r"printer|epson|canon|brother|laserjet|officejet|nas\b|synology|qnap|"
    r"camera|ipcam|rtsp|hikvision|dahua|chromecast|roku|apple.?tv|sonos|"
    r"philips.?hue|plex|kodi", re.IGNORECASE)


def load_json(path, default):
    """Same tolerant loader the monitor and dashboard use."""
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json_atomic(path, obj):
    """Write via tmp + os.replace so the monitor/dashboard (which re-read
    these files on their own schedules) only ever see the old or the new
    file, never a half-written one. A brief retry absorbs Windows sharing
    violations when a reader has the file open at the wrong moment."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    for attempt in range(3):
        try:
            os.replace(tmp, path)
            return
        except OSError:
            if attempt == 2:
                raise
            time.sleep(0.1)


def normalize_mac(mac):
    """'AA-BB-CC-DD-EE-FF' -> 'aa:bb:cc:dd:ee:ff'; None if hopeless."""
    m = str(mac).strip().lower().replace("-", ":")
    return m if MAC_RE.fullmatch(m) else None


# ---------------------------------------------------------------------------
# Validators - each returns (errors, warnings); an entry is
# {"file": ..., "path": "floors[2]", "msg": ...}. Errors block the save,
# warnings don't.
# ---------------------------------------------------------------------------

def _err(file, path, msg):
    return {"file": file, "path": path, "msg": msg}


def validate_config(cfg):
    errors, warnings = [], []
    if not isinstance(cfg, dict):
        return [_err("config", "", "must be a JSON object")], []

    title = cfg.get("title")
    if title is not None and (not isinstance(title, str) or not title.strip() or len(title) > 100):
        errors.append(_err("config", "title", "must be non-empty text, at most 100 characters"))

    floors = cfg.get("floors")
    if floors is not None:
        if (not isinstance(floors, list) or not (1 <= len(floors) <= 12)
                or any(not isinstance(f, str) or not f.strip() for f in floors)):
            errors.append(_err("config", "floors", "must be a list of 1-12 non-empty floor names"))
        elif len(set(floors)) != len(floors):
            errors.append(_err("config", "floors", "floor names must be unique"))
    floor_set = set(floors) if isinstance(floors, list) else set()

    under = cfg.get("underground_floors")
    if under is not None:
        if not isinstance(under, list) or any(not isinstance(f, str) for f in under):
            errors.append(_err("config", "underground_floors", "must be a list of floor names"))
        else:
            for f in under:
                if floor_set and f not in floor_set:
                    errors.append(_err("config", "underground_floors", f"'{f}' is not in floors"))

    main_floor = cfg.get("main_router_floor")
    if main_floor is not None and floor_set and main_floor not in floor_set:
        errors.append(_err("config", "main_router_floor", f"'{main_floor}' is not in floors"))

    prefixes = cfg.get("hide_ip_prefixes")
    if prefixes is not None:
        if not isinstance(prefixes, list):
            errors.append(_err("config", "hide_ip_prefixes", "must be a list of IP prefixes"))
        else:
            for i, p in enumerate(prefixes):
                if not isinstance(p, str) or not IP_PREFIX_RE.fullmatch(p):
                    errors.append(_err("config", f"hide_ip_prefixes[{i}]",
                                       'must look like "192.168.100." (a dotted IP prefix)'))

    thresholds = cfg.get("thresholds")
    if thresholds is not None:
        if not isinstance(thresholds, dict):
            errors.append(_err("config", "thresholds", "must be an object of {metric: {good, fair}}"))
        else:
            for metric, th in thresholds.items():
                if (not isinstance(th, dict)
                        or any(k not in ("good", "fair") for k in th)
                        or any(not isinstance(v, (int, float)) for v in th.values())):
                    errors.append(_err("config", f"thresholds.{metric}",
                                       'must be {"good": number, "fair": number}'))

    for key in ("plan_down_mbps", "plan_up_mbps"):
        v = cfg.get(key)
        if v is not None and (not isinstance(v, (int, float)) or v <= 0):
            errors.append(_err("config", key, "must be a positive number (or removed)"))

    if cfg.get("update_check") is not None and not isinstance(cfg.get("update_check"), bool):
        errors.append(_err("config", "update_check", "must be true or false"))

    alerts = cfg.get("alerts")
    if alerts is not None:
        if not isinstance(alerts, dict):
            errors.append(_err("config", "alerts", "must be an object"))
        else:
            if alerts.get("enabled") is not None and not isinstance(alerts["enabled"], bool):
                errors.append(_err("config", "alerts.enabled", "must be true or false"))
            for key, lo, hi in (("min_duration_sec", 0, 3600), ("cooldown_minutes", 0, 1440)):
                v = alerts.get(key)
                if v is not None and (not isinstance(v, (int, float)) or not lo <= v <= hi):
                    errors.append(_err("config", f"alerts.{key}", f"must be a number between {lo} and {hi}"))
            qh = alerts.get("quiet_hours")
            if qh is not None and qh != {}:
                hm = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
                if (not isinstance(qh, dict)
                        or not isinstance(qh.get("start"), str) or not hm.fullmatch(qh["start"])
                        or not isinstance(qh.get("end"), str) or not hm.fullmatch(qh["end"])):
                    errors.append(_err("config", "alerts.quiet_hours",
                                       'must be {"start": "HH:MM", "end": "HH:MM"} (or removed)'))
            evs = alerts.get("events")
            if evs is not None:
                if not isinstance(evs, dict):
                    errors.append(_err("config", "alerts.events", "must be an object of {event: true/false}"))
                else:
                    for k, v in evs.items():
                        if k not in ("outage", "degraded", "new_device", "ip_change"):
                            warnings.append(_err("config", f"alerts.events.{k}", "unknown event type - kept as-is"))
                        elif not isinstance(v, bool):
                            errors.append(_err("config", f"alerts.events.{k}", "must be true or false"))
            ch = alerts.get("channels")
            if ch is not None:
                if not isinstance(ch, dict):
                    errors.append(_err("config", "alerts.channels", "must be an object"))
                else:
                    wh = ch.get("webhook")
                    if isinstance(wh, dict):
                        if wh.get("enabled") and not str(wh.get("url", "")).startswith(("http://", "https://")):
                            errors.append(_err("config", "alerts.channels.webhook.url", "must start with http:// or https://"))
                        if wh.get("format") not in (None, "", "json", "ntfy", "text"):
                            errors.append(_err("config", "alerts.channels.webhook.format", 'must be "json", "ntfy" or "text"'))
                    em = ch.get("email")
                    if isinstance(em, dict) and em.get("enabled"):
                        if not em.get("host"):
                            errors.append(_err("config", "alerts.channels.email.host", "required when email is enabled"))
                        port = em.get("port")
                        if port is not None and (not isinstance(port, int) or not 1 <= port <= 65535):
                            errors.append(_err("config", "alerts.channels.email.port", "must be a port number (1-65535)"))
                        if not em.get("to"):
                            errors.append(_err("config", "alerts.channels.email.to", "required when email is enabled"))

    known = {"title", "floors", "underground_floors", "main_router_floor",
             "hide_ip_prefixes", "thresholds", "plan_down_mbps", "plan_up_mbps",
             "update_check", "alerts"}
    for key in cfg:
        if key not in known:
            warnings.append(_err("config", key, "unknown setting - kept as-is"))
    return errors, warnings


def validate_routers(routers, floors=None):
    """floors: the floor list to check against (from the same POST when
    config is being saved too, else the on-disk config)."""
    errors, warnings = [], []
    if not isinstance(routers, list):
        return [_err("routers", "", "must be a JSON list")], []
    if len(routers) > 50:
        return [_err("routers", "", "at most 50 routers")], []

    if floors is None:
        floors = load_json(CONFIG_PATH, {}).get("floors")
    floor_set = set(floors) if isinstance(floors, list) else set()

    seen_names, seen_ips = set(), set()
    for i, r in enumerate(routers):
        if not isinstance(r, dict):
            errors.append(_err("routers", f"[{i}]", "each entry must be an object"))
            continue
        name = r.get("name")
        if not isinstance(name, str) or not name.strip() or len(name) > 80:
            errors.append(_err("routers", f"[{i}].name", "must be non-empty text, at most 80 characters"))
        elif name.strip() in seen_names:
            # the monitor keys per-router state and events off the name
            errors.append(_err("routers", f"[{i}].name", f"duplicate name '{name.strip()}'"))
        else:
            seen_names.add(name.strip())

        ip = r.get("ip")
        try:
            parsed = ipaddress.ip_address(str(ip))
            if parsed.version != 4:
                raise ValueError
            if str(parsed) in seen_ips:
                warnings.append(_err("routers", f"[{i}].ip", f"duplicate IP {parsed} - monitored twice"))
            seen_ips.add(str(parsed))
        except ValueError:
            errors.append(_err("routers", f"[{i}].ip", "not a valid IPv4 address"))

        floor = r.get("floor")
        if floor is not None:
            if not isinstance(floor, str):
                errors.append(_err("routers", f"[{i}].floor", "must be a floor name"))
            elif floor_set and floor not in floor_set:
                warnings.append(_err("routers", f"[{i}].floor",
                                     f"'{floor}' is not in the floors list - shown unplaced on the house map"))

        for key in r:
            if key not in ("name", "ip", "floor"):
                warnings.append(_err("routers", f"[{i}].{key}", "unknown field - kept as-is"))
    return errors, warnings


def validate_devices(devices):
    errors, warnings = [], []
    if not isinstance(devices, dict):
        return [_err("devices", "", "must be a JSON object of {mac: name}")], []
    for mac, name in devices.items():
        if normalize_mac(mac) is None:
            errors.append(_err("devices", mac, "not a valid MAC address (aa:bb:cc:dd:ee:ff)"))
        if not isinstance(name, str) or not name.strip() or len(name) > 80:
            errors.append(_err("devices", mac, "name must be non-empty text, at most 80 characters"))
    return errors, warnings


def normalize_devices(devices):
    """Canonicalize MAC keys (dashes -> colons, lowercase)."""
    return {normalize_mac(mac): name.strip() for mac, name in devices.items()}


# ---------------------------------------------------------------------------
# Discovery job (one at a time, polled by the wizard/settings pages)
# ---------------------------------------------------------------------------

_job_lock = threading.Lock()
_job = {"state": "idle"}  # idle | running | done | error


def _gateway_ip():
    try:
        import monitor
        return monitor.get_default_gateway()
    except Exception:
        return None


def _decorate(results, gateway_ip):
    """Add the wizard-facing fields to raw discover() results."""
    routers_by_ip = {}
    for r in load_json(ROUTERS_CONFIG_PATH, []):
        if isinstance(r, dict) and r.get("ip"):
            routers_by_ip[str(r["ip"])] = r.get("name")
    out = []
    for r in results:
        fingerprint = " ".join(filter(None, (r.get("title"), r.get("server"), r.get("hostname"))))
        is_gateway = bool(gateway_ip) and r["ip"] == gateway_ip
        suggested = (bool(r.get("ports"))
                     and not is_gateway
                     and not NON_ROUTER_HINT_RE.search(fingerprint)
                     and bool(ROUTER_HINT_RE.search(fingerprint)))
        out.append({**r, "is_gateway": is_gateway, "suggested": suggested,
                    "known_router_name": routers_by_ip.get(r["ip"])})
    return out


def start_discovery():
    """Kick off a scan in a daemon thread. Returns (status, payload)."""
    with _job_lock:
        if _job.get("state") == "running":
            return 409, {"ok": False, "error": "a scan is already running"}
        _job.clear()
        _job.update({"state": "running", "phase": "starting", "done": 0, "total": 0})

    def progress(phase, done, total):
        with _job_lock:
            if _job.get("state") == "running":
                _job.update({"phase": phase, "done": done, "total": total})

    def run():
        try:
            scan = scan_routers.discover(progress=progress)
            gateway = _gateway_ip()
            with _job_lock:
                _job.clear()
                _job.update({"state": "done", "network": scan["network"],
                             "own_ip": scan["own_ip"],
                             "results": _decorate(scan["results"], gateway)})
        except Exception as e:
            with _job_lock:
                _job.clear()
                _job.update({"state": "error", "error": str(e) or type(e).__name__})

    threading.Thread(target=run, daemon=True, name="wizard-discovery").start()
    return 202, {"ok": True}


def discovery_status():
    with _job_lock:
        return 200, dict(_job)


# ---------------------------------------------------------------------------
# HTTP-shaped entry points (serve.py calls these)
# ---------------------------------------------------------------------------

def wizard_needed():
    """A brand-new install: neither routers.json nor config.json exists."""
    return not os.path.exists(ROUTERS_CONFIG_PATH) and not os.path.exists(CONFIG_PATH)


def _file_meta(path):
    if not os.path.exists(path):
        return {"exists": False}
    st = os.stat(path)
    return {"exists": True, "mtime": st.st_mtime, "size": st.st_size}


# ---------------------------------------------------------------------------
# "Test now" command rail (writes data/commands.json for monitor.py; reads
# back data/test_status.json that only the monitor writes — one writer per
# file, so no locking is needed across the two processes)
# ---------------------------------------------------------------------------

COMMANDS_PATH = os.path.join(BASE_DIR, "data", "commands.json")
TEST_STATUS_PATH = os.path.join(BASE_DIR, "data", "test_status.json")
TEST_COOLDOWN_SEC = 60

_test_lock = threading.Lock()
_last_test_started = 0.0


def start_test(body):
    """Queue an on-demand connectivity test for the monitor to pick up
    (its command poller runs every 2s). 409 while one runs, 429 inside the
    cooldown — double-clicks and enthusiastic phone-tapping are expected."""
    global _last_test_started
    want_speed = bool(isinstance(body, dict) and body.get("speedtest"))
    with _test_lock:
        status = load_json(TEST_STATUS_PATH, {})
        if status.get("state") == "running":
            # trust a running status only if it's fresh — a monitor killed
            # mid-test would otherwise wedge the button forever
            try:
                started = datetime.fromisoformat(status.get("started_ts"))
                fresh = (datetime.now(timezone.utc) - started).total_seconds() < 300
            except (TypeError, ValueError):
                fresh = False
            if fresh:
                return 409, {"ok": False, "error": "a test is already running"}
        if time.time() - _last_test_started < TEST_COOLDOWN_SEC:
            return 429, {"ok": False, "error": "please wait a minute between tests"}
        _last_test_started = time.time()
        cmd = {"id": f"{int(time.time() * 1000)}-{os.getpid()}", "action": "test_now",
               "speedtest": want_speed, "issued_ts": datetime.now(timezone.utc).isoformat()}
        try:
            save_json_atomic(COMMANDS_PATH, cmd)
        except OSError as e:
            return 500, {"ok": False, "error": f"couldn't write command: {e}"}
    return 202, {"ok": True}


def send_test_alert_command():
    """Queue a test-alert command (Settings page button)."""
    cmd = {"id": f"{int(time.time() * 1000)}-{os.getpid()}", "action": "test_alert",
           "issued_ts": datetime.now(timezone.utc).isoformat()}
    try:
        save_json_atomic(COMMANDS_PATH, cmd)
    except OSError as e:
        return 500, {"ok": False, "error": f"couldn't write command: {e}"}
    return 202, {"ok": True}


def handle_get(path):
    if path == "/api/test/status":
        return 200, load_json(TEST_STATUS_PATH, {"state": "idle"})
    if path == "/api/config":
        return 200, {
            "config": load_json(CONFIG_PATH, {}),
            "routers": load_json(ROUTERS_CONFIG_PATH, []),
            "devices": load_json(DEVICE_NAMES_PATH, {}),
            "meta": {
                "version": __version__,
                "wizard_needed": wizard_needed(),
                "files": {
                    "config": _file_meta(CONFIG_PATH),
                    "routers": _file_meta(ROUTERS_CONFIG_PATH),
                    "devices": _file_meta(DEVICE_NAMES_PATH),
                },
            },
        }
    if path == "/api/discover/status":
        return discovery_status()
    return 404, {"ok": False, "error": "unknown endpoint"}


def handle_post(path, body):
    if path == "/api/discover":
        return start_discovery()
    if path == "/api/config":
        return _save_config(body)
    if path == "/api/test/run":
        return start_test(body)
    if path == "/api/alerts/test":
        return send_test_alert_command()
    return 404, {"ok": False, "error": "unknown endpoint"}


def _save_config(body):
    if not isinstance(body, dict):
        return 400, {"ok": False, "errors": [_err("request", "", "body must be a JSON object")]}

    present = [k for k in ("config", "routers", "devices") if k in body]
    if not present:
        return 400, {"ok": False, "errors": [_err("request", "", "nothing to save")]}

    # The wizard posts all three at once. If this install already has a
    # router list, that's almost certainly a re-run wizard about to clobber
    # a working setup - require an explicit overwrite.
    if (set(present) == {"config", "routers", "devices"}
            and os.path.exists(ROUTERS_CONFIG_PATH)
            and body.get("overwrite") is not True):
        return 409, {"ok": False, "error": "already configured",
                     "hint": "this install already has a routers.json - "
                             "pass overwrite:true to replace it, or use Settings"}

    errors, warnings = [], []
    if "config" in body:
        e, w = validate_config(body["config"])
        errors += e; warnings += w
    if "routers" in body:
        floors = None
        if "config" in body and isinstance(body["config"], dict):
            floors = body["config"].get("floors")
        e, w = validate_routers(body["routers"], floors=floors)
        errors += e; warnings += w
    if "devices" in body:
        e, w = validate_devices(body["devices"])
        errors += e; warnings += w
    if errors:
        return 400, {"ok": False, "errors": errors, "warnings": warnings}

    # All valid - write only what was sent, each file atomically.
    saved = []
    if "config" in body:
        save_json_atomic(CONFIG_PATH, body["config"])
        saved.append("config")
    if "routers" in body:
        save_json_atomic(ROUTERS_CONFIG_PATH, body["routers"])
        saved.append("routers")
    if "devices" in body:
        save_json_atomic(DEVICE_NAMES_PATH, normalize_devices(body["devices"]))
        saved.append("devices")
    return 200, {"ok": True, "saved": saved, "warnings": warnings}
