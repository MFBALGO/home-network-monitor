# Home Network Monitor - Windows setup (Windows 10/11).
#
# Easiest: double-click setup-windows.bat (it runs this file).
# Or from PowerShell:  powershell -NoProfile -ExecutionPolicy Bypass -File setup.ps1
#
# What it does (mirrors setup.sh on macOS):
#   1. finds your Python 3 installation
#   2. downloads the chart library into vendor\ (one-time, so the dashboard
#      works even while your internet is down)
#   3. optionally installs the Ookla speedtest CLI and nmap via winget
#   4. registers two Task Scheduler jobs for the current user:
#        - "NetMon Monitor"   - runs monitor.py continuously from logon,
#                               restarts it if it ever crashes
#        - "NetMon Dashboard" - regenerates dashboard.html every minute
#   5. starts them immediately
#
# Everything stays in this folder - database in data\, logs in logs\.
# Remove it all again with uninstall-windows.bat.

$ErrorActionPreference = "Stop"
$NetmonDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Write-Host "Network monitor folder: $NetmonDir"

New-Item -ItemType Directory -Force -Path "$NetmonDir\data", "$NetmonDir\logs", "$NetmonDir\vendor" | Out-Null

# --- 1. find Python 3 -------------------------------------------------------
# Try the py launcher first (installed by python.org builds), then python on
# PATH. The bare "python" name can be a Microsoft Store stub that just opens
# the Store page - asking it for sys.executable weeds that out, because the
# stub produces no output.
function Find-Python {
    foreach ($cand in @("py", "python", "python3")) {
        $cmd = Get-Command $cand -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        try {
            if ($cand -eq "py") {
                $exe = & $cmd.Source -3 -c "import sys; print(sys.executable)" 2>$null | Select-Object -First 1
            } else {
                $exe = & $cmd.Source -c "import sys; print(sys.executable)" 2>$null | Select-Object -First 1
            }
            if ($exe) { $exe = $exe.Trim() }
            if ($exe -and (Test-Path $exe)) { return $exe }
        } catch {}
    }
    return $null
}

$PythonExe = Find-Python
if (-not $PythonExe) {
    Write-Host ""
    Write-Warning "Python 3 was not found."
    Write-Host "Install it first (either way works):"
    Write-Host "  - https://www.python.org/downloads/  (tick 'Add python.exe to PATH')"
    Write-Host "  - or:  winget install Python.Python.3.12"
    Write-Host "Then run this setup again."
    exit 1
}
Write-Host "Python found: $PythonExe"

# pythonw.exe runs without a console window - that's what the background
# tasks should use so nothing flashes on screen. Fall back to python.exe.
$PythonwExe = Join-Path (Split-Path $PythonExe) "pythonw.exe"
if (-not (Test-Path $PythonwExe)) { $PythonwExe = $PythonExe }

# --- 2. vendor chart libraries ---------------------------------------------
# Local copies matter here: the one time you most want this dashboard (your
# internet is down) is exactly when a CDN copy would fail to load.
function Get-VendorLib {
    param([string]$Dest, [string[]]$Urls)
    if ((Test-Path $Dest) -and ((Get-Item $Dest).Length -gt 10000)) { return }
    foreach ($u in $Urls) {
        $hostname = ([Uri]$u).Host
        Write-Host "Downloading $(Split-Path -Leaf $Dest) from $hostname..."
        try {
            Invoke-WebRequest -Uri $u -OutFile $Dest -TimeoutSec 30 -UseBasicParsing
            if ((Get-Item $Dest -ErrorAction SilentlyContinue).Length -gt 10000) {
                Write-Host "  OK ($((Get-Item $Dest).Length) bytes)"
                return
            }
        } catch {}
        Remove-Item $Dest -ErrorAction SilentlyContinue
    }
    Write-Warning "all sources failed for $(Split-Path -Leaf $Dest) - charts may not render. Re-run setup later to retry."
}

Get-VendorLib "$NetmonDir\vendor\chart.umd.min.js" @(
    "https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js",
    "https://unpkg.com/chart.js@4.4.4/dist/chart.umd.min.js",
    "https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.4/chart.umd.min.js")
Get-VendorLib "$NetmonDir\vendor\chartjs-adapter-date-fns.bundle.min.js" @(
    "https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js",
    "https://unpkg.com/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js",
    "https://cdnjs.cloudflare.com/ajax/libs/chartjs-adapter-date-fns/3.0.0/chartjs-adapter-date-fns.bundle.min.js")

# --- 3. optional tools (speed tests, better device discovery) ---------------
$winget = Get-Command winget -ErrorAction SilentlyContinue
if ($winget) {
    if (-not (Get-Command speedtest -ErrorAction SilentlyContinue)) {
        Write-Host "Installing the official Ookla speedtest CLI via winget (optional, for speed test charts)..."
        try {
            winget install --id Ookla.Speedtest.CLI --accept-source-agreements --accept-package-agreements --silent
            Write-Host "  OK"
        } catch { Write-Host "  (skipped - you can install it later; see README)" }
    }
    if (-not (Get-Command nmap -ErrorAction SilentlyContinue)) {
        Write-Host "Installing nmap via winget (optional, improves device discovery; may ask for admin approval)..."
        try {
            winget install --id Insecure.Nmap --accept-source-agreements --accept-package-agreements --silent
            Write-Host "  OK"
        } catch { Write-Host "  (skipped - the monitor will use its built-in ping sweep)" }
    }
} else {
    Write-Host "winget not found - skipping optional speedtest CLI / nmap installs (see README to add them later)."
}

# --- 4. Task Scheduler jobs --------------------------------------------------
$MonitorTask = "NetMon Monitor"
$DashboardTask = "NetMon Dashboard"
$WebTask = "NetMon Web"

# Remove any previous versions of these tasks so two monitors never run at once.
foreach ($name in @($MonitorTask, $DashboardTask, $WebTask)) {
    $existing = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($existing) {
        Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
        Write-Host "Removed old task '$name'"
    }
}

# Monitor: start at logon, keep running forever, restart on crash.
$monAction = New-ScheduledTaskAction -Execute $PythonwExe -Argument "`"$NetmonDir\monitor.py`"" -WorkingDirectory $NetmonDir
$monTrigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$monSettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan)   # zero = no time limit
# If nmap is installed, its Npcap driver is often in "Admin-only mode" - a
# non-elevated monitor then fires a UAC prompt on EVERY 5-minute device sweep.
# Registering the task with highest privileges avoids that, but can only be
# done from an elevated shell, so do it when we can and hint when we can't.
$IsAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if ($IsAdmin) {
    $monPrincipal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest
    Register-ScheduledTask -TaskName $MonitorTask -Action $monAction -Trigger $monTrigger -Settings $monSettings `
        -Principal $monPrincipal `
        -Description "Home network monitor - logs pings/outages/devices to a local SQLite DB in $NetmonDir" | Out-Null
    Write-Host "Installed task '$MonitorTask' (elevated - keeps nmap's Npcap driver from prompting)"
} else {
    Register-ScheduledTask -TaskName $MonitorTask -Action $monAction -Trigger $monTrigger -Settings $monSettings `
        -Description "Home network monitor - logs pings/outages/devices to a local SQLite DB in $NetmonDir" | Out-Null
    Write-Host "Installed task '$MonitorTask'"
    if (Get-Command nmap -ErrorAction SilentlyContinue) {
        Write-Host "  Note: if you see UAC prompts every few minutes (Npcap admin-only mode),"
        Write-Host "  run this from an elevated PowerShell to silence them permanently:"
        Write-Host '    $p = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest'
        Write-Host "    Set-ScheduledTask -TaskName 'NetMon Monitor' -Principal `$p"
    }
}

# Dashboard: regenerate dashboard.html every minute, forever.
$dashAction = New-ScheduledTaskAction -Execute $PythonwExe -Argument "`"$NetmonDir\dashboard.py`"" -WorkingDirectory $NetmonDir
# Note: omit -RepetitionDuration for "repeat indefinitely" - on Windows 10/11,
# [TimeSpan]::MaxValue serializes to XML that Task Scheduler rejects (0x80041318).
$dashTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 1)
$dashSettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName $DashboardTask -Action $dashAction -Trigger $dashTrigger -Settings $dashSettings `
    -Description "Home network monitor - regenerates dashboard.html every minute" | Out-Null
Write-Host "Installed task '$DashboardTask'"

# Web: serve dashboard.html to the rest of the house on TCP 8080 (see serve.py).
if (Test-Path "$NetmonDir\serve.py") {
    $webAction = New-ScheduledTaskAction -Execute $PythonwExe -Argument "`"$NetmonDir\serve.py`"" -WorkingDirectory $NetmonDir
    $webTrigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $webSettings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit (New-TimeSpan)
    Register-ScheduledTask -TaskName $WebTask -Action $webAction -Trigger $webTrigger -Settings $webSettings `
        -Description "Home network monitor - serves dashboard.html on TCP 8080 to the local network" | Out-Null
    Write-Host "Installed task '$WebTask'"

    # Firewall: let other devices in, but only on Private (home) networks.
    if ($IsAdmin) {
        if (-not (Get-NetFirewallRule -DisplayName "NetMon Dashboard (TCP 8080)" -ErrorAction SilentlyContinue)) {
            New-NetFirewallRule -DisplayName "NetMon Dashboard (TCP 8080)" -Direction Inbound -Action Allow `
                -Protocol TCP -LocalPort 8080 -Profile Private | Out-Null
            Write-Host "Opened firewall: TCP 8080, Private networks only"
        }
    } else {
        Write-Host "Not elevated - couldn't open the firewall, so only THIS machine can see the"
        Write-Host "web dashboard. To let the rest of the house in, run this from an admin PowerShell:"
        Write-Host '  New-NetFirewallRule -DisplayName "NetMon Dashboard (TCP 8080)" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8080 -Profile Private'
    }
    Start-ScheduledTask -TaskName $WebTask
} else {
    Write-Host "serve.py not found - skipping the LAN web server task"
}

# --- 5. start both now -------------------------------------------------------
Start-ScheduledTask -TaskName $MonitorTask
Start-ScheduledTask -TaskName $DashboardTask

Write-Host ""
Write-Host "Done. The monitor is now running in the background and will start"
Write-Host "automatically every time you log in (and restart itself if it crashes)."
Write-Host ""
Write-Host "Dashboard file: $NetmonDir\dashboard.html  (regenerates every minute)"
Write-Host "Open it once now - it'll be mostly empty until data accumulates."
$LanIp = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" } |
    Select-Object -First 1).IPAddress
if ($LanIp -and (Test-Path "$NetmonDir\serve.py")) {
    Write-Host "House-wide dashboard:  http://${LanIp}:8080/  (any device on your network)"
}
Write-Host ""
Write-Host "To check on the tasks: open 'Task Scheduler' and look for '$MonitorTask'"
Write-Host "and '$DashboardTask', or run:  Get-ScheduledTask -TaskName 'NetMon*'"
Write-Host "Logs: $NetmonDir\logs\"
Write-Host "To stop everything: double-click uninstall-windows.bat"
