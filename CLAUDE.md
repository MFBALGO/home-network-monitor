# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# Home Network Monitor

A home network monitoring toolkit answering "was the internet actually
down or does it just feel that way" with data. A collector (`monitor.py`)
runs continuously as a background service, logs to SQLite
(`data/network_monitor.db`, WAL mode), and `dashboard.py` regenerates a
self-contained `dashboard.html` every minute. `serve.py` publishes that
page to the whole LAN on port 8080. Pure-stdlib Python 3; no build step,
no package manager. Cross-platform: Windows (Task Scheduler via
`setup.ps1`), macOS (launchd via `setup.sh`), Linux (manual/systemd — see
README). `README.md` has full user-facing docs. `bash share.sh` builds a
clean shareable zip with example configs and no personal data.

If a `CLAUDE.local.md` exists next to this file, it holds the
site-specific install state (network topology, router inventory, open
items) — read it too.

## Common commands

Windows (live install runs as three Task Scheduler jobs):

```powershell
# Health check
Get-ScheduledTask -TaskName 'NetMon*'

# Restart the collector after editing monitor.py
Stop-ScheduledTask -TaskName 'NetMon Monitor'; Start-ScheduledTask -TaskName 'NetMon Monitor'

# dashboard.py changes apply on the next 60s regen automatically; to force:
python dashboard.py

# Restart the web server after editing serve.py
Stop-ScheduledTask -TaskName 'NetMon Web'; Start-ScheduledTask -TaskName 'NetMon Web'

# Run pieces manually in the foreground (for debugging)
python monitor.py       # stop the scheduled task first so two don't race
python serve.py         # serves on :8080 (NETMON_WEB_PORT to change)
python scan_routers.py  # standalone LAN scanner to find router IPs

# (Re)install / remove everything
.\setup-windows.bat
.\uninstall-windows.bat
```

macOS: `bash setup.sh` registers launchd jobs `com.<user>.netmon.monitor`
/ `.dashboard` / `.web`; restart the monitor with
`launchctl kickstart -k gui/$(id -u)/com.$(id -un).netmon.monitor`.

Scheduled tasks on Windows (registered by `setup.ps1`):
- **NetMon Monitor** — `pythonw monitor.py`, at-logon, auto-restart on
  crash. Registered elevated when possible (nmap's Npcap driver otherwise
  fires UAC prompts on every 5-min device sweep).
- **NetMon Dashboard** — `pythonw dashboard.py` every 1 min.
- **NetMon Web** — `pythonw serve.py`, at-logon, auto-restart; firewall
  rule "NetMon Dashboard (TCP 8080)" allows inbound 8080, Private profile.

Under `pythonw` there is no console: stdout/stderr go to `logs/`
(`monitor.err.log`, `web.err.log`, `dashboard.out.log`). Check these
first when something misbehaves.

Debugging the DB: sqlite3 from Python works, but PowerShell mangles
inline SQL parentheses in `python -c` — write a temp .py script instead.
Always set `pragma busy_timeout` (the collector writes every few seconds).

## Architecture / code map

- `monitor.py` — always-on collector, 8 daemon threads: ping (gateway +
  1.1.1.1/8.8.8.8/9.9.9.9 every 15s), wifi (5m), device scan (5m),
  speedtest (30m, Ookla CLI), public IP (10m), router checks (15s per
  router), DNS health (60s), retention (daily, prunes >90d).
  - Cross-platform via IS_MACOS / IS_WINDOWS branches: Windows uses
    `ping -n` (+ "TTL=" guard because Windows ping exits 0 even for
    "unreachable"; locale-tolerant ms regex), `arp -a` (dash-MAC table),
    `route print`/`Get-NetRoute` gateway, `Get-NetIPAddress` prefix,
    `netsh wlan` Wi-Fi (% → approx dBm). Every subprocess gets
    CREATE_NO_WINDOW on Windows.
  - Router liveness 3-tier: ping → TCP 80/443 → ARP (~20min down-lag).
    `method` column in `router_pings` records icmp/tcp/arp; dashboard
    shows "Online (web)" for tcp, "Online (silent)" for arp.
  - Device scan: nmap `-sn` sweep when installed (checked every cycle),
    else built-in ping sweep, then `arp -an`/`arp -a` (NOT plain `arp -a`
    on mac — reverse-DNS blew the timeout: the old "0 devices" bug), then
    bounded reverse-DNS.
  - New-device events (baseline = all MACs ever; first scan absorbs
    unknowns silently). SQLite WAL + busy_timeout.
- `dashboard.py` — reads DB, writes `dashboard.html`. Dark-first "Mission
  Control" theme (light theme kept; theme + 60s auto-refresh persist via
  localStorage). One giant triple-quoted HTML template in `build_html()`,
  data injected as inline JSON via `.replace()` placeholders; all
  rendering is client-side JS from `const DATA`. Feature set: stat cards
  with GOOD/FAIR/HIGH rating badges (JS `THRESHOLDS` defaults,
  overridable via config.json `thresholds`); an architectural SVG house
  map — section-drawing style (flat roof slab, street-level datum line
  with elevation marker, hatched earth below), an "Internet" status node
  buried as a fiber line that rises into the main router (live latency +
  last speed test, red OFFLINE when down), compact router pills with
  hover/tap detail cards, Wi-Fi coverage bubbles (pulsing red hole when
  an AP is down), windows lit per-floor while that floor's APs are up,
  animated packet links; outages log with SVG 7-day incident timeline +
  filters; Chart.js charts (vendored) with threshold reference lines, a
  synced 24h/7d toggle, and the speed chart pinned to 0..plan+100 so the
  plan lines stay visible; devices table with friendly names from
  devices.json, `hide_ip_prefixes` drops matching devices. Chart colors
  are baked at build time → charts fully re-render on theme change. Page
  scrollbars are hidden globally but scrolling still works — beware
  `overflow-x: clip` (it coerces overflow-y to clip and kills page
  scrolling; use `hidden`).
- `serve.py` — stdlib LAN web server. LAN sees ONLY dashboard.html + the
  two vendor JS files (whitelist ROUTES dict; DB/logs/config
  unreachable). Localhost additionally gets `/setup` (first-run wizard),
  `/settings`, and the `/api/*` config endpoints — POSTs are guarded
  against CSRF/DNS-rebinding (Host/Origin/Content-Type checks, 256KB
  body cap). `/` 302s to the wizard on a fresh install and serves a
  "warming up" page until dashboard.html first exists. Port 8080
  (`NETMON_WEB_PORT` to override), no-store on HTML, 503 + Retry-After
  if the file is mid-rewrite.
- `settings_api.py` — HTTP-agnostic backend for the wizard/settings:
  tolerant loads, atomic saves (tmp+fsync+os.replace with retry),
  per-file validators returning (errors, warnings), and the background
  discovery job (threading.Lock-guarded status dict, decorates results
  with is_gateway/suggested/known_router_name).
- `settings_page.py` — `WIZARD_HTML` (auto-scan → floors → routers →
  review, 409-guarded overwrite) and `SETTINGS_HTML` (General/Routers/
  Devices tabs) as Python strings served from memory, styled to match
  the dashboard.
- `scan_routers.py` — standalone LAN scanner to find router IPs (TCP
  80/443 sweep + ping sweep + ARP + HTTP title fingerprint). Core logic
  is the importable `discover(progress=None)`; the CLI prints from its
  return value.
- `version.py` — single source of truth for the version; every script
  imports it with a fallback and supports `--version`. dashboard.py runs
  a daily fail-silent GitHub releases check (cached in
  `data/update_check.json`, opt-out `"update_check": false`).
- `diagnose.py` — builds `netmon-diagnostics-*.zip` (report + logs tail
  + configs) for remote troubleshooting; `build_bundle()` is importable.
- SQLite tables: `pings`, `router_pings`, `devices`, `dns_checks`,
  `events`, `public_ip`, `speedtests`, `wifi`.
- Config files (all optional, user-editable JSON in this folder; the
  committed `.example.json` files show the format; normally written by
  the wizard/settings UI, hot-reloaded — routers ≤15s, devices ≤5min,
  config on next dashboard regen, no restarts):
  - `routers.json` — [{name, ip, floor}], order = file order.
  - `devices.json` — {mac: friendly name}.
  - `config.json` — {title, floors[], underground_floors[],
    main_router_floor} + optional `hide_ip_prefixes`, `thresholds`,
    `plan_down_mbps`/`plan_up_mbps`.
- `setup.sh` / `setup.ps1` install services, vendor Chart.js (committed
  in `vendor/`, re-downloaded via CDN fallback chain if missing), Ookla
  speedtest CLI (NOT homebrew/pip speedtest-cli), optional nmap.
  `share.sh` → clean generic zip (no personal config).

## Working notes

- Nothing person-specific may be hardcoded in the Python — house
  specifics live only in the JSON config files (keeps the repo and
  `share.sh` zips clean). Personal configs (`routers.json`,
  `devices.json`, `config.json`, `CLAUDE.local.md`) are gitignored;
  before pushing, verify with `git ls-files` that none are tracked.
- Match the code style: heavily commented, stdlib-only, single-file
  scripts; comments explain *why* (platform quirks, past bugs).
