# Home Network Monitor - Windows uninstall.
# Removes the Task Scheduler jobs and the dashboard firewall rule. Your data
# (data\network_monitor.db), logs, and config files are left untouched -
# delete the folder if you want them gone too.
#
# Easiest: double-click uninstall-windows.bat.

$removed = 0
foreach ($name in @("NetMon Monitor", "NetMon Dashboard", "NetMon Web")) {
    $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($task) {
        Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
        Write-Host "Removed task '$name'"
        $removed++
    }
}
if ($removed -eq 0) {
    Write-Host "No NetMon tasks were installed - nothing to remove."
}

# The monitor or web server may still be running from before the tasks were
# removed. They run as pythonw.exe with monitor.py / serve.py on the command
# line - stop those processes (and only those).
try {
    Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe' OR Name = 'python.exe'" |
        Where-Object { $_.CommandLine -match "monitor\.py|serve\.py" } |
        ForEach-Object {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
            Write-Host "Stopped running process (pid $($_.ProcessId))"
        }
} catch {}

# Close the firewall opening again (needs an elevated shell to succeed).
try {
    $rule = Get-NetFirewallRule -DisplayName "NetMon Dashboard (TCP 8080)" -ErrorAction SilentlyContinue
    if ($rule) {
        $rule | Remove-NetFirewallRule -ErrorAction Stop
        Write-Host "Removed firewall rule 'NetMon Dashboard (TCP 8080)'"
    }
} catch {
    Write-Host "Couldn't remove the firewall rule (needs admin). From an elevated PowerShell:"
    Write-Host '  Remove-NetFirewallRule -DisplayName "NetMon Dashboard (TCP 8080)"'
}

Write-Host "Done. Your database, logs, and config files are untouched."
