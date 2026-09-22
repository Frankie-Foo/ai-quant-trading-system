param(
    [Parameter(Mandatory = $true)][string]$PythonPath,
    [Parameter(Mandatory = $true)][string]$EnvironmentFile,
    [Parameter(Mandatory = $true)][string]$DataRoot,
    [Parameter(Mandatory = $true)][string]$ActivePolicyFile,
    [Parameter(Mandatory = $true)][string]$ChallengerPolicyFile,
    [Parameter(Mandatory = $true)][string]$RuntimeStateRoot,
    [switch]$ArmPaper,
    [decimal]$PaperSmokeMaxNotional = 100.0
)

$ErrorActionPreference = "Stop"
$PythonPath = (Resolve-Path -LiteralPath $PythonPath).Path
$EnvironmentFile = (Resolve-Path -LiteralPath $EnvironmentFile).Path
$DataRoot = (Resolve-Path -LiteralPath $DataRoot).Path
$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location -LiteralPath $repositoryRoot
$smokeCap = $PaperSmokeMaxNotional.ToString([Globalization.CultureInfo]::InvariantCulture)
& $PythonPath -m operations.paper_release --validate-cap $smokeCap | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Invalid Paper portfolio notional cap; owner-approved release validation failed."
}

function Assert-QuiescedDeployment {
    $startup = [Environment]::GetFolderPath("Startup")
    $supervisorLink = Join-Path $startup "Trading System V2 - Local Observation Supervisor.lnk"
    if (Test-Path -LiteralPath $supervisorLink) {
        throw "Disable and preserve the legacy supervisor startup shortcut before cutover."
    }
    $ownedTasks = @(
        "Trading System V2 - AI Quant Funnel", "Trading System V2 - Premarket",
        "Trading System V2 - Paper Session", "Trading System V2 - Postmarket Review",
        "Trading System V2 - Monthly Evolution", "Trading System V2 - Research Cycle"
    )
    foreach ($name in $ownedTasks) {
        $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        if ($task -and ($task.Settings.Enabled -or $task.State -eq "Running")) {
            throw "Disable and drain the existing task before cutover: $name"
        }
    }
    $writers = Get-CimInstance Win32_Process | Where-Object {
        $_.Name -match '^(pythonw?|powershell|pwsh)(\.exe)?$' -and
        $_.CommandLine -match '(schedule\.(supervisor|modern_funnel|premarket|postmarket|monthly_evolution|research_cycle)|scripts\.(monitor_modern_momentum_paper|run_modern_funnel_stage)|run_local_observation_supervisor\.ps1|run_(modern_funnel|premarket|postmarket|monthly_evolution|research_cycle)_tick\.ps1)'
    }
    if ($writers) {
        throw "Existing scheduler or Paper process remains active; reconcile and drain before cutover."
    }
}

Assert-QuiescedDeployment
$activePolicy = (Resolve-Path -LiteralPath $ActivePolicyFile).Path
$challengerPolicy = [IO.Path]::GetFullPath($ChallengerPolicyFile)
$persistentState = (Resolve-Path -LiteralPath $RuntimeStateRoot).Path
& $PythonPath -m scripts.check_release_binding `
    --release-root $repositoryRoot --state-root $persistentState --active-policy $activePolicy
if ($LASTEXITCODE -ne 0) {
    throw "Persistent state or approved policy binding failed; no tasks were changed."
}

function Set-DailyRepeatingWindow {
    param(
        [Parameter(Mandatory = $true)][string]$TaskName,
        [Parameter(Mandatory = $true)][int]$IntervalMinutes,
        [Parameter(Mandatory = $true)][TimeSpan]$Duration,
        [string]$StartAt = "20:00"
    )

    $doc = [xml](Export-ScheduledTask -TaskName $TaskName)
    $namespace = "http://schemas.microsoft.com/windows/2004/02/mit/task"
    $manager = New-Object System.Xml.XmlNamespaceManager($doc.NameTable)
    $manager.AddNamespace("t", $namespace)
    $triggers = $doc.SelectSingleNode("//t:Triggers", $manager)
    $triggers.RemoveAll()

    $trigger = $doc.CreateElement("CalendarTrigger", $namespace)
    $start = $doc.CreateElement("StartBoundary", $namespace)
    $start.InnerText = ((Get-Date).Date.Add([TimeSpan]::Parse($StartAt))).ToString("yyyy-MM-ddTHH:mm:sszzz")
    $trigger.AppendChild($start) | Out-Null
    $enabled = $doc.CreateElement("Enabled", $namespace)
    $enabled.InnerText = "true"
    $trigger.AppendChild($enabled) | Out-Null

    $daily = $doc.CreateElement("ScheduleByDay", $namespace)
    $days = $doc.CreateElement("DaysInterval", $namespace)
    $days.InnerText = "1"
    $daily.AppendChild($days) | Out-Null
    $trigger.AppendChild($daily) | Out-Null

    $repeat = $doc.CreateElement("Repetition", $namespace)
    $interval = $doc.CreateElement("Interval", $namespace)
    $interval.InnerText = "PT{0}M" -f $IntervalMinutes
    $repeat.AppendChild($interval) | Out-Null
    $durationText = "PT{0}H{1}M" -f [math]::Floor($Duration.TotalHours), $Duration.Minutes
    $durationNode = $doc.CreateElement("Duration", $namespace)
    $durationNode.InnerText = $durationText
    $repeat.AppendChild($durationNode) | Out-Null
    $stop = $doc.CreateElement("StopAtDurationEnd", $namespace)
    $stop.InnerText = "true"
    $repeat.AppendChild($stop) | Out-Null
    $trigger.AppendChild($repeat) | Out-Null
    $triggers.AppendChild($trigger) | Out-Null

    $startWhenAvailable = $doc.SelectSingleNode("//t:Settings/t:StartWhenAvailable", $manager)
    if ($null -ne $startWhenAvailable) {
        $startWhenAvailable.InnerText = "false"
    }
    Register-ScheduledTask -TaskName $TaskName -Xml $doc.OuterXml -Force | Out-Null
}

function Register-ObservationTask {
    param(
        [Parameter(Mandatory = $true)][string]$TaskName,
        [Parameter(Mandatory = $true)][string]$Runner,
        [Parameter(Mandatory = $true)][int]$IntervalMinutes,
        [Parameter(Mandatory = $true)][int]$ExecutionHours,
        [Parameter(Mandatory = $true)][string]$RunnerArguments,
        [string]$DailyAt,
        [string[]]$WeeklyOn,
        [string]$WindowStart,
        [TimeSpan]$WindowDuration,
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
        -Disable `
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
    if ($WindowStart -and ($WindowDuration -gt [TimeSpan]::Zero)) {
        Set-DailyRepeatingWindow `
            -TaskName $TaskName `
            -IntervalMinutes $IntervalMinutes `
            -Duration $WindowDuration `
            -StartAt $WindowStart
    }
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
    $funnelArguments += " -ArmPaper -PaperSmokeMaxNotional $smokeCap"
}

Register-ObservationTask `
    -TaskName "Trading System V2 - AI Quant Funnel" `
    -Runner (Join-Path $PSScriptRoot "run_modern_funnel_tick.ps1") `
    -IntervalMinutes 1 `
    -ExecutionHours 1 `
    -RunnerArguments $funnelArguments `
    -WindowStart "20:30" `
    -WindowDuration (New-TimeSpan -Hours 10 -Minutes 30) `
    -Description "Durable ET/XNYS four-checkpoint funnel: 08:30 Top20, 09:00 Top20, 09:30 Top10, 09:35 confirmation; Paper remains fail-closed."

Register-ObservationTask `
    -TaskName "Trading System V2 - Postmarket Review" `
    -Runner (Join-Path $PSScriptRoot "run_postmarket_tick.ps1") `
    -IntervalMinutes 30 `
    -ExecutionHours 2 `
    -RunnerArguments $commonArguments `
    -WindowStart "04:00" `
    -WindowDuration (New-TimeSpan -Hours 3) `
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
