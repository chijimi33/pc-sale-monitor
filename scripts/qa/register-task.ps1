param([switch]$Disable)
$ErrorActionPreference = 'Stop'
$qaRoot = 'E:\Codex\pc-sale-monitor-qa'
$taskName = 'PCSaleMonitor-Qwen-QA'
if ($Disable) {
    Disable-ScheduledTask -TaskName $taskName | Out-Null
    exit
}
if (-not (Test-Path -LiteralPath "$qaRoot\model-review.json")) { throw 'Complete the three-model comparison and Codex review first.' }
$qaConfig = Get-Content -LiteralPath "$qaRoot\config.json" -Raw | ConvertFrom-Json
if (-not $qaConfig.enabled -or -not $qaConfig.selected_model) { throw 'QA worker is not enabled.' }
$workerPath = "$qaRoot\runtime-code\qa\worker.py"
$pythonWindowless = Join-Path (Split-Path $qaConfig.python) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonWindowless)) { throw 'pythonw.exe is required for background execution.' }
$action = New-ScheduledTaskAction -Execute $pythonWindowless -Argument ('-B -X utf8 "' + $workerPath + '"') -WorkingDirectory $qaRoot
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 15)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description 'Qwen validates new sale-monitor snapshots. Artifacts E: only; deployment requires Codex review.' -Force | Out-Null
Get-ScheduledTask -TaskName $taskName | Select-Object TaskName,State
