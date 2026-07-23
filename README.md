# Home Network Monitor

Continuously watches your home internet connection and logs disruptions, so
next time someone says "the internet is being weird" you have data instead of
a guess.

## What it does

- **Pings your router (gateway) and 3 external internet targets** (1.1.1.1,
  8.8.8.8, 9.9.9.9) every 15 seconds.
- **Tells apart two different kinds of problem:**
  - *Gateway down* — your router or this Mac's Wi-Fi lost the local network.
    Usually means: router needs a reboot, or you're too far from it.
  - *Internet down* — your router/Wi-Fi is fine but nothing outside your
    house is reachable. Usually means: your ISP is having an outage.
  - *Degraded* — nothing is fully down, but latency or packet loss is
    elevated. This is the "feels slow" case that a simple up/down check
    would miss.
- **Speed tests** every 30 minutes (download/upload/ping), if a speed test
  tool is installed (see below).
- **Wi-Fi signal strength** for this Mac every 5 minutes.
- **Devices on your LAN** (IP / MAC / hostname) every 5 minutes. Each scan
  first sweeps your whole subnet so even devices that never talk to
  this Mac directly show up, then reads the Mac's ARP cache and resolves
  hostnames (with a strict time budget so slow DNS can't stall the scan).
  If **nmap** is installed (setup.sh offers to install it), the sweep uses
  it — it probes each host several ways (ICMP + TCP), catching
  ping-blocking devices more reliably and finishing faster; otherwise a
  built-in ping sweep is used. Multicast/broadcast noise (mDNS, SSDP,
  etc.) is filtered out automatically.
- **Public IP** every 10 minutes, via a plain-text lookup (api.ipify.org,
  with two fallbacks). When it changes, that's logged as an event — ISPs
  often reassign your address around a brief disconnect/reconnect, so
  seeing "IP changed" line up with a gap in the ping log is a useful clue
  about whether an issue was on their end. This is a different thing from
  the local (LAN) IPs in the devices table above — your public IP is the
  single address your whole household is seen as from the internet.
- **Other routers/access points around the house**, if you list them in
  `routers.json` (see below) — each gets pinged every 15 seconds just like
  the main gateway, with its own up/down detection, so "the mesh node in
  the garage died" shows up by name instead of as a vague slowdown. The
  dashboard shows each router's floor/location (if you set one), plus a
  per-router latency history chart so you can see *which* floor's Wi-Fi
  degrades and when. Routers that are configured to ignore ping are still
  detected via their web-admin port (shown as "Online (web)").
- **DNS health** every minute — the classic "internet feels broken but
  pings look fine" failure is DNS. Resolution time appears on the latency
  chart and as a 24h stat card; sustained failures are logged as "DNS
  failure" events.
- **Jitter** — how *unsteady* the latency is (what ruins video calls and
  gaming even when average latency looks fine). Shown as a 24h stat card
  and a line on the latency chart.
- **New-device alerts** — when a never-seen-before device joins your
  network, that's logged as an event ("New device joined the network:
  ..."). Give devices friendly names in `devices.json` (see below) so
  alerts and the device table say "Dad's iPhone" instead of a raw MAC.
- **Device presence history** — the device table shows every device seen
  in the past week with Online/Away status and last-seen time, plus a
  "devices online over time" chart, so "when did that phone leave the
  house" has an answer.
- **Honest sleep gaps** — when this Mac was asleep (or the monitor wasn't
  running), nothing was measured. Those periods now show up explicitly as
  "Monitoring paused" in the event log and as visible breaks in the
  charts, instead of silently looking like healthy uptime.
- **A plain-language diagnosis banner** at the top of the dashboard that
  says what's wrong *right now* and what to do about it — "Internet is
  down but your router is fine → it's your ISP, check the modem lights"
  instead of making you read charts.
- **Bufferbloat / latency under load** — the speed test also records how
  much your latency climbs while the line is saturated (the reason
  "speed tests look fine but calls stutter during downloads"). Rated
  GOOD/FAIR/POOR with a hint about enabling SQM/QoS. Needs the official
  Ookla CLI.
- **A "Test now" button** — run a live ping + DNS check (optionally a
  speed test) on demand from any device on your Wi-Fi, results in
  seconds. Rate-limited, and only reachable from your own LAN.
- **Alerts (optional)** — desktop notifications on the monitor PC,
  webhook push (ntfy.sh / Slack / Discord-style), or email (one or
  several recipients — add them one per row in Settings) when an
  outage outlasts a minimum duration, plus a recovery message with the
  total downtime ("Internet was down 02:14–02:31, 17m"). Configured in
  Settings → Alerts; alerts that can't leave the house during an
  internet outage are queued and sent on recovery. Quiet hours
  supported.
- **A printable ISP evidence report** (`report.html`, linked from the
  outage log) — every outage with timestamps and durations, monthly
  measured uptime, and the speed tests that came in under 80% of the
  plan you pay for. Made for attaching to an ISP complaint.
- **Wi-Fi channel advice** — an hourly scan of neighboring networks; if
  your 2.4 GHz channel is crowded, the dashboard suggests the quietest
  of channels 1/6/11. (On Windows 11 this needs Location permission for
  desktop apps, else Windows hides the neighbor list.)
- **Double-NAT detection** — a daily traceroute check that spots the
  classic "ISP box and your own router are both doing NAT" setup that
  breaks consoles, VoIP and port forwarding, with a fix hint (bridge
  mode / DMZ). Also shown during the setup wizard.
- Writes everything to a local SQLite database (`data/network_monitor.db`)
  and regenerates a **self-contained dashboard** (`dashboard.html`) every
  minute, showing uptime %, an outage/degradation log, latency & loss
  trends, speed test history, Wi-Fi signal trend, current public IP, the
  status of every router you've listed, and the current device list.
  History older than 90 days is pruned automatically once a day so the
  database doesn't grow forever (events and speed tests are kept
  indefinitely — they're tiny).

Nothing leaves your Mac — there's no cloud service involved, just local
files.

## One-time setup

1. Open **Terminal** and go to the folder you put this in, e.g.:
   ```
   cd ~/network-monitor
   ```
2. Run the setup script:
   ```
   bash setup.sh
   ```
   This registers three background services with macOS (`launchd`) so they
   run continuously — including after you restart your Mac — without
   needing Terminal or this chat open:
   - `com.<your username>.netmon.monitor` — the always-on pinger/logger
   - `com.<your username>.netmon.dashboard` — regenerates `dashboard.html`
     every minute
   - `com.<your username>.netmon.web` — serves the dashboard to your whole
     network on port 8080 (see below)

   (The service names are derived from whoever runs `setup.sh`, so this
   folder works on anyone's Mac without editing anything.)

3. Open `http://localhost:8080/` in a browser on the same machine. The
   first time, a **setup wizard** appears: it scans your network, finds
   your routers/access points, and writes the config files for you.

That's it. Give it a few minutes to collect data, then refresh the page
whenever you want the latest — it rewrites itself automatically every
minute, you just need to reload the tab. (You can also open
`dashboard.html` directly as a file, without the web server.)

### Setting it up on Windows (10/11)

The same folder works on Windows — the monitor knows the Windows versions
of every command it uses (`ping -n`, `arp -a`, `netsh wlan`, `route print`).

1. Install **Python 3** if you don't have it: from
   [python.org/downloads](https://www.python.org/downloads/) (tick *"Add
   python.exe to PATH"*), or in PowerShell: `winget install Python.Python.3.12`
2. **Double-click `setup-windows.bat`** (it runs `setup.ps1`).
3. Open `http://localhost:8080/` in a browser — the first-run **setup
   wizard** scans your network and writes the config for you.

That registers three Task Scheduler jobs for your user account — *NetMon
Monitor* (runs continuously from logon, auto-restarts if it crashes),
*NetMon Dashboard* (regenerates `dashboard.html` every minute), and
*NetMon Web* (serves the dashboard to your network on port 8080) —
downloads the chart library, and offers to install the optional speed test
CLI and nmap via winget. To stop everything: double-click
`uninstall-windows.bat`.

Windows notes:

- The Wi-Fi chart uses `netsh`, which reports signal as a percentage; the
  monitor converts it to an approximate dBm value (fine for spotting trends,
  not lab-accurate).
- On a non-English Windows, some Wi-Fi fields (channel/rate) may come back
  empty — everything else is parsed language-independently and works.
- Logs go to the same `logs\` folder (`monitor.out.log`, `monitor.err.log`).
- To check on it: open **Task Scheduler** and look for the three *NetMon*
  entries, or in PowerShell: `Get-ScheduledTask -TaskName 'NetMon*'`
- When run as administrator, `setup.ps1` also opens TCP 8080 in the
  Windows firewall (Private networks only) so other devices in the house
  can reach the dashboard; without admin rights it prints the one command
  to run yourself.

## Viewing the dashboard from any device (port 8080)

`serve.py` runs as the third background service and serves the dashboard
to your whole network:

    http://<the-monitor-machine's-ip>:8080/

(The setup script prints the exact address.) It's deliberately minimal:
the network can fetch **only** `dashboard.html` and the two chart-library
files — your database, logs, and config files are not reachable. The
setup wizard, settings pages, and the config API answer only to the
machine itself; from any other device they return a "settings live on the
monitor PC" page.

If port 8080 is taken, set the `NETMON_WEB_PORT` environment variable for
the service (or just run `python serve.py` manually to test).

## Setup wizard & Settings

Open these **on the machine running the monitor**:

- `http://localhost:8080/setup` — the first-run wizard. Scans your
  network (about a minute), shows everything it found with the likely
  routers pre-ticked, lets you name your floors, and writes all three
  config files. Rerunning it later asks before overwriting an existing
  setup.
- `http://localhost:8080/settings` — edit everything afterwards: title,
  floors, thresholds, plan speeds, the router list (with a rescan
  button), and device names.

Changes apply on their own: router-list edits reach the monitor within
~15 seconds, device names within ~5 minutes, and everything else on the
dashboard's next one-minute regeneration. No restarts, no Task
Scheduler/launchctl commands.

Prefer plain text files? All three configs (`routers.json`,
`devices.json`, `config.json`) remain ordinary JSON you can edit by hand
— the same hot-reloading applies. The sections below document their
formats.

### Optional: speed test history

The dashboard's speed test chart stays empty until a speed test tool is
installed. `setup.sh` tries to install Ookla's official CLI via Homebrew
automatically. To do it manually:
```
brew tap teamookla/speedtest
brew install speedtest
```
**Do not run `brew install speedtest-cli`** — despite the similar name,
that's a different, unmaintained community tool with an incompatible
command-line interface, and the monitor won't be able to use it (it'll log
a clear error telling you to fix it). `setup.sh` checks for and
automatically replaces it if it's already installed.

(If you don't use Homebrew at all, `pip3 install speedtest-cli` installs a
Python package — confusingly, also under this name, but this one *is* used
correctly by the monitor as an automatic fallback when no CLI binary is
found. It's only the Homebrew formula of the same name that's the wrong
tool.)

On Windows, `setup.ps1` tries this automatically; manually it's:
```
winget install Ookla.Speedtest.CLI
```

### Optional: monitoring other routers/access points

If you have more than one router, mesh node, or access point in the house,
list them in `routers.json` in this folder — a plain JSON array of
`{"name": ..., "ip": ...}` objects:

```json
[
  { "name": "Living Room AP", "ip": "192.168.1.5", "floor": "Ground Floor" },
  { "name": "Bedroom Mesh Node", "ip": "192.168.1.6", "floor": "First Floor" },
  { "name": "Garage AP", "ip": "192.168.1.7" }
]
```

`name` can be anything you'll recognize later (it's what shows up on the
dashboard and in the outage log). `floor` is optional — if set, it shows
as a location column in the routers table. The table displays routers in
the same order as this file, so arranging the file by floor groups them
nicely. `ip` needs to be that device's actual IP on your network — a few
ways to find them:

- Your mesh system's app (eero, Orbi, Google Wifi, etc.) usually lists each
  node's IP under its device/network details.
- Log into your main router's admin page (`http://<gateway IP>`) and look
  for a "connected devices" or "DHCP clients" list.
- Check the **Devices on your network** table on the dashboard itself —
  routers/APs often show up there too, sometimes identifiable by hostname.
- Run the included scanner, which checks every address on your network for
  an open web admin port (80/443) — the way you'd normally reach a
  router's settings page — and shows you the page title for each hit:
  ```
  python3 scan_routers.py
  ```
  It prints a table of IP / MAC / hostname / page title, and saves the
  same to `scan_results.json`. It'll also catch printers, NAS boxes, and
  other web-admin devices, not just routers — use the title/Server column
  to tell them apart (a router's login page usually names the brand).

One entry may carry `"role": "isp"` to mark your ISP's modem/ONT (the
Settings page's *Routers* tab has a dedicated "Internet box" section for
it). It's monitored like any other router, but the house map draws it as
a wall-mounted box where the fiber enters — internet → ISP box → your
router — so you can see *which* leg of the connection died. If the ISP
box **is** your main router (a modem-router combo, no separate router of
your own), you can still add it: the dashboard notices its IP is the
network gateway and merges it into the Main Router node instead of
showing the same device twice.

The monitor picks up edits to `routers.json` by itself within ~15 seconds
— no restart needed. The dashboard will start showing a **Routers &
access points** table (with each one's live status, 24h uptime, and
average latency) on its next regeneration. No entry is required — leave
the file as `[]` if you only want to monitor your main connection. (The
Settings page's *Routers* tab edits this same file, with a built-in
network scan.)

### Optional: naming your devices

The device table shows raw hostnames/MACs by default. To give devices
friendly names, edit `devices.json` in this folder — a plain
`{"mac": "name"}` mapping:

```json
{
  "11:22:33:44:55:66": "Dad's iPhone",
  "aa:bb:cc:dd:ee:ff": "Living room TV"
}
```

Copy each MAC straight from the dashboard's device table. Names appear in
the device table and in "New device joined" events. The monitor picks up
edits on the next scan (no restart needed); the dashboard on its next
one-minute regeneration.

New-device alerts work automatically: any MAC never recorded before
triggers a "New device" event in the outage log. (The first scan after a
monitor restart absorbs unknown devices silently as a baseline, so a code
update can't flood the log.)

### Optional: IoT devices (cameras, intercoms, printers, lights…)

A devices.json value can also be an object that tags the device with a
type and, optionally, puts it under active watch:

```json
{
  "11:22:33:44:55:66": { "name": "Front door camera", "type": "camera", "watch": true },
  "22:33:44:55:66:77": { "name": "Office printer", "type": "printer" },
  "aa:bb:cc:dd:ee:ff": "Living room TV"
}
```

(The Settings page's *Devices* tab has a Type dropdown and a Watch
checkbox for this — no hand-editing needed. Plain-string entries keep
working unchanged.)

Any device with a `type` appears in a dedicated **IoT devices** section
on the dashboard, grouped by type — `camera`, `intercom`, `printer`,
`light`, `plug`, `speaker`, `tv`, or `other`.

`"watch": true` upgrades a device from "seen by the periodic scan" to
**actively checked every ~30 seconds** (configurable as `intervals.iot`,
10–600 s) with the same 4-tier liveness ladder used for routers:
ping → TCP → closed-port probe → ARP. That matters for IoT gear —
cameras and printers often ignore ping but answer on their RTSP/web
ports, showing as "Online · web" or "Online · silent" just like silent
access points. Watched devices are tracked by MAC, so a DHCP lease
change doesn't lose them; the monitor re-resolves the IP automatically.

When a watched device stops answering, an **"IoT device down"** event
opens in the outage log (with its own filter chip and timeline marks)
and closes on recovery. These events are deliberately *excluded* from
your internet uptime numbers and the ISP evidence report — a dead
lightbulb is not your ISP's fault — and they never alert unless you
enable the separate **"IoT devices (watched)"** checkbox in Settings →
Alerts (off by default). While your own network/internet connection is
down, IoT events are held back so one house-wide outage doesn't page
you once per camera.

### Optional: customizing the house map (config.json)

The dashboard's house map draws your floors from `config.json`:

```json
{
  "title": "Home Network Monitor",
  "floors": ["First Floor", "Ground Floor", "Basement"],
  "underground_floors": ["Basement"],
  "main_router_floor": "Ground Floor"
}
```

- `title` — the dashboard's heading (e.g. "Smith House Network").
- `floors` — top-to-bottom list; the house is drawn with one band per
  entry, however many you have. These names must match the `floor` values
  in `routers.json`.
- `underground_floors` — drawn below the grass line with earth shading.
- `main_router_floor` — which floor the main router (gateway) card sits
  in the center of.

The file is optional: without it the floors are derived from
`routers.json` (in file order, top first), anything named "basement" is
treated as underground, and the main router lands on the lowest
above-ground floor.

### Optional: hiding stale devices by IP (config.json)

If an old subnet is still cluttering the device table (e.g. leftover
`192.168.100.x` entries after renumbering the house), add an
`"hide_ip_prefixes"` list to `config.json`. Any device whose current IP
starts with one of the prefixes is dropped from the device table, the
online/seen counts, and the devices-online chart:

```json
{ "hide_ip_prefixes": ["192.168.100."] }
```

It filters by the device's *latest* IP, so anything that has since moved
onto your main subnet still shows up normally.

### Optional: tuning what counts as "normal" (config.json)

Each metric card and several charts show a rating (GOOD / FAIR / HIGH) and
a "normal range" helper. The defaults reflect common home-network norms —
latency good ≤ 40 ms, jitter good ≤ 10 ms, DNS good ≤ 40 ms, packet loss
good < 1%, uptime good ≥ 99.9%, Wi-Fi good ≥ −60 dBm. If your household's
expectations differ, override any of them in `config.json`:

```json
{
  "thresholds": {
    "latency": { "good": 30, "fair": 80 },
    "wifi":    { "good": -55, "fair": -67 }
  },
  "plan_down_mbps": 500,
  "plan_up_mbps": 50
}
```

Only the metrics you list are changed; the rest keep their defaults. Keys
are `latency`, `jitter`, `dns`, `loss`, `uptime`, `wifi` (each with `good`
and `fair`). `plan_down_mbps` / `plan_up_mbps`, if set, draw your plan's
speeds as reference lines on the speed-test chart so you can see at a
glance whether you're getting what you pay for. All optional.

## Sharing this setup with someone else

Run `bash share.sh` — it builds `netmon-share.zip` containing the code,
setup scripts (macOS *and* Windows), README, chart library, and *example*
config files, but none of your personal data (no database, logs, router
IPs, device names, or dashboard). Send the zip to a friend; they unzip
it, run the setup for their OS — `bash setup.sh` on a Mac, or a
double-click of `setup-windows.bat` on Windows — and the first-run wizard
at `http://localhost:8080/` configures everything for their home. (The
monitoring code itself also runs on Linux; see the Raspberry Pi section
below for service setup there.) Nothing in the code is tied to any one
person or house.

The project also lives at
[github.com/MFBALGO/home-network-monitor](https://github.com/MFBALGO/home-network-monitor)
— releases there are what the update check (below) looks at.

## Updates

The dashboard shows a small **"Update available"** pill (top of the page,
next to the timestamp) and a banner with the release notes when a newer
release exists on GitHub. The generating machine checks at most **once a
day**, remembers the answer in `data/update_check.json`, and never lets a
failed check affect the dashboard — offline, it just tries again
tomorrow. This is the only network call the toolkit makes apart from its
actual monitoring targets (installing an update downloads one zip from
GitHub, and nothing else); turn the check off by adding
`"update_check": false` to `config.json` (or unticking it in Settings).

Three ways to install an update, easiest first:

1. **One click** — on the monitor PC, open
   `http://localhost:8080/settings` → General → **Updates** → *Update
   now*. It downloads the release, keeps the old version in
   `data/backup/`, swaps the code, and the services restart themselves
   (the dashboard pauses for a minute or so). This deliberately only
   works from the monitor PC itself, like all settings.
2. **Double-click** — run `update-windows.bat` (Windows) or
   `bash update.sh` (macOS/Linux) in the install folder. Same engine,
   progress printed to the window, asks before installing.
3. **By hand** — download the release zip and copy its files over your
   old ones, then restart the services (`bash setup.sh` /
   `setup-windows.bat` re-registers them safely).

Whichever way you choose, your configs, database, and logs are never
touched — the updater refuses to write them even if a zip contained
them. Every replaced file must compile before the update counts; if
anything is wrong it rolls itself back. To go back on purpose, run
`python update.py --rollback` in the install folder — the previous
version is restored from `data/backup/` and the services restart onto it.

## Troubleshooting

If an install misbehaves, run:

```
python diagnose.py
```

It writes `netmon-diagnostics-<timestamp>.zip` containing a status report
(versions, service state, database health, recent events), the logs, and
your config files — one file to send to whoever is helping you.
**Review it first**: it contains your device names, router IPs/MACs, and
file paths.

## Checking it's running

```
launchctl list | grep netmon
```
You should see the `...netmon.monitor`, `...netmon.dashboard`, and
`...netmon.web` services listed (on Windows:
`Get-ScheduledTask -TaskName 'NetMon*'`). Logs are in the `logs/` folder
if something looks wrong — and `python diagnose.py` bundles everything
relevant (see Troubleshooting).

## Stopping it

```
bash uninstall.sh
```
This removes the background services but keeps your collected data and
dashboard.

## Files

| File | Purpose |
|---|---|
| `monitor.py` | The always-on background monitor (ping/wifi/devices/speedtest → SQLite) |
| `dashboard.py` | Reads the database, writes `dashboard.html` |
| `dashboard.html` | The dashboard itself — open this in a browser |
| `serve.py` | Serves the dashboard to your network on port 8080, plus the localhost-only wizard/settings |
| `settings_api.py` / `settings_page.py` | The wizard and settings pages behind `serve.py` |
| `data/network_monitor.db` | SQLite database of everything collected |
| `routers.json` | Optional list of extra routers/access points to monitor by name (with optional floor) |
| `devices.json` | Optional MAC → name mapping for the device table and new-device alerts; entries can also carry an IoT `type` and `watch` flag (see "IoT devices" above) |
| `config.json` | Optional house/dashboard settings: title, floors, thresholds, plan speeds |
| `scan_routers.py` | One-off diagnostic: scans your LAN for devices with a web admin port open, to help you find router IPs |
| `diagnose.py` | Builds a diagnostics zip for remote troubleshooting |
| `version.py` | The toolkit's version number (what the update check compares against) |
| `setup.sh` / `uninstall.sh` | Install/remove the background services (macOS/Linux) |
| `setup.ps1` / `uninstall.ps1` | The same for Windows (Task Scheduler); run via the `.bat` files |
| `setup-windows.bat` / `uninstall-windows.bat` | Double-clickable launchers for the PowerShell scripts |
| `*.plist` | macOS launchd service definitions (templates; `setup.sh` fills in the real path) |
| `vendor/` | Pinned local copy of the chart library, so charts render while your internet is down |
| `logs/` | stdout/stderr logs from the background services, for troubleshooting |

## Moving this to a Raspberry Pi or home server later

### Docker (recommended for any Linux box)

A `Dockerfile` and a reference `docker-compose.yml` ship with the project.
The container bundles everything the Linux code paths shell out to
(`iputils-ping`, `net-tools` for `arp`, `iproute2`, `traceroute`, `nmap`,
and the Ookla speedtest CLI), runs all three services (collector, 60-second
dashboard renderer, web server) under one supervisor, and keeps **all**
mutable state — configs, the SQLite database, logs, the generated HTML —
in a single mounted `app/` directory that survives rebuilds.

```
mkdir -p ~/docker/network-monitor/app
cd ~/docker/network-monitor
git clone https://github.com/MFBALGO/home-network-monitor.git src
cp src/docker-compose.yml .
docker compose build
docker compose up -d
```

Things to know:

- **Host networking is required** (`network_mode: host`, already set in the
  compose file). The monitor reads the ARP table, ping-sweeps your /24 and
  discovers routers at layer 2 — none of which can see your real LAN from
  behind Docker's bridge NAT. The only capability it needs beyond the
  defaults is `CAP_NET_RAW` (ICMP ping + nmap's ARP scan), which is in
  Docker's default set — it's declared in the compose file as documentation.
  No `--privileged` needed.
- Because of host networking, the listen port comes solely from the
  `NETMON_WEB_PORT` environment variable (the reference file uses 8090);
  `ports:` mappings are ignored.
- The **setup wizard and settings pages are localhost-only** by design. On a
  headless server, reach them through an SSH tunnel:
  `ssh -L 8090:127.0.0.1:8090 you@server` then browse
  `http://localhost:8090/setup`. Alternatively pre-place your
  `config.json` / `routers.json` / `devices.json` into `app/` before the
  first start. Or — if you're comfortable with every device on your LAN
  being able to read and change the monitor's config — set
  `NETMON_ADMIN_LAN: "1"` in the compose file (there's a commented line
  ready): the wizard, settings and their API then answer to any
  private-IP device, and the dashboard shows its Settings button to
  those devices. The anti-rebinding/CSRF guards stay active either way.
- **Updates happen by rebuilding the image** (`git pull` in `src/`, then
  `docker compose build && docker compose up -d`). The in-app "Update now"
  button is a no-op in the container: the entrypoint re-seeds the image's
  code over `app/` on every start, precisely so the running code always
  matches the image.
- Linux differences vs a Windows/macOS install: no Wi-Fi chart and no
  desktop toast notifications (use webhook or email alerts instead), and
  silent (ARP-only) routers are detected as down more slowly — the Linux
  ARP check is presence-only, so a dead silent router can linger "online"
  for up to ~20 minutes of cache decay.

### Without Docker

The scripts have no Mac-specific dependency except two things:
`system_profiler` (Wi-Fi signal) and macOS's `ping`/`route` flags. To move
to a Raspberry Pi (or any always-on Linux box) later:

1. Copy `monitor.py`, `dashboard.py`, and the `data/` folder to the Pi.
2. Install the tools the Linux branch shells out to (Debian/Ubuntu):
   `sudo apt install iputils-ping net-tools iproute2 traceroute nmap`
   (nmap is optional but gives much better device scans; the Ookla
   speedtest CLI is optional too — packages.ookla.com has the apt repo).
3. The ping/arp code already has a Linux branch (`IS_MACOS` check), so it
   works as-is — you'll just lose the Wi-Fi-signal chart unless you swap in
   a Linux equivalent (e.g. `iwconfig`/`iw dev wlan0 link`), which is a
   small, contained change in `get_wifi_snapshot()`.
4. Replace `setup.sh`'s launchd services with a `systemd` unit or a `cron
   @reboot` line running `python3 monitor.py`.
5. A Pi (or any device that's always plugged in, unlike a laptop that
   sleeps) also means monitoring doesn't stop whenever your Mac goes to
   sleep or leaves the house — worth doing once you've validated the setup
   works the way you want.

Ask me any time you're ready to do this migration and I can generate the
Pi-specific setup script and systemd unit files.

## Limitations to know about

- **Per-device Wi-Fi signal** (e.g. "is the PS5 in the living room getting
  bad signal") isn't available without your router's admin API, which
  varies by brand/model. What you get here is signal strength for this Mac
  only, plus a list of what's connected. If you tell me your router model,
  I can look into whether it exposes an API or a way to pull richer
  per-device stats.
- **Monitoring only runs while this Mac is awake** (not asleep/shut down).
  Sleep periods are shown honestly — as "Monitoring paused" events and
  chart breaks — but they're still blind spots: an outage during sleep is
  invisible. This is the main reason to eventually move to an always-on Pi.
- The device list actively ping-sweeps the subnet each scan, so it catches
  most devices — but anything with a firewall that ignores ping (e.g. a Mac
  with "stealth mode" on, many phones on battery) can still be missed. It's
  a strong inventory, not a guaranteed one.
