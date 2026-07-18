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
   This registers two background services with macOS (`launchd`) so they
   run continuously — including after you restart your Mac — without
   needing Terminal or this chat open:
   - `com.<your username>.netmon.monitor` — the always-on pinger/logger
   - `com.<your username>.netmon.dashboard` — regenerates `dashboard.html`
     every minute

   (The service names are derived from whoever runs `setup.sh`, so this
   folder works on anyone's Mac without editing anything.)

That's it. Give it a few minutes to collect data, then open `dashboard.html`
in your browser (double-click it, or `open dashboard.html` in Terminal).
Refresh the page whenever you want the latest data — it rewrites itself
automatically every minute, you just need to reload the tab.

### Setting it up on Windows (10/11)

The same folder works on Windows — the monitor knows the Windows versions
of every command it uses (`ping -n`, `arp -a`, `netsh wlan`, `route print`).

1. Install **Python 3** if you don't have it: from
   [python.org/downloads](https://www.python.org/downloads/) (tick *"Add
   python.exe to PATH"*), or in PowerShell: `winget install Python.Python.3.12`
2. Rename/edit the three `.example.json` files, same as on Mac.
3. **Double-click `setup-windows.bat`** (it runs `setup.ps1`).

That registers two Task Scheduler jobs for your user account — *NetMon
Monitor* (runs continuously from logon, auto-restarts if it crashes) and
*NetMon Dashboard* (regenerates `dashboard.html` every minute) — downloads
the chart library, and offers to install the optional speed test CLI and
nmap via winget. Then open `dashboard.html` in your browser, exactly like
on a Mac. To stop everything: double-click `uninstall-windows.bat`.

Windows notes:

- The Wi-Fi chart uses `netsh`, which reports signal as a percentage; the
  monitor converts it to an approximate dBm value (fine for spotting trends,
  not lab-accurate).
- On a non-English Windows, some Wi-Fi fields (channel/rate) may come back
  empty — everything else is parsed language-independently and works.
- Logs go to the same `logs\` folder (`monitor.out.log`, `monitor.err.log`).
- To check on it: open **Task Scheduler** and look for the two *NetMon*
  entries, or in PowerShell: `Get-ScheduledTask -TaskName 'NetMon*'`

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

After editing `routers.json`, restart the monitor so it picks up the
change:
```
launchctl kickstart -k gui/$(id -u)/com.$(id -un).netmon.monitor
```
The dashboard will start showing a **Routers & access points** table (with
each one's live status, 24h uptime, and average latency) on its next
regeneration. No entry is required — leave the file as `[]` if you only
want to monitor your main connection.

### Optional: naming your devices

The device table shows raw hostnames/MACs by default. To give devices
friendly names, edit `devices.json` in this folder — a plain
`{"mac": "name"}` mapping:

```json
{
  "22:ec:06:59:dc:20": "Friend's Mac",
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
setup scripts (macOS *and* Windows), README, and *example* config files,
but none of your personal data (no database, logs, router IPs, device
names, or dashboard). Send the zip to a friend; they unzip it, rename/edit
the three `.example.json` files for their home, and run the setup for
their OS — `bash setup.sh` on a Mac, or a double-click of
`setup-windows.bat` on Windows. (The monitoring code itself also runs on
Linux; see the Raspberry Pi section below for service setup there.)
Nothing in the code is tied to any one person or house.

## Checking it's running

```
launchctl list | grep netmon
```
You should see both the `...netmon.monitor` and `...netmon.dashboard`
services listed. Logs are in the `logs/` folder if something looks wrong.

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
| `data/network_monitor.db` | SQLite database of everything collected |
| `routers.json` | Optional list of extra routers/access points to monitor by name (with optional floor) — edit this directly |
| `devices.json` | Optional MAC → friendly-name mapping for the device table and new-device alerts — edit this directly |
| `scan_routers.py` | One-off diagnostic: scans your LAN for devices with a web admin port open, to help you find router IPs |
| `setup.sh` / `uninstall.sh` | Install/remove the background services (macOS/Linux) |
| `setup.ps1` / `uninstall.ps1` | The same for Windows (Task Scheduler); run via the `.bat` files |
| `setup-windows.bat` / `uninstall-windows.bat` | Double-clickable launchers for the PowerShell scripts |
| `*.plist` | macOS launchd service definitions (templates; `setup.sh` fills in the real path) |
| `logs/` | stdout/stderr logs from the background services, for troubleshooting |

## Moving this to a Raspberry Pi or home server later

The scripts have no Mac-specific dependency except two things:
`system_profiler` (Wi-Fi signal) and macOS's `ping`/`route` flags. To move
to a Raspberry Pi (or any always-on Linux box) later:

1. Copy `monitor.py`, `dashboard.py`, and the `data/` folder to the Pi.
2. The ping/arp code already has a Linux branch (`IS_MACOS` check), so it
   works as-is — you'll just lose the Wi-Fi-signal chart unless you swap in
   a Linux equivalent (e.g. `iwconfig`/`iw dev wlan0 link`), which is a
   small, contained change in `get_wifi_snapshot()`.
2. Replace `setup.sh`'s launchd services with a `systemd` unit or a `cron
   @reboot` line running `python3 monitor.py`.
3. A Pi (or any device that's always plugged in, unlike a laptop that
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
