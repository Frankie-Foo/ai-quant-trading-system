$ErrorActionPreference = "Stop"

function Register-ObservationTask {
    param(
        [Parameter(Mandatory = $true)][string]$TaskName,
        [Parameter(Mandatory = $true)][string]$Runner,
        [Parameter(Mandatory = $true)][int]$IntervalMinutes,
        [Parameter(Mandatory = $true)][int]$ExecutionHours,
        [string]$DailyAt,
        [Parameter(Mandatory = $true)][string]$Description
    )

    $action = New-ScheduledTaskAction `
        -Execute "powershell.exe" `
        -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$Runner`""
    $trigger = if ($DailyAt) {
        New-ScheduledTaskTrigger -Daily -At $DailyAt
    } else {
        New-ScheduledTaskTrigger `
            -Once `
            -At ((Get-Date).AddMinutes(1)) `
            -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
            -RepetitionDuration (New-TimeSpan -Days 3650)
    }
    $settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -WakeToRun `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Hours $ExecutionHours)

    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Description $Description `
        -Force | Out-Null
}

Register-ObservationTask `
    -TaskName "Trading System V2 - Premarket" `
    -Runner (Join-Path $PSScriptRoot "run_premarket_tick.ps1") `
    -IntervalMinutes 5 `
    -ExecutionHours 12 `
    -DailyAt "20:00" `
    -Description "Idempotent point-in-time data refresh and locked selection."

if (Get-ScheduledTask -TaskName "Trading System V2 - Paper Session" -ErrorAction SilentlyContinue) {
    Disable-ScheduledTask -TaskName "Trading System V2 - Paper Session" | Out-Null
}

Register-ObservationTask `
    -TaskName "Trading System V2 - Postmarket Review" `
    -Runner (Join-Path $PSScriptRoot "run_postmarket_tick.ps1") `
    -IntervalMinutes 30 `
    -ExecutionHours 2 `
    -Description "Idempotent postmarket replay, episode build, and governed review."
