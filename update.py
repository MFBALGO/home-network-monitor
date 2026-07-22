#!/usr/bin/env python3
"""Home Network Monitor - self-updater.

Downloads the latest release zip from GitHub, backs up the current code,
swaps the new files in, sanity-checks them, and rolls back automatically
if anything looks wrong. Three front doors share this one engine:

  * Settings -> General -> "Update now"   (spawns `python update.py --auto`)
  * double-clicking update-windows.bat / running `bash update.sh`
  * running `python update.py` by hand (interactive, asks before acting)

Design rules, learned the careful way:

  * The updater NEVER kills or restarts the running services. On Windows
    the updater is usually a child of serve.py's own scheduled task, so
    stopping "NetMon Web" would kill the updater mid-write; and a process
    that re-execs itself becomes an ORPHAN Task Scheduler can no longer
    see (verified empirically: the task flips to Ready while the exec'd
    process keeps running, and Stop-ScheduledTask can't kill it - a
    recipe for two monitors racing). Instead, monitor.py and serve.py run
    install_restart_watcher(): when data/update_status.json says a newer
    version landed, each process EXITS with code 1 and the service
    manager (Task Scheduler -RestartCount / launchd KeepAlive) restarts
    it on the new code within about a minute. dashboard.py is a fresh
    process every minute and needs nothing.
  * Personal files are untouchable. The release zip never contains them,
    but the updater enforces it anyway (PROTECTED below) so even a
    corrupt or malicious zip cannot clobber configs, the database, or
    logs.
  * Every replaced file goes to data/backup/v<current>/ first, and every
    new .py must compile before the update counts - a syntax error in
    any file triggers a full automatic rollback. `python update.py
    --rollback` restores the newest backup by hand.
  * data/update_status.json has exactly ONE writer (this process), like
    the other command-rail files. settings_page polls it via
    /api/update/status; the services' restart watchers read it too.

Stdlib only, like everything else here.
"""
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import zipfile
from datetime import datetime, timezone

try:
    from version import __version__
except ImportError:  # partially-copied install
    __version__ = "0.0.0"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
STATE_PATH = os.path.join(DATA_DIR, "update_check.json")    # shared cache with dashboard.py's daily check
STATUS_PATH = os.path.join(DATA_DIR, "update_status.json")  # live progress; one writer (this file)
BACKUP_ROOT = os.path.join(DATA_DIR, "backup")
BACKUPS_KEPT = 2

# Overridable so forks point at their own repo (and so the test suite can
# aim the whole pipeline at a local stub server).
API_URL = os.environ.get(
    "NETMON_UPDATE_API",
    "https://api.github.com/repos/MFBALGO/home-network-monitor/releases/latest")

DOWNLOAD_MAX_BYTES = 50 * 1024 * 1024          # release zip is ~1 MB; anything huge is wrong
UNPACKED_MAX_BYTES = 200 * 1024 * 1024         # zip-bomb ceiling
MEMBER_MAX_COUNT = 500
NOTES_MAX_CHARS = 8000

# Paths (relative to the install folder) the updater refuses to write, no
# matter what the zip contains. Everything personal or generated lives
# here; "data" and "logs" cover the DB, status files, and backups.
PROTECTED_NAMES = {"routers.json", "devices.json", "config.json",
                   "dashboard.html", "report.html", "CLAUDE.local.md",
                   "netmon-share.zip"}
PROTECTED_DIRS = ("data", "logs", ".git", ".claude")


def _parse_version(tag):
    """'v0.2.0' / '0.2.0' -> (0, 2, 0); None if it doesn't look like one.
    (Duplicated from dashboard.py on purpose - importing dashboard here
    would drag its whole template in; keep the two in sync.)"""
    m = re.fullmatch(r"v?(\d+(?:\.\d+)*)", str(tag).strip())
    if not m:
        return None
    return tuple(int(p) for p in m.group(1).split("."))


def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _save_json_atomic(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_status():
    return _load_json(STATUS_PATH, {"state": "idle"})


def _set_status(**kw):
    """Merge-write the status file. Callers always pass state=..."""
    status = read_status() if kw.get("keep") else {}
    kw.pop("keep", None)
    status.update(kw)
    status["updated_ts"] = datetime.now(timezone.utc).isoformat()
    try:
        _save_json_atomic(STATUS_PATH, status)
    except OSError:
        pass  # a status write must never sink the update itself


def _url_allowed(url):
    """https only - except loopback/private hosts, so a fork on a home
    server (or the test stub) can serve over plain http."""
    from urllib.parse import urlparse
    import ipaddress
    p = urlparse(url)
    if p.scheme == "https":
        return True
    if p.scheme != "http":
        return False
    try:
        return ipaddress.ip_address(p.hostname or "").is_private or \
            ipaddress.ip_address(p.hostname or "").is_loopback
    except ValueError:
        return p.hostname in ("localhost",)


def fetch_release_info(timeout=10):
    """Ask the releases API for the latest release. Returns
    {tag, version, html_url, zip_url, notes, published_at} or raises.
    Also refreshes data/update_check.json so the dashboard banner and the
    Settings page agree on what "latest" means."""
    import urllib.request
    req = urllib.request.Request(
        API_URL, headers={"User-Agent": f"home-network-monitor/{__version__}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        release = json.loads(resp.read().decode("utf-8", errors="ignore"))

    tag = release.get("tag_name") or ""
    zip_url = ""
    # prefer the share zip we publish with every release; tolerate a
    # renamed asset by falling back to any .zip attachment
    for asset in release.get("assets") or []:
        name = (asset.get("name") or "").lower()
        url = asset.get("browser_download_url") or ""
        if name == "netmon-share.zip" and url:
            zip_url = url
            break
        if name.endswith(".zip") and url and not zip_url:
            zip_url = url
    info = {
        "tag": tag,
        "version": ".".join(str(p) for p in (_parse_version(tag) or ())),
        "html_url": release.get("html_url") or "",
        "zip_url": zip_url,
        "notes": (release.get("body") or "")[:NOTES_MAX_CHARS],
        "published_at": release.get("published_at") or "",
    }
    state = _load_json(STATE_PATH, {})
    state.update({
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "latest_tag": tag,
        "html_url": info["html_url"],
        "zip_url": zip_url,
        "notes": info["notes"],
        "published_at": info["published_at"],
    })
    try:
        _save_json_atomic(STATE_PATH, state)
    except OSError:
        pass
    return info


def cached_info():
    """What the last successful check (ours or dashboard.py's daily one)
    knew, without touching the network. {} when never checked."""
    state = _load_json(STATE_PATH, {})
    if not state.get("latest_tag"):
        return {}
    latest = _parse_version(state.get("latest_tag") or "")
    current = _parse_version(__version__)
    return {
        "tag": state.get("latest_tag"),
        "version": ".".join(str(p) for p in (latest or ())),
        "html_url": state.get("html_url") or "",
        "zip_url": state.get("zip_url") or "",
        "notes": state.get("notes") or "",
        "published_at": state.get("published_at") or "",
        "checked_at": state.get("checked_at") or "",
        "available": bool(latest and current and latest > current),
    }


def _download_zip(url, progress):
    """Stream the release zip into data/; returns the local path."""
    import urllib.request
    if not _url_allowed(url):
        raise RuntimeError(f"refusing non-https download url: {url}")
    dest = os.path.join(DATA_DIR, "update-download.zip")
    os.makedirs(DATA_DIR, exist_ok=True)
    req = urllib.request.Request(
        url, headers={"User-Agent": f"home-network-monitor/{__version__}"})
    total = 0
    with urllib.request.urlopen(req, timeout=30) as resp, open(dest, "wb") as out:
        length = resp.headers.get("Content-Length")
        if length and int(length) > DOWNLOAD_MAX_BYTES:
            raise RuntimeError(f"release zip is implausibly large ({length} bytes)")
        while True:
            chunk = resp.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > DOWNLOAD_MAX_BYTES:
                raise RuntimeError("release zip exceeded the download size cap")
            out.write(chunk)
    progress(f"downloaded {total // 1024} KB")
    return dest


def _is_protected(rel):
    parts = rel.replace("\\", "/").split("/")
    if parts[0] in PROTECTED_DIRS:
        return True
    return parts[-1] in PROTECTED_NAMES and len(parts) == 1


def _inspect_zip(zip_path):
    """Validate the archive and plan the copy. Returns
    (zf_prefix, [relative file paths], new_version). Raises on anything
    suspicious - a failed update must fail BEFORE files move."""
    zf = zipfile.ZipFile(zip_path)
    members = [m for m in zf.infolist() if not m.is_dir()]
    if len(members) > MEMBER_MAX_COUNT:
        raise RuntimeError(f"zip has {len(members)} files - not our release zip")
    if sum(m.file_size for m in members) > UNPACKED_MAX_BYTES:
        raise RuntimeError("zip unpacks too large")

    # share.sh nests everything under one folder ("network-monitor/");
    # tolerate a flat zip too. Reject absolute paths and any '..' segment
    # (zip-slip) before trusting the names.
    names = [m.filename for m in members]
    for n in names:
        norm = n.replace("\\", "/")
        if norm.startswith("/") or re.match(r"^[a-zA-Z]:", norm) or \
                any(part == ".." for part in norm.split("/")):
            raise RuntimeError(f"zip contains an unsafe path: {n}")
    first = names[0].replace("\\", "/").split("/", 1)[0]
    prefix = first + "/" if all(
        n.replace("\\", "/").startswith(first + "/") for n in names) else ""

    rels = [n.replace("\\", "/")[len(prefix):] for n in names]
    for required in ("version.py", "monitor.py", "dashboard.py", "serve.py"):
        if required not in rels:
            raise RuntimeError(f"zip is missing {required} - not a release of this tool")

    # the version INSIDE the zip is the truth about what we'd install
    with zf.open(prefix + "version.py") as f:
        src = f.read().decode("utf-8", errors="ignore")
    m = re.search(r"__version__\s*=\s*[\"']([^\"']+)[\"']", src)
    if not m or not _parse_version(m.group(1)):
        raise RuntimeError("couldn't read the version inside the zip")
    zf.close()
    return prefix, rels, m.group(1)


def _backup_and_apply(zip_path, prefix, rels, from_version, to_version, progress):
    """Copy current files aside, then swap the new ones in (write to a
    temp name + os.replace, so a mid-copy crash never leaves a truncated
    module). Returns (backup_dir, replaced, created)."""
    backup_dir = os.path.join(BACKUP_ROOT, f"v{from_version}")
    if os.path.isdir(backup_dir):
        shutil.rmtree(backup_dir)
    replaced, created = [], []
    zf = zipfile.ZipFile(zip_path)
    try:
        # backup pass first - all of it, before anything is overwritten
        for rel in rels:
            if _is_protected(rel):
                continue
            dest = os.path.join(BASE_DIR, *rel.split("/"))
            if os.path.exists(dest):
                bpath = os.path.join(backup_dir, *rel.split("/"))
                os.makedirs(os.path.dirname(bpath), exist_ok=True)
                shutil.copy2(dest, bpath)
                replaced.append(rel)
            else:
                created.append(rel)
        manifest = {"from": from_version, "to": to_version,
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "files": replaced, "created": created}
        os.makedirs(backup_dir, exist_ok=True)
        _save_json_atomic(os.path.join(backup_dir, "manifest.json"), manifest)

        # swap pass
        for rel in rels:
            if _is_protected(rel):
                continue
            dest = os.path.join(BASE_DIR, *rel.split("/"))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            tmp = dest + ".new-update"
            with zf.open(prefix + rel) as src, open(tmp, "wb") as out:
                shutil.copyfileobj(src, out)
            os.replace(tmp, dest)
        progress(f"installed {len(replaced) + len(created)} files "
                 f"({len(created)} new)")
    finally:
        zf.close()
    return backup_dir, replaced, created


def _verify_compiles(rels):
    """Every installed .py must at least parse - a syntax error in one
    module would take a service down until someone intervenes. In-memory
    compile only (no __pycache__ side effects)."""
    for rel in rels:
        if not rel.endswith(".py") or _is_protected(rel):
            continue
        path = os.path.join(BASE_DIR, *rel.split("/"))
        with open(path, "r", encoding="utf-8") as f:
            compile(f.read(), path, "exec")


def _restore_backup(backup_dir, progress):
    manifest = _load_json(os.path.join(backup_dir, "manifest.json"), {})
    for rel in manifest.get("files", []):
        src = os.path.join(backup_dir, *rel.split("/"))
        dest = os.path.join(BASE_DIR, *rel.split("/"))
        if os.path.exists(src):
            shutil.copy2(src, dest)
    # files the update newly created have no pre-update state - remove them
    for rel in manifest.get("created", []):
        try:
            os.unlink(os.path.join(BASE_DIR, *rel.split("/")))
        except OSError:
            pass
    progress(f"restored {len(manifest.get('files', []))} files from "
             f"{os.path.basename(backup_dir)}")
    return manifest


def _prune_backups():
    try:
        dirs = [os.path.join(BACKUP_ROOT, d) for d in os.listdir(BACKUP_ROOT)
                if os.path.isdir(os.path.join(BACKUP_ROOT, d))]
        dirs.sort(key=os.path.getmtime, reverse=True)
        for d in dirs[BACKUPS_KEPT:]:
            shutil.rmtree(d, ignore_errors=True)
    except OSError:
        pass


def run_update(auto=False, progress=print):
    """The whole pipeline. Returns 'updated', 'up-to-date', 'cancelled',
    or False (failed) - only False is a CLI failure. Writes
    data/update_status.json at every step so the Settings page (and the
    services' restart watchers) can follow along."""
    running = read_status()
    if running.get("state") == "running":
        try:
            started = datetime.fromisoformat(running.get("started_ts"))
            if (datetime.now(timezone.utc) - started).total_seconds() < 600:
                progress("another update is already running - aborting")
                return False
        except (TypeError, ValueError):
            pass  # stale/garbled status never blocks an update

    started_ts = datetime.now(timezone.utc).isoformat()

    def step(name, detail=""):
        progress(name + (f": {detail}" if detail else ""))
        _set_status(state="running", step=name, detail=detail,
                    started_ts=started_ts, **{"from": __version__})

    try:
        step("checking", "asking GitHub for the latest release")
        info = fetch_release_info()
        latest = _parse_version(info["tag"])
        current = _parse_version(__version__)
        if not latest:
            raise RuntimeError(f"couldn't parse the release tag {info['tag']!r}")
        if current and latest <= current:
            progress(f"already up to date (v{__version__})")
            _set_status(state="idle", step="up-to-date", started_ts=started_ts,
                        **{"from": __version__})
            return "up-to-date"
        if not info["zip_url"]:
            raise RuntimeError("the release has no zip attached - update by "
                               "hand from " + (info["html_url"] or "GitHub"))

        if not auto:
            progress(f"installed v{__version__} -> latest v{info['version']}")
            if info["notes"]:
                head = "\n".join(info["notes"].splitlines()[:15])
                progress("what's new:\n" + head)
            answer = input("Install this update? [y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                progress("cancelled")
                _set_status(state="idle", step="cancelled", started_ts=started_ts)
                return "cancelled"

        step("downloading", f"v{info['version']} from GitHub")
        zip_path = _download_zip(info["zip_url"], progress)

        step("validating", "checking the archive")
        prefix, rels, zip_version = _inspect_zip(zip_path)
        if zip_version != info["version"]:
            raise RuntimeError(f"zip contains v{zip_version} but the release "
                               f"says v{info['version']} - refusing")

        step("backing-up", f"keeping v{__version__} in data/backup")
        backup_dir, replaced, created = _backup_and_apply(
            zip_path, prefix, rels, __version__, zip_version, progress)

        try:
            step("verifying", "compiling the new code")
            _verify_compiles(rels)
        except SyntaxError as e:
            progress(f"new code does not compile ({e}) - rolling back")
            _restore_backup(backup_dir, progress)
            # this backup captured the state we just restored - keeping it
            # would make a later manual --rollback a no-op restore of the
            # currently-running version instead of the truly previous one
            shutil.rmtree(backup_dir, ignore_errors=True)
            raise RuntimeError(f"update was rolled back: {e}")

        try:
            os.unlink(zip_path)
        except OSError:
            pass
        _prune_backups()
        # This is the line the restart watchers wait for: state done +
        # a "to" version different from the one they're running.
        _set_status(state="done", step="done", started_ts=started_ts,
                    finished_ts=datetime.now(timezone.utc).isoformat(),
                    to=zip_version, backup=os.path.basename(backup_dir),
                    **{"from": __version__})
        progress(f"updated v{__version__} -> v{zip_version}. Services restart "
                 "themselves within a minute or two; if you run the scripts "
                 "by hand, start them again yourself.")
        return "updated"
    except Exception as e:
        _set_status(state="error", error=str(e)[:400], started_ts=started_ts,
                    finished_ts=datetime.now(timezone.utc).isoformat(),
                    **{"from": __version__})
        progress(f"update failed: {e}")
        return False


def run_rollback(progress=print):
    """Restore the newest backup in data/backup and let the services
    restart onto it (the status file flip below triggers the watchers)."""
    try:
        dirs = [os.path.join(BACKUP_ROOT, d) for d in os.listdir(BACKUP_ROOT)
                if os.path.isdir(os.path.join(BACKUP_ROOT, d))]
    except OSError:
        dirs = []
    if not dirs:
        progress("no backups in data/backup - nothing to roll back to")
        return False
    newest = max(dirs, key=os.path.getmtime)
    manifest = _restore_backup(newest, progress)
    _set_status(state="done", step="rolled-back",
                finished_ts=datetime.now(timezone.utc).isoformat(),
                to=manifest.get("from") or "unknown",
                **{"from": __version__})
    progress("rolled back. Services restart themselves within a minute or two.")
    return True


def install_restart_watcher(current_version, name, log=print):
    """Daemon thread for monitor.py / serve.py: when an update lands
    (status file changes to state=done with a different version), exit
    the process with code 1 so the service manager restarts it on the
    new code. Only reacts to status CHANGES after this process started -
    a stale done-file from last week must not bounce a fresh process."""
    def _mtime():
        try:
            return os.path.getmtime(STATUS_PATH)
        except OSError:
            return None

    baseline = _mtime()

    def watch():
        nonlocal baseline
        while True:
            time.sleep(5)
            try:
                m = _mtime()
                if m is None or m == baseline:
                    continue
                baseline = m
                status = read_status()
                if status.get("state") != "done":
                    continue
                to = status.get("to")
                if to and to != current_version:
                    log(f"update installed (v{current_version} -> v{to}) - "
                        f"exiting so the service manager restarts {name} "
                        "on the new code")
                    os._exit(1)
            except Exception:
                pass  # the watcher must never kill its host by accident

    t = threading.Thread(target=watch, daemon=True, name="update-watcher")
    t.start()
    return t


def main(argv):
    if "--version" in argv:
        print(__version__)
        return 0
    if "--rollback" in argv:
        return 0 if run_rollback() else 1
    if "--check" in argv:
        try:
            info = fetch_release_info()
        except Exception as e:
            print(f"check failed: {e}")
            return 1
        print(f"installed v{__version__}, latest v{info['version'] or '?'} "
              f"({info['tag']})")
        return 0
    auto = "--auto" in argv or "--yes" in argv
    print(f"Home Network Monitor updater (installed: v{__version__})")
    return 0 if run_update(auto=auto) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
