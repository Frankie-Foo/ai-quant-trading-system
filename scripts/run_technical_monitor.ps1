[CmdletBinding()]
param(
    [string]$Symbol = "RNG",
    [ValidateRange(1, 60)]
    [int]$IntervalMinutes = 15,
    [int]$PositionShares = 45,
    [double]$PositionAverage = 46.50,
    [int]$NewLotShares = 25,
    [double]$NewLotEntry = 46.70,
    [double]$NewLotProtect = 47.20,
    [double]$AllExit = 46.20,
    [int]$AddShares = 25,
    [ValidateRange(1024, 65535)]
    [int]$CloudPort = 8766,
    [switch]$Once
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$OutputsRoot = (Resolve-Path -LiteralPath (Join-Path $RepoRoot "..")).Path
$CloudRoot = (Resolve-Path -LiteralPath (
    Join-Path $OutputsRoot "cloud-strategy-platform"
)).Path
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$CloudPython = Join-Path $CloudRoot ".venv\Scripts\python.exe"
$Runs = Join-Path $RepoRoot "runs"
$JsonLog = Join-Path $Runs "technical-monitor-$($Symbol.ToUpperInvariant()).jsonl"
$LockName = "Local\TradingSystemTechnicalMonitor-$($Symbol.ToUpperInvariant())"
$Eastern = [TimeZoneInfo]::FindSystemTimeZoneById("Eastern Standard Time")
$Invariant = [Globalization.CultureInfo]::InvariantCulture
$OwnedCloudProcess = $null

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "trading-system Python runtime is unavailable"
}
if (-not (Test-Path -LiteralPath $CloudPython -PathType Leaf)) {
    throw "cloud-strategy-platform Python runtime is unavailable"
}
New-Item -ItemType Directory -Force -Path $Runs | Out-Null

$CreatedNew = $false
$Mutex = [Threading.Mutex]::new($true, $LockName, [ref]$CreatedNew)
if (-not $CreatedNew) {
    $Mutex.Dispose()
    throw "a monitor for this symbol is already running"
}

function Test-CloudHealth {
    try {
        $Health = Invoke-RestMethod `
            -Uri "http://127.0.0.1:$CloudPort/health" `
            -TimeoutSec 3
        return $Health.status -eq "ready"
    }
    catch {
        return $false
    }
}

function Assert-OrStartCloudApi {
    $Connection = Get-NetTCPConnection `
        -LocalPort $CloudPort `
        -State Listen `
        -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -ne $Connection) {
        $Owner = Get-CimInstance Win32_Process `
            -Filter "ProcessId=$($Connection.OwningProcess)"
        $IsExpected = (
            $Owner.Name -eq "python.exe" -and
            $Owner.CommandLine -like "*-m scripts.serve_api*" -and
            $Owner.CommandLine -like "*--port $CloudPort*"
        )
        if (-not $IsExpected) {
            throw "cloud port is owned by an unexpected process"
        }
        if (-not (Test-CloudHealth)) {
            throw "existing cloud market-data API is not healthy"
        }
        return
    }

    $CloudOut = Join-Path $CloudRoot "runs\technical-monitor-api.out.log"
    $CloudErr = Join-Path $CloudRoot "runs\technical-monitor-api.err.log"
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $CloudOut) |
        Out-Null
    $script:OwnedCloudProcess = Start-Process `
        -FilePath $CloudPython `
        -ArgumentList @(
            "-m",
            "scripts.serve_api",
            "--host",
            "127.0.0.1",
            "--port",
            $CloudPort.ToString($Invariant)
        ) `
        -WorkingDirectory $CloudRoot `
        -RedirectStandardOutput $CloudOut `
        -RedirectStandardError $CloudErr `
        -WindowStyle Hidden `
        -PassThru
    for ($Attempt = 0; $Attempt -lt 30; $Attempt++) {
        if (Test-CloudHealth) {
            return
        }
        Start-Sleep -Milliseconds 500
    }
    throw "cloud market-data API did not become ready"
}

function Invoke-MonitorOnce {
    $env:CLOUD_PLATFORM_BASE_URL = "http://127.0.0.1:$CloudPort"
    $Arguments = @(
        "-m",
        "scripts.monitor_technical",
        "--symbol",
        $Symbol.ToUpperInvariant(),
        "--position-shares",
        $PositionShares.ToString($Invariant),
        "--position-average",
        $PositionAverage.ToString($Invariant),
        "--new-lot-shares",
        $NewLotShares.ToString($Invariant),
        "--new-lot-entry",
        $NewLotEntry.ToString($Invariant),
        "--new-lot-protect",
        $NewLotProtect.ToString($Invariant),
        "--all-exit",
        $AllExit.ToString($Invariant),
        "--add-shares",
        $AddShares.ToString($Invariant)
    )
    $Result = & $Python @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "technical monitor one-shot failed with exit code $LASTEXITCODE"
    }
    $Json = ($Result | Out-String).Trim()
    $Parsed = $Json | ConvertFrom-Json
    if (
        $Parsed.schema_version -ne "technical_monitor.v1" -or
        $Parsed.safety.automatic_order_authorized -ne $false
    ) {
        throw "technical monitor returned an invalid safety contract"
    }
    Add-Content -LiteralPath $JsonLog -Value $Json -Encoding utf8
    Write-Output $Json
}

try {
    Assert-OrStartCloudApi
    $SessionDate = [TimeZoneInfo]::ConvertTime(
        [DateTimeOffset]::UtcNow,
        $Eastern
    ).Date
    $ConsecutiveFailures = 0
    while ($true) {
        try {
            Invoke-MonitorOnce
            $ConsecutiveFailures = 0
        }
        catch {
            $ConsecutiveFailures += 1
            [Console]::Error.WriteLine(
                "$([DateTimeOffset]::UtcNow.ToString('o')) $($_.Exception.Message)"
            )
            if ($ConsecutiveFailures -ge 3) {
                throw "monitor stopped after 3 consecutive failures"
            }
        }

        if ($Once) {
            break
        }
        $NowEastern = [TimeZoneInfo]::ConvertTime(
            [DateTimeOffset]::UtcNow,
            $Eastern
        )
        if (
            $NowEastern.Date -ne $SessionDate -or
            $NowEastern.TimeOfDay -ge [TimeSpan]::FromHours(15.9666667)
        ) {
            break
        }
        $SecondsIntoInterval = (
            ($NowEastern.Minute % $IntervalMinutes) * 60
        ) + $NowEastern.Second
        $WaitSeconds = ($IntervalMinutes * 60) - $SecondsIntoInterval
        if ($WaitSeconds -lt 5) {
            $WaitSeconds = $IntervalMinutes * 60
        }
        Start-Sleep -Seconds $WaitSeconds
    }
}
finally {
    if ($null -ne $OwnedCloudProcess -and -not $OwnedCloudProcess.HasExited) {
        Stop-Process -Id $OwnedCloudProcess.Id
    }
    $Mutex.ReleaseMutex()
    $Mutex.Dispose()
}
