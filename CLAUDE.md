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

- `monitor.py` — always-on collector, 12 daemon threads (all intervals
  below are the defaults — config.json `intervals` overrides them, hot-
  reloaded): ping (gateway +
  1.1.1.1/8.8.8.8/9.9.9.9 every 15s — BURSTS of 3 per target via
  ping_burst; pings.sent/received give real per-packet loss, which
  feeds the degradation window and the dashboard loss stats; failed
  runs shorter than the outage threshold land in `blips`, ≥4 blips/hour
  opens a kind='instability' event, closed after a blip-free hour),
  wifi (5m; BSSID/band + roam
  events + hourly neighbor scan into `wifi_scan`), device scan (5m),
  speedtest (30m, Ookla CLI; also captures ping.jitter,
  download/upload.latency.iqm = bufferbloat, packetLoss), public IP
  (10m), router checks (15s per router), iot watch (30s; devices.json
  entries with watch:true probed via the same check_router 4-tier
  ladder, MAC-keyed with the IP re-resolved each pass from the devices
  table + a live-ARP drift re-probe on failure, samples into
  `iot_pings`; never-seen MACs are skipped silently; scope='iot'
  outage events at detection outage_fails, HELD while the gateway is
  unreachable so a dead monitor-PC link can't page once per camera;
  un-watch/delete closes the event '[watch removed]'; excluded from
  uptime, the report, and the diagnosis banner; alerts only via the
  separate alerts.events.iot_outage key, default OFF — enabled_for
  maps outage+scope=iot onto it), DNS health (60s; failing
  domains named in the event note; www.speedtest.net is IN the rotation
  deliberately — instruments the Ookla CLI's "Couldn't resolve host
  name" failure class; each cycle ALSO queries the gateway
  + 1.1.1.1 + 8.8.8.8 directly via hand-rolled UDP `dns_query_direct`
  — dns_checks.resolver tags 'system'/'gateway'/IP, only 'system' rows
  drive events and the chart), speedtest failures RETRY ONCE after 60s
  (only the final outcome hits the DB — transients go to the log, not
  the chart), retention (daily, prunes >90d incl.
  blips),
  command poller (2s; executes web-UI commands from
  `data/commands.json`, writes `data/test_status.json` — two one-way
  files, ONE writer each, that's the whole web→monitor IPC), alert
  notifier (10s poll of the events table; toast immediately,
  webhook/email queue-and-retry so internet-down alerts flush on
  recovery; email is multipart plain+HTML — build_email_html inline-styled
  table layout (severity color band, detail rows in local time, LAN
  dashboard link via the UDP-connect own-IP trick), plain part kept as
  the fallback, and the subject is the event headline via meta.headline
  ("Recovered: …" on close; toast/webhook titles unchanged — email-only);
  config.json `alerts` block, hot-reloaded), topology (daily
  traceroute double-NAT check into `topology_checks` — verdict from hop
  2's address class, NOT hop counting: ISPs run 10.x internally, only a
  192.168.x second hop is confidently a second home router; 100.64/10 =
  CGNAT, not user-fixable). Flight recorder: when a gateway/internet/dns
  outage or degraded event OPENS, start_evidence_capture spawns a
  one-shot thread that snapshots traceroute + per-resolver DNS + gateway
  burst + router liveness + ARP into events.evidence (JSON, 16KB cap,
  60s throttle — concurrent events share one snapshot; the "where did
  the path die" data that's unrecoverable after recovery).
  - Cross-platform via IS_MACOS / IS_WINDOWS branches: Windows uses
    `ping -n` (+ "TTL=" guard because Windows ping exits 0 even for
    "unreachable"; locale-tolerant ms regex), `arp -a` (dash-MAC table),
    `route print`/`Get-NetRoute` gateway, `Get-NetIPAddress` prefix,
    `netsh wlan` Wi-Fi (% → approx dBm). Every subprocess gets
    CREATE_NO_WINDOW on Windows.
  - Router liveness 4-tier: ping → TCP 80/443 → closed-port RST probe
    (rst_probe, port 9: a live host refuses the SYN with an RST = fresh
    proof + a real latency sample; needs timeout ≥3s on Windows, which
    retries the SYN after an RST so "refused" surfaces at ~2.1s) → ARP.
    The ARP tier is STATE-AWARE on Windows (netsh neighbor state:
    'reachable' = answered ARP seconds ago; dead entries decay to
    unreachable within ~10s of our traffic, cutting silent-router
    down-detection from ~20min cache-linger to ~2-3 cycles; transient
    stale/probe states get re-read, unrecognized/localized output falls
    back to the old presence check) and presence-only on mac/linux.
    NOTE this network's Buffalo APs DROP closed-port SYNs (stealth), so
    the RST tier never fires for them — the state-aware ARP tier is
    what actually detects them; rst_probe earns its keep on gear that
    refuses (GE230 and the Linksys do). `method` column in
    `router_pings` records icmp/tcp/probe/arp; dashboard shows
    "Online · web" for tcp, "Online · silent" for probe AND arp — sage
    green (--status-silent) on the map. Footers: probe = "rst probe" at
    router cadence; arp = "arp state" at router cadence on Windows,
    "arp cache" at device-scan cadence elsewhere (checks.arp_cmd
    carries the platform).
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
  rendering is client-side JS from `const DATA`. Section order:
  diagnosis banner → "command deck" (house map front-and-center with the
  7 stat cards flanking it left/right in a 3-column grid ≥1200px —
  `grid-auto-flow: row dense` or the right column starts below the left
  one; map first then auto-fit cards below 1200) → per-router chart →
  latency/speed charts → outages → devices. The banner is a JS-side 8-rule table (stale page /
  monitor paused / gateway down / ISP down / DNS / AP down / degraded /
  all-clear, first match wins, mirroring monitor.py's causal
  precedence). Also generates `report.html` (ISP evidence report:
  outages, monthly measured uptime, below-plan speed tests, print CSS;
  throttled to every 10 min, gitignored like dashboard.html). A "Test
  now" topbar button drives on-demand ping/DNS/speed tests via
  `/api/test/run` + status polling. Feature
  set: stat cards with GOOD/FAIR/HIGH rating badges (JS `THRESHOLDS`
  defaults, overridable via config.json `thresholds`; the good/fair
  legend lives in the badge's hover tooltip, not on the card face;
  cards go 2-up under 480px); check-cadence footers on every stat/chart
  card, router hover card, and the internet node (command · age ·
  frequency, e.g. "ping · 6s ago · every ~30s"; `[data-checkfoot]`
  elements re-ticked every 10s client-side so ages stay honest between
  60s regens, amber when a check runs >2× its cadence. The frequency
  shown is the MEASURED cadence — median gap in the data (marked ~) —
  falling back to the configured interval, because real cadence = sleep
  + work time: a 15s router setting yields ~30s when ARP-only routers
  burn ping/TCP timeouts each cycle. Router footers show the live
  per-router method from router_pings.method; "arp cache" footers show
  the device-scan interval since that's what refreshes their evidence
  — this doubles as the "Online · silent" decoder. dashboard.py holds
  a defensive mirror of monitor.py's INTERVAL_DEFAULTS/INTERVAL_BOUNDS,
  same rule as the schema lists: update together); an architectural SVG house
  map — section-drawing style (flat roof slab, street-level datum line
  with elevation marker, hatched earth below), an "Internet" status node
  buried as a fiber line that rises into the main router (live latency +
  last speed test, red OFFLINE when down), compact router pills with
  hover/tap detail cards, Wi-Fi coverage bubbles (pulsing red hole when
  an AP is down), windows lit per-floor while that floor's APs are up,
  animated packet links; below ~520px container width the map rerenders
  as a compact portrait variant (`compact` flag: ~330-unit viewBox so
  text keeps its size, pills packed into centered rows, per-floor band
  height auto-grows to fit, no windows, fiber node below the house) —
  chosen at render time, so a rotate applies on the next reload; outages
  log with SVG 7-day incident timeline + filters (8 events shown,
  "older" expand scrolls inside a capped `.list-scroll` box; blips
  drawn as amber ticks on the timeline's bottom edge + a "Blips · 7d"
  summary chip; events with a flight-recorder snapshot get an
  "evidence" toggle expanding a full-width row — DNS-per-resolver,
  router liveness, traceroute; NB the JS lives in a Python string, so
  backslash escapes must be doubled — an unescaped \n in the template
  killed the whole page's script once); DNS card sub-line shows the
  direct per-resolver verdict (router vs 1.1.1.1 vs 8.8.8.8, details
  in the tooltip); Chart.js
  charts (vendored) with threshold reference lines and the speed chart
  pinned to 0..plan+100 so the plan lines stay visible — chart ranges
  are PER-CHART (`chartRange` map + `rangeFor(key)`, a mini 3h/24h/7d
  toggle in each card's `.card-tools` corner) plus a topbar
  `#globalRange` toggle that sets them all (lit only while every chart
  agrees); the load-time default is config `default_range_hours`
  (3|24|168, Settings → General select, omitted when 3 = built-in
  default; served in the payload → `DEFAULT_RANGE`); ONE `RANGE_CHARTS`
  registry feeds both the toggles and rerenderCharts (replaces the two
  hand-maintained renderer lists);
  `timeScale(hours)`/`rangeWord(hours)` take params. The Wi-Fi signal
  chart was REMOVED (his call; monitor still collects wifi snapshots —
  roam events/timeline category stay; a legacy thresholds.wifi config
  key is carried through General saves untouched). Latency/Loss/Speed
  cards have "Check now" buttons riding the one-test-at-a-time command
  rail (quick = ping+DNS, speed = full test; on-demand results are
  written to the SAME DB tables, so they appear on the charts on the
  next regen; inline note is instant), and the devices section head has
  a "Scan now" button → POST /api/devices/scan (in serve.py's LAN_API
  carve-out; 60s cooldown/409 server-side) polling the shared status
  file — chart TOOLTIPS are enabled:false + an external handler
  rendering the tooltip model into a per-card `.chart-tip` panel that
  TRACKS THE CURSOR's x (caretX, clamped) while pinned BELOW the
  `.chart-box`, so it never covers the plot (per-chart label callbacks
  still apply); the
  timeline is CLICKABLE (rows and timeline marks share a startMs|cat
  `data-ev` key → scroll+flash the row, auto-reset filter / expand
  "older"), summary chips click through to their filter pill, log rows
  carry relative times; map hover cards have a "chart ↓" link focusing
  that router's line in the per-router chart (others dimmed,
  `routerFocusName` reapplied by applyRouterFocus() after re-renders;
  cards accept the mouse while shown — hide is on a 250ms delay), a
  plain-language <title> decoder on the status line, and the map
  re-renders on resize across the 520px compact threshold
  (renderHouseMap is a named function); TWO `.quick-nav` jump bars
  share delegated clicks — a static always-visible one under the topbar
  and the fixed one sliding in past 480px scroll
  (Map/Latency/Speed/Outages/Devices → `#sec-*` ids incl. `sec-speed`);
  devices table with friendly names from
  devices.json (online rows first, away rows collapsed behind a toggle;
  a `#devSearch` live filter matches name/hostname/IP/MAC and shows
  away matches too; first_seen within 24h ⇒ green "new" tag;
  MAC as a muted mono sub-line under the device name — NOT a column,
  which was the widest cell and forced phone side-scroll; phone tables
  must fit without side-scroll since scrollbars are invisible),
  `hide_ip_prefixes` drops matching devices; IoT devices live INSIDE
  the devices section as the right column of `.dev-cols` (devices chart
  on top, all-devices table left 3fr / `#iotCard` right 2fr; right card
  hidden + `no-iot` full-width left when nothing is typed/watched;
  stacked <940px; BOTH cards share one table anatomy — equal 1fr/1fr
  widths, MAC in its own `.col-mac` column (on phones <640px the MAC
  column and IoT's `.col-ip-iot` collapse back into the `.dev-mac`
  sub-line — a MAC column once forced invisible side-scroll at 375px,
  so both renderings exist and CSS picks per breakpoint); IoT columns
  are Device/MAC/IP/Status/Latency/Last checked with the type label as
  a rowspan `.iot-type-cell` stacking categories down the left edge;
  NB `.chart-card` is `overflow: visible` — an auto
  overflow with hidden scrollbars made cards silently wheel-scrollable
  whenever the hover readout poked past an edge; wide content must
  scroll in its own wrapper, never on the card;
  own outage-log category cat='iot'/"IoT" chip — hardDown filters
  cat outage|dns so IoT is auto-excluded from the downtime chips; NOT
  in the diagnosis banner, whose rules are all scope-filtered). Chart colors
  are baked at build time → charts fully re-render on theme change. Page
  scrollbars are hidden globally but scrolling still works — beware
  `overflow-x: clip` (it coerces overflow-y to clip and kills page
  scrolling; use `hidden`).
- `serve.py` — stdlib LAN web server. LAN sees ONLY dashboard.html,
  report.html + the two vendor JS files (whitelist ROUTES dict;
  DB/logs/config unreachable), PLUS exactly the `LAN_API` carve-out
  (`/api/test/status` GET, `/api/test/run` POST — Host header must be a
  private-IP literal). Localhost additionally gets `/setup` (first-run
  wizard), `/settings`, and the `/api/*` config endpoints — POSTs are
  guarded against CSRF/DNS-rebinding (Host/Origin/Content-Type checks,
  256KB body cap). `/` 302s to the wizard on a fresh install and serves a
  "warming up" page until dashboard.html first exists. Port 8080
  (`NETMON_WEB_PORT` to override), no-store on HTML, 503 + Retry-After
  if the file is mid-rewrite.
- `settings_api.py` — HTTP-agnostic backend for the wizard/settings:
  tolerant loads, atomic saves (tmp+fsync+os.replace with retry),
  per-file validators returning (errors, warnings), the background
  discovery job (threading.Lock-guarded status dict, decorates results
  with is_gateway/suggested/known_router_name), and
  `load_device_census()` — read-only mac→{ip, hostname, first/last
  seen, online} from the DB's devices table (30d window, respects
  hide_ip_prefixes, {} on fresh install/busy DB) served as `census` on
  GET /api/config for the Devices tab, plus `device_scan_cmd()` (the
  human-readable scan command shown by the Devices tab's scan button —
  mirrors monitor.py's nmap-else-ping-sweep decision, served as
  meta.scan_cmd; `router_scan_cmd()` similarly discloses the Routers-tab
  scan pipeline as meta.router_scan_cmd, both sharing `_subnet_label()`
  built on scan_routers.get_own_ip_and_prefix) and POST
  /api/devices/scan (LAN-reachable via serve.py's LAN_API; guarded like
  start_test — 409 while running, 60s cooldown → 429) → writes an
  action:"scan_now" command; monitor.py's command_loop runs
  run_device_scan (the extracted device_loop body, state-dict + lock so
  the scheduled cycle and on-demand path share the new-device baseline
  and can't sweep concurrently) and reports via test_status.json.
- `settings_page.py` — `WIZARD_HTML` (auto-scan → floors → routers →
  review, 409-guarded overwrite, double-NAT heads-up from the
  piggybacked topology check) and `SETTINGS_HTML` (General/Routers/
  Devices/Alerts tabs; Alerts has a Send-test-alert button riding the
  command rail) as Python strings served from memory, styled to match
  the dashboard. Tabs are a real ARIA tablist (roving tabindex,
  Arrow/Home/End) and persist across refresh via the URL hash
  (activateTab writes history.replaceState('#name'), restored after
  load()). Per-tab unsaved-changes guard: input/change +
  row-button clicks set a dirty flag (amber dot on the tab,
  beforeunload warning; saves/test-alert/scan/filter box exempt),
  cleared by that tab's successful save. The Devices tab renders the
  CENSUS union — KNOWN devices by default (entry/manual/typed rows),
  "Show all N seen devices" or any search query reveals the full census
  (visibility-based, not a re-render, so unsaved edits survive; the
  view toggle and scans are on the dirty-tracker's clean-list): one
  row per seen device (known-first then numeric-IP-ascending;
  hostname as name-input placeholder, mono context line; the ✕ renders
  only when the row HAS an entry, and clearing fades the row + "entry
  forgotten on save" note — reversed by typing) + a Scan-for-devices-now
  button (shows the real command from meta.scan_cmd, polls
  /api/test/status, re-pulls the census on done) + "Named, but not
  seen in 30 days" group +
  manual add-by-MAC; save collects only rows with name/type/watch and
  keeps the compact string-or-object form. Quiet hours are
  `<input type=time>` (loadAlerts pads legacy "7:00" → "07:00" or the
  input shows empty and the next save silently drops the block).
- `scan_routers.py` — standalone LAN scanner to find router IPs (TCP
  80/443 sweep + ping sweep + ARP + HTTP title fingerprint). Core logic
  is the importable `discover(progress=None)`; the CLI prints from its
  return value.
- `version.py` — single source of truth for the version; every script
  imports it with a fallback and supports `--version`. dashboard.py runs
  a daily fail-silent GitHub releases check (cached in
  `data/update_check.json`, opt-out `"update_check": false`; the check
  also caches the release NOTES + share-zip asset URL for the banner
  and the updater).
- `update.py` — the self-updater engine behind three front doors:
  Settings → General → Updates ("Update now" → POST /api/update/run
  spawns `python update.py --auto` DETACHED), the shipped
  `update-windows.bat`/`update.sh` double-click wrappers (interactive),
  and plain `python update.py` (also `--check`, `--rollback`,
  `--version`). Pipeline: fetch releases/latest (env
  `NETMON_UPDATE_API` overrides the repo for forks/tests) → download
  the netmon-share.zip asset (https-only except loopback/private hosts;
  size caps) → validate (zip-slip guard, required files present,
  version INSIDE the zip must match the tag) → backup replaced files to
  `data/backup/v<current>/` (manifest.json lists replaced+created; last
  2 backups kept) → per-file tmp+os.replace swap → in-memory compile
  check of every new .py, AUTO-ROLLBACK on failure. PROTECTED list
  refuses to ever write configs/data/logs/dashboard.html even if a zip
  contains them. Progress → `data/update_status.json` (ONE writer, the
  updater process; Settings polls GET /api/update/status). RESTART
  DESIGN: the updater never kills or restarts services — monitor.py and
  serve.py run update.install_restart_watcher(), which watches the
  status file for state=done with a DIFFERENT version and then exits
  the process with code 1 so Task Scheduler (-RestartCount) / launchd
  (KeepAlive) restarts it on the new code ~1 min later. os.execv
  self-restart was tested and REJECTED: on Windows the exec'd process
  becomes an orphan outside the task's job — the task flips to Ready,
  Stop-ScheduledTask can't kill it, and a later task start would race
  it. The Settings poll treats fetch failures as "web server
  restarting, keep waiting" and declares success when the reported
  version CHANGES; update endpoints are localhost-only (NOT in
  serve.py's LAN_API — a phone must not update the house monitor).
- `diagnose.py` — builds `netmon-diagnostics-*.zip` (report + logs tail
  + configs) for remote troubleshooting; `build_bundle()` is importable.
- SQLite tables: `pings` (+sent/received burst counts), `router_pings`,
  `devices` (+idx_devices_mac for the iot MAC→IP lookups), `dns_checks`
  (+resolver), `events` (kinds: outage/degraded/
  ip_change/new_device/wifi_roam/instability; scopes incl. 'iot';
  +evidence JSON), `blips`
  (micro-outages shorter than the outage threshold), `iot_pings`
  (watched-IoT liveness, MAC-keyed — deliberately NOT router_pings,
  whose name-keyed history feeds the router-list fallback), `public_ip`,
  `speedtests`, `wifi`, `wifi_scan`, `topology_checks`.
  Schema changes go in BOTH guarded-ALTER migration lists (monitor.py
  init_db AND dashboard.py's defensive mirror).
- Config files (all optional, user-editable JSON in this folder; the
  committed `.example.json` files show the format; normally written by
  the wizard/settings UI, hot-reloaded — routers ≤15s, devices ≤5min,
  config on next dashboard regen, no restarts):
  - `routers.json` — [{name, ip, floor, role?}], order = file order.
    `role: "isp"` (at most one; Settings → Routers has a dedicated
    "Internet box" section for it) marks the ISP modem/ONT: monitored
    like any router, but the map draws it as a wall-mounted box where
    the fiber enters — chain internet → ISP box → main router, with the
    first segment colored by internet reachability and the second by
    the box's own liveness — instead of a floor pill; it's excluded
    from floor derivation and gets its own diagnosis-banner wording.
    COMBO-BOX MERGE: any routers.json entry whose IP is the default
    gateway (usually the role:"isp" box when the ISP modem IS the house
    router) is deduped, not shown twice — monitor.py's router_loop
    skips pinging it (drop_gateway_dupes; the gateway ping thread
    already covers it, else every outage logs as scope='gateway' AND
    scope='router' with double alerts), dashboard.py drops it from
    router_summary (matching on the CONFIGURED IP, so the ≤7d
    router_pings tail from before the skip can't resurrect it) and
    sets gateway.isp_name when it was the role:"isp" entry — the map
    then runs the fiber straight into the Main Router pill, shows the
    box's name on that pill's IP line, and the banner's gateway-down
    rule swaps to ISP-flavored wording ("that's a call to the ISP")
    since "not your ISP" would be exactly wrong there. The settings
    validator warns (not errors) when an entered IP is the gateway.
    When the
    file exists it is AUTHORITATIVE for the dashboard's router list —
    deleted routers must not resurrect from router_pings history (they
    used to); the history-derived fallback only applies when the file
    is missing/empty.
  - `devices.json` — {mac: friendly name} OR {mac: {name, type?,
    watch?}} for IoT devices (types camera/intercom/printer/light/plug/
    speaker/tv/other; Settings → Devices has the Type dropdown + Watch
    checkbox). Typed devices render in the dashboard's "IoT devices"
    section grouped by type (watched rows show live ladder status with
    the same "Online · web"/"Online · silent" decoding as routers,
    sage .status-silent-pill; unwatched rows are "scan only" — census
    online/away) and get a muted type tag in the main devices table
    (which they deliberately stay in, so scan counts still match).
    normalize_devices writes the COMPACT form back (plain string when
    no type and watch off) so untouched entries stay byte-identical;
    the string-or-object normalizer is mirrored in monitor.py
    (_device_meta_from_value/IOT_TYPES), dashboard.py, and
    settings_api.py — update together.
  - `config.json` — {title, floors[], underground_floors[],
    main_router_floor} + optional `hide_ip_prefixes` (deliberately has
    NO Settings-UI field — it confused users; hand-edit only, README
    documents it; the Settings save carries an existing value through
    untouched), `thresholds`
    (incl. `bufferbloat` and `plan_pct` {good, fair}% — the Speed card
    rating AND the report's below-plan bar), `plan_down_mbps`/
    `plan_up_mbps`, `monitor_location` (which router/AP the monitor PC
    hangs off — any router incl. the ISP box, or "Main Router"; labels
    the Speed card/chart/report with "via X" AND moves the map's speed
    readout from the Internet cloud onto that node's hover card, so a
    slow reading isn't blamed on the ISP when it's an in-house path),
    `detection` ({outage_fails, degraded_latency_ms, degraded_loss_pct}
    — the monitor's EVENT triggers, hot-reloaded via detection(), bounds
    in DETECTION_BOUNDS mirrored in settings_api; display `thresholds`
    only color badges, `detection` decides what becomes an event),
    `custom_targets` ([{name, host}] max 5, Settings → General "Extra
    ping targets"; ping-burst per ping cycle into pings with
    target_type='custom' KEYED BY NAME (rename host, keep history);
    per-target scope='target' outage events (router_name = target name)
    SUPPRESSED while the internet itself is down; own chart card "Your
    targets" (hidden when none) + "Targets" filter in the outage log;
    NOT counted in downtime/uptime — a dead game server isn't the
    line's fault),
    `alerts`
    (see config.example.json; email password is plaintext — app
    passwords only; events.iot_outage is the separate default-off
    opt-in for watched-IoT outages — kind='outage' scope='iot' maps
    onto it in enabled_for so it can't ride the plain outage toggle),
    `default_range_hours` (3|24|168 — the dashboard charts' load-time
    range; Settings → General select; omitted when 3, the built-in
    default), `intervals` ({check: seconds} overriding
    monitor.py's INTERVAL_DEFAULTS — keys ping/router/dns/wifi/devices/
    iot/speedtest/public_ip; hot-reloaded per loop pass via the stamp-cached
    check_interval(), clamped to INTERVAL_BOUNDS, edited from the
    Settings General tab; bounds are mirrored in settings_api's
    validator and dashboard.py — update all three together).
- `setup.sh` / `setup.ps1` install services, vendor Chart.js (committed
  in `vendor/`, re-downloaded via CDN fallback chain if missing), Ookla
  speedtest CLI (NOT homebrew/pip speedtest-cli), optional nmap.
  `share.sh` → clean generic zip (no personal config; incl. the Docker
  files).
- `Dockerfile` + `entrypoint.sh` + `docker-compose.yml` — Linux/home-server
  deployment. python:3.13-slim + apt iputils-ping/net-tools/iproute2/
  traceroute/nmap + Ookla speedtest static binary (ARG-pinned). Design:
  SEED-ON-START — image code lives in /opt/netmon-dist, entrypoint copies
  *.py + *.example.json + vendor/ over the /app bind mount on every boot
  (never touches configs/data/logs/html), so /app is the single mutable-
  state mount (os.replace atomic saves need directory mounts — single-file
  binds break them) and the container deterministically runs the image's
  code; in-container "Update now" is therefore reverted on restart —
  updates = git pull + compose build (README documents this). Entrypoint
  supervises monitor.py + serve.py + a 60s `python dashboard.py; sleep 60`
  loop (dashboard.py is one-shot); `wait -n; exit 1` on monitor/serve death
  so restart: unless-stopped relaunches (also services update.py's
  restart-watcher os._exit(1); the watcher baselines the status-file mtime
  at start, so a persisted state=done file can't crash-loop — verified).
  Dashboard-render failures only log. network_mode: host REQUIRED (ARP/
  sweep/discovery are blind behind bridge NAT); port from NETMON_WEB_PORT
  alone (`ports:` ignored under host networking); CAP_NET_RAW is the one
  needed capability (in Docker's default set); runs as root (unprivileged
  nmap -sn silently degrades ARP→TCP probes; Ookla wants writable $HOME).
  .dockerignore is an ALLOWLIST (`*` then !*.py etc.) so personal configs
  can't leak into images built from a live install. Linux deltas: ARP tier
  presence-only (silent-router down-lag ~20min), no Wi-Fi, no toasts.

## Working notes

- Nothing person-specific may be hardcoded in the Python — house
  specifics live only in the JSON config files (keeps the repo and
  `share.sh` zips clean). Personal configs (`routers.json`,
  `devices.json`, `config.json`, `CLAUDE.local.md`) are gitignored;
  before pushing, verify with `git ls-files` that none are tracked.
- Match the code style: heavily commented, stdlib-only, single-file
  scripts; comments explain *why* (platform quirks, past bugs).
