<#
    Removes the TLHWatcher scheduled task created by install-autostart.ps1.

        powershell -ExecutionPolicy Bypass -File .\uninstall-autostart.ps1
#>

$ErrorActionPreference = 'Stop'
$TaskName = 'TLHWatcher'

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask  -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed the '$TaskName' scheduled task." -ForegroundColor Green
} else {
    Write-Host "No '$TaskName' scheduled task was installed."
}
