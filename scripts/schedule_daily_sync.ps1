# scripts/schedule_daily_sync.ps1 - Windows Task Scheduler Installer for tradeBotTiuku
param(
    [string]$TaskName = "tradeBotTiuku_DailySync",
    [string]$Time = "23:05",
    [switch]$Uninstall
)

$ProjectRoot = (Resolve-Path "$PSScriptRoot\..").Path
$BatchScript = "$ProjectRoot\scripts\run_daily_sync.bat"

if ($Uninstall) {
    Write-Host "Unregistering scheduled task '$TaskName'..." -ForegroundColor Yellow
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Task '$TaskName' removed successfully." -ForegroundColor Green
    exit 0
}

if (-not (Test-Path $BatchScript)) {
    Write-Error "Batch script not found: $BatchScript"
    exit 1
}

Write-Host "Configuring Windows Task Scheduler for tradeBotTiuku Daily Sync..." -ForegroundColor Cyan
Write-Host "Project Root: $ProjectRoot"
Write-Host "Script:       $BatchScript"
Write-Host "Run Time:     $Time"

$Action = New-ScheduledTaskAction -Execute $BatchScript -WorkingDirectory $ProjectRoot
$Trigger = New-ScheduledTaskTrigger -Daily -At $Time
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable

try {
    Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Description "tradeBotTiuku Daily Walk-Forward Sync across all 12 portfolios" -Force | Out-Null
    Write-Host "Scheduled Task '$TaskName' registered successfully!" -ForegroundColor Green
    Write-Host "All 12 portfolios (P1 to P12) will now automatically sync daily at $Time." -ForegroundColor Green
} catch {
    Write-Warning "Failed to register scheduled task: $_"
    Write-Host "You can run the sync manually anytime via: scripts\run_daily_sync.bat" -ForegroundColor Yellow
}
