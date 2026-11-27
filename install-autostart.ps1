<#
    Registers the Last Hunt watcher as a Windows Scheduled Task so it starts
    automatically at logon and restarts itself if it ever crashes.

    Run from PowerShell:
        powershell -ExecutionPolicy Bypass -File .\install-autostart.ps1

    Remove it again with:
        powershell -ExecutionPolicy Bypass -File .\uninstall-autostart.ps1
#>

$ErrorActionPreference = 'Stop'

$TaskName = 'TLHWatcher'
$Here     = Split-Path -Parent $MyInvocation.MyCommand.Path
$Script   = Join-Path $Here 'tlhwatch.py'

if (-not (Test-Path $Script)) {
    throw "Cannot find tlhwatch.py next to this script ($Script)."
}

# pythonw.exe runs with no console window, so the watcher stays out of the way.
$PyDir   = Split-Path -Parent (& py -3 -c "import sys; print(sys.executable)")
$Pythonw = Join-Path $PyDir 'pythonw.exe'
if (-not (Test-Path $Pythonw)) {
    throw "Could not locate pythonw.exe (looked in $PyDir)."
}

Write-Host "Python   : $Pythonw"
Write-Host "Watcher  : $Script"

$action = New-ScheduledTaskAction -Execute $Pythonw `
                                  -Argument "`"$Script`"" `
                                  -WorkingDirectory $Here

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

# Wake the machine is deliberately NOT enabled: a sleeping PC cannot watch
# anything, so see the README for the always-on options.

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Write-Host "Replacing the existing '$TaskName' task..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask -TaskName $TaskName `
                       -Action $action `
                       -Trigger $trigger `
                       -Settings $settings `
                       -Description 'Watches thelasthunt.com and pushes a phone alert when a watched product is buyable.' | Out-Null

Start-ScheduledTask -TaskName $TaskName

Write-Host ''
Write-Host "Installed and started. The watcher now runs at every logon." -ForegroundColor Green
Write-Host "Check it with : Get-ScheduledTask -TaskName $TaskName"
Write-Host "Live log      : Get-Content '$(Join-Path $Here 'watch.log')' -Tail 20 -Wait"
