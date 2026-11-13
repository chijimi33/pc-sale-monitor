param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('install', 'uninstall', 'status')]
    [string]$Action
)

$ErrorActionPreference = 'Stop'
$taskName = 'PCSaleMonitorSatellite'
$repository = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
$pythonPath = Join-Path $repository '.venv\Scripts\pythonw.exe'

switch ($Action) {
    'install' {
        if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
            throw "Missing virtual environment interpreter: $pythonPath"
        }
        $userId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
        $taskAction = New-ScheduledTaskAction -Execute $pythonPath `
            -Argument '-m sale_monitor.satellite run' -WorkingDirectory $repository
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $userId
        $principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited
        $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
            -RestartInterval (New-TimeSpan -Minutes 5) -RestartCount 999 `
            -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
        Register-ScheduledTask -TaskName $taskName -Action $taskAction -Trigger $trigger `
            -Principal $principal -Settings $settings -Force
    }
    'uninstall' {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    }
    'status' {
        Get-ScheduledTask -TaskName $taskName
        Get-ScheduledTaskInfo -TaskName $taskName
    }
}
