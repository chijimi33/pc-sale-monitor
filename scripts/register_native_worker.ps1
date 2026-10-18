param(
    [Parameter(Mandatory = $true)][string]$Root,
    [string]$TaskName = 'PCSaleMonitor-Native-Yahoo'
)
$ErrorActionPreference = 'Stop'
$installationRoot = (Resolve-Path -LiteralPath $Root).Path
$installationPath = Join-Path $installationRoot 'installation.json'
$installation = Get-Content -LiteralPath $installationPath -Raw | ConvertFrom-Json
if ([IO.Path]::GetFullPath($installation.root) -ne $installationRoot) { throw 'Installation root mismatch' }
$runner = Join-Path $installation.source 'scripts\native_worker_runner.py'
& $installation.python -B $runner --config $installationPath --verify-only
if ($LASTEXITCODE -ne 0) { throw 'Installation verification failed' }
$pythonWindowless = Join-Path (Split-Path -Parent $installation.python) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonWindowless -PathType Leaf)) { throw 'Windowless Python missing' }
$managedDescription = 'pc-sale-monitor-native-worker: fixed source, two-minute mailbox polling; collection remains every four hours.'
$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existingTask) {
    if ($existingTask.Description -ne $managedDescription -or $existingTask.Actions.Count -ne 1 -or
        $existingTask.Actions[0].Execute -ne $pythonWindowless -or $existingTask.State -eq 'Running') {
        throw 'Existing task is unrelated or running; refusing to replace it'
    }
    $backup = Join-Path $installationRoot ('task-before-' + (Get-Date -Format 'yyyyMMddTHHmmssfff') + '.xml')
    Export-ScheduledTask -TaskName $TaskName | Set-Content -LiteralPath $backup -Encoding utf8
}
$arguments = '-B "' + $runner + '" --config "' + $installationPath + '"'
$action = New-ScheduledTaskAction -Execute $pythonWindowless -Argument $arguments -WorkingDirectory $installation.source
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 2)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 50) `
    -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
$definition = New-ScheduledTask -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description $managedDescription
if ($existingTask) {
    Register-ScheduledTask -TaskName $TaskName -InputObject $definition -Force | Out-Null
} else {
    Register-ScheduledTask -TaskName $TaskName -InputObject $definition | Out-Null
}
[xml]$saved = Export-ScheduledTask -TaskName $TaskName
if ($saved.Task.Actions.Exec.Command -ne $pythonWindowless -or $saved.Task.Actions.Exec.Arguments -ne $arguments -or
    $saved.Task.Actions.Exec.WorkingDirectory -ne $installation.source -or
    $saved.Task.Settings.MultipleInstancesPolicy -ne 'IgnoreNew' -or
    $saved.Task.Triggers.TimeTrigger.Repetition.Interval -ne 'PT2M' -or
    $saved.Task.Principals.Principal.LogonType -ne 'InteractiveToken') { throw 'Scheduled task readback mismatch' }
$saved.Save((Join-Path $installationRoot 'registered-task.xml'))
[pscustomobject]@{ task = $TaskName; status = 'registered_and_read_back'; root = $installationRoot; interval = 'PT2M'; model_started = $false } | ConvertTo-Json -Compress
