param(
    [Parameter(Mandatory = $true)][string]$PythonPath,
    [Parameter(Mandatory = $true)][string]$EnvironmentFile,
    [Parameter(Mandatory = $true)][string]$DataRoot,
    [Parameter(Mandatory = $true)][string]$StrategyPolicyApprovedBy,
    [switch]$ArmPaper,
    [ValidateRange(0.01, 100.0)][decimal]$PaperSmokeMaxNotional = 100.0
)

$ErrorActionPreference = "Stop"
$PythonPath = (Resolve-Path -LiteralPath $PythonPath).Path
$EnvironmentFile = (Resolve-Path -LiteralPath $EnvironmentFile).Path
$DataRoot = (Resolve-Path -LiteralPath $DataRoot).Path
$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location -LiteralPath $repositoryRoot

function Register-ObservationTask {
    param(
        [Parameter(Mandatory = $true)][string]$TaskName,
        [Parameter(Mandatory = $true)][string]$Runner,
        [Parameter(Mandatory = $true)][int]$IntervalMinutes,
        [Parameter(Mandatory = $true)][int]$ExecutionHours,
        [Parameter(Mandatory = $true)][string]$RunnerArguments,
        [string]$DailyAt,
        [string[]]$WeeklyOn,
        [Parameter(Mandatory = $true)][string]$Description
    )

    $action = New-ScheduledTaskAction `
        -Execute "powershell.exe" `
        -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Runner`" $RunnerArguments"
    $trigger = if ($WeeklyOn) {
        New-ScheduledTaskTrigger -Weekly -DaysOfWeek $WeeklyOn -At $DailyAt
    } elseif ($DailyAt) {
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

$strategyRoot = Join-Path $repositoryRoot "runs\strategy"
$activePolicy = Join-Path $strategyRoot "active.json"
$challengerPolicy = Join-Path $strategyRoot "challenger.json"
New-Item -ItemType Directory -Force -Path $strategyRoot | Out-Null
& $PythonPath -m scripts.manage_strategy_policy bootstrap `
    --active $activePolicy `
    --approved-by $StrategyPolicyApprovedBy `
    --version "selection-baseline" `
    --min-rvol 3.0 | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Unable to bootstrap the owner-approved active strategy policy."
}

$legacyTasks = @(
    "Trading System V2 - Premarket",
    "Trading System V2 - Paper Session"
)
foreach ($taskName in $legacyTasks) {
    if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
        Disable-ScheduledTask -TaskName $taskName | Out-Null
    }
}

$commonArguments = (
    "-PythonPath `"$PythonPath`" -EnvironmentFile `"$EnvironmentFile`" " +
    "-DataRoot `"$DataRoot`" -ActivePolicyFile `"$activePolicy`" " +
    "-ChallengerPolicyFile `"$challengerPolicy`""
)
$funnelArguments = $commonArguments
if ($ArmPaper) {
    $smokeCap = $PaperSmokeMaxNotional.ToString(
        [Globalization.CultureInfo]::InvariantCulture
    )
    $funnelArguments += " -ArmPaper -PaperSmokeMaxNotional $smokeCap"
}

Register-ObservationTask `
    -TaskName "Trading System V2 - AI Quant Funnel" `
    -Runner (Join-Path $PSScriptRoot "run_modern_funnel_tick.ps1") `
    -IntervalMinutes 1 `
    -ExecutionHours 1 `
    -RunnerArguments $funnelArguments `
    -Description "Durable ET/XNYS three-stage funnel; order execution remains fail-closed."

Register-ObservationTask `
    -TaskName "Trading System V2 - Postmarket Review" `
    -Runner (Join-Path $PSScriptRoot "run_postmarket_tick.ps1") `
    -IntervalMinutes 30 `
    -ExecutionHours 2 `
    -RunnerArguments $commonArguments `
    -Description "Idempotent postmarket replay, episode build, and governed review."

Register-ObservationTask `
    -TaskName "Trading System V2 - Monthly Evolution" `
    -Runner (Join-Path $PSScriptRoot "run_monthly_evolution_tick.ps1") `
    -IntervalMinutes 1 `
    -ExecutionHours 2 `
    -RunnerArguments $commonArguments `
    -DailyAt "08:30" `
    -Description "First-XNYS-session governed proposal, OOS sandbox, and shadow Challenger build."

Register-ObservationTask `
    -TaskName "Trading System V2 - Research Cycle" `
    -Runner (Join-Path $PSScriptRoot "run_research_cycle_tick.ps1") `
    -IntervalMinutes 1 `
    -ExecutionHours 18 `
    -RunnerArguments $commonArguments `
    -DailyAt "10:00" `
    -WeeklyOn Saturday `
    -Description "Weekly point-in-time data refresh and governed OOS research; no orders."
