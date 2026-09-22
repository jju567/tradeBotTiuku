# start_all.ps1 - Launch tradeBotTiuku UI and Paper Trader Daemon
param(
    [double]$IntervalHours = 4.0,
    [switch]$UiOnly,
    [switch]$DaemonOnly,
    [switch]$RunOnce,
    [int]$Port = 8502
)

$argsList = @()
if ($UiOnly) { $argsList += "--ui-only" }
if ($DaemonOnly) { $argsList += "--daemon-only" }
if ($RunOnce) { $argsList += "--run-once" }
if ($Port -ne 8502) { $argsList += "--port", $Port }
if ($IntervalHours -ne 4.0) { $argsList += "--interval-hours", $IntervalHours }

python start_all.py @argsList
