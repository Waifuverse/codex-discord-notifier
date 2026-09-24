param([ValidateSet('install','start','stop','status','uninstall')][string]$Action = 'status')
$ErrorActionPreference = 'Stop'
$taskName = 'Codex Discord Reply Bridge'
$watchdogName = 'Codex Discord Bridge Health'
$bridgeRoot = $PSScriptRoot
$pythonPath = Join-Path $bridgeRoot '.venv\Scripts\pythonw.exe'
$bridgePath = Join-Path $bridgeRoot 'reply_bridge.py'
switch ($Action) {
    'install' {
        if (!(Test-Path -LiteralPath $pythonPath)) { throw 'Create .venv and install requirements.txt first.' }
        $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
        $taskAction = New-ScheduledTaskAction -Execute $pythonPath -Argument ('"' + $bridgePath + '"') -WorkingDirectory $bridgeRoot
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
        $principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
        $settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
        Register-ScheduledTask -TaskName $taskName -Action $taskAction -Trigger $trigger -Principal $principal -Settings $settings -Description 'Delivers Discord replies directly to their original Codex tasks, steering active work. Runs after user sign-in.' -Force | Out-Null
        $watchdogAction = New-ScheduledTaskAction -Execute $pythonPath -Argument ('"' + (Join-Path $bridgeRoot 'health_watchdog.py') + '"') -WorkingDirectory $bridgeRoot
        Register-ScheduledTask -TaskName $watchdogName -Action $watchdogAction -Trigger $trigger -Principal $principal -Settings $settings -Description 'Independent Discord bridge health alerts after sign-in.' -Force | Out-Null
        Start-ScheduledTask -TaskName $taskName
        Start-ScheduledTask -TaskName $watchdogName
        Write-Output 'Installed and started. Automatically starts at Windows sign-in; stays running while the session is locked.'
    }
    'start' { Start-ScheduledTask -TaskName $taskName }
    'stop' {
        $bridgeProcesses = Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'pythonw.exe' -and $_.CommandLine -and $_.CommandLine.Contains($bridgePath) }
        Stop-ScheduledTask -TaskName $taskName
        foreach ($bridgeProcess in $bridgeProcesses) {
            Wait-Process -Id $bridgeProcess.ProcessId -Timeout 15 -ErrorAction SilentlyContinue
        }
    }
    'uninstall' {
        Stop-ScheduledTask -TaskName $watchdogName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $watchdogName -Confirm:$false -ErrorAction SilentlyContinue
        Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        Write-Output 'Startup task removed. Notification hook and saved replies are retained.'
    }
    'status' {
        Get-ScheduledTask -TaskName $watchdogName -ErrorAction SilentlyContinue | Select-Object TaskName,State | Format-List
        Get-ScheduledTask -TaskName $taskName | Select-Object TaskName,State | Format-List
        Get-ScheduledTaskInfo -TaskName $taskName | Select-Object LastRunTime,LastTaskResult,NextRunTime | Format-List
        Push-Location -LiteralPath $bridgeRoot
        try {
            & (Join-Path $bridgeRoot '.venv\Scripts\python.exe') (Join-Path $bridgeRoot 'health_watchdog.py') --once
            & (Join-Path $bridgeRoot '.venv\Scripts\python.exe') -c 'from bridge_store import Store; s=Store(); print({k:s.get(k) for k in ("connected","heartbeat","pid")}); print({state:len(s.rows((state,))) for state in ("pending","dispatching","submitted","uncertain","completed")})'
        } finally { Pop-Location }
    }
}
