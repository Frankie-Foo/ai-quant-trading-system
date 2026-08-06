[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Config,
    [ValidateRange(1024, 65535)]
    [int]$ClientPort = 8787,
    [switch]$SkipWarmup
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$ClientRoot = Join-Path $RepoRoot "client"
$Runs = Join-Path $RepoRoot "runs"
$StateDb = Join-Path $Runs "adaptive-plans.sqlite3"
$SipDb = Join-Path $Runs "sip-stream.sqlite3"
$ConfigPath = (Resolve-Path -LiteralPath $Config).Path
$OwnedProcesses = [Collections.Generic.List[Diagnostics.Process]]::new()
$LockName = "Local\TradingSystemAdaptiveClient"

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "trading-system Python runtime is unavailable"
}
if (-not (Get-Command npm.cmd -ErrorAction SilentlyContinue)) {
    throw "npm.cmd is unavailable"
}
if (-not (Test-Path -LiteralPath (Join-Path $ClientRoot "node_modules") -PathType Container)) {
    throw "client dependencies are missing; run npm install in the client directory"
}
New-Item -ItemType Directory -Force -Path $Runs | Out-Null

$CreatedNew = $false
$Mutex = [Threading.Mutex]::new($true, $LockName, [ref]$CreatedNew)
if (-not $CreatedNew) {
    $Mutex.Dispose()
    throw "the adaptive client is already running"
}

function Start-OwnedProcess {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Module,
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,
        [Parameter(Mandatory = $true)]
        [string]$LogStem
    )

    $OutLog = Join-Path $Runs "$LogStem.out.log"
    $ErrLog = Join-Path $Runs "$LogStem.err.log"
    $Process = Start-Process `
        -FilePath $Python `
        -ArgumentList (@("-m", $Module) + $Arguments) `
        -WorkingDirectory $RepoRoot `
        -RedirectStandardOutput $OutLog `
        -RedirectStandardError $ErrLog `
        -WindowStyle Hidden `
        -PassThru
    $script:OwnedProcesses.Add($Process)
    Start-Sleep -Milliseconds 800
    if ($Process.HasExited) {
        throw "$Module failed during startup; inspect $ErrLog"
    }
}

try {
    $ParsedConfig = Get-Content -LiteralPath $ConfigPath -Raw -Encoding utf8 |
        ConvertFrom-Json
    if (
        $ParsedConfig.schema_version -ne "adaptive_plan_config.v1" -or
        $null -eq $ParsedConfig.plans -or
        $ParsedConfig.plans.Count -lt 1
    ) {
        throw "adaptive plan config is invalid"
    }
    $Symbols = @(
        foreach ($Plan in $ParsedConfig.plans) {
            $Plan.baseline.symbol
            $Plan.evidence.benchmark_symbol
            $Plan.evidence.sector_symbol
        }
    ) |
        Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
        ForEach-Object { $_.Trim().ToUpperInvariant() } |
        Sort-Object -Unique
    if ($Symbols.Count -lt 2) {
        throw "adaptive plan config does not contain a complete symbol set"
    }

    & $Python -m scripts.register_adaptive_plans `
        --config $ConfigPath `
        --state-db $StateDb
    if ($LASTEXITCODE -ne 0) {
        throw "adaptive plan registration failed"
    }
    if (-not $SkipWarmup) {
        & $Python -m scripts.warm_adaptive_sip_store `
            --config $ConfigPath `
            --sip-db $SipDb
        if ($LASTEXITCODE -ne 0) {
            throw "adaptive SIP warmup failed"
        }
    }

    Start-OwnedProcess `
        -Module "scripts.stream_alpaca_sip" `
        -Arguments @(
            "--symbols",
            ($Symbols -join ","),
            "--state-db",
            $SipDb,
            "--lock-file",
            (Join-Path $Runs "adaptive-client-sip.lock")
        ) `
        -LogStem "adaptive-client-sip"
    Start-OwnedProcess `
        -Module "scripts.run_adaptive_plan_monitor" `
        -Arguments @(
            "--config",
            $ConfigPath,
            "--state-db",
            $StateDb,
            "--sip-db",
            $SipDb,
            "--lock-file",
            (Join-Path $Runs "adaptive-client-monitor.lock")
        ) `
        -LogStem "adaptive-client-monitor"

    $env:ADAPTIVE_CLIENT_PORT = $ClientPort.ToString(
        [Globalization.CultureInfo]::InvariantCulture
    )
    Push-Location $ClientRoot
    try {
        & npm.cmd run desktop
        if ($LASTEXITCODE -ne 0) {
            throw "desktop client exited with code $LASTEXITCODE"
        }
    }
    finally {
        Pop-Location
    }
}
finally {
    foreach ($Process in $OwnedProcesses) {
        if (-not $Process.HasExited) {
            Stop-Process -Id $Process.Id
        }
    }
    $Mutex.ReleaseMutex()
    $Mutex.Dispose()
}
