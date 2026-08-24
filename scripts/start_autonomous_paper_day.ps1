param(
    [Parameter(Mandatory = $true)][string]$Config
)

$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$python = Join-Path $root ".venv\Scripts\python.exe"
$configPath = Resolve-Path $Config
$dayRoot = Split-Path -Parent $configPath
$runs = Join-Path $root "runs"
New-Item -ItemType Directory -Force -Path $runs | Out-Null
Set-Location -LiteralPath $root

function Start-AutonomousProcess {
    param([string]$Name, [string[]]$Arguments)
    $out = Join-Path $runs "autonomous-$Name.out.log"
    $err = Join-Path $runs "autonomous-$Name.err.log"
    return Start-Process -FilePath $python -WindowStyle Hidden -ArgumentList $Arguments `
        -RedirectStandardOutput $out -RedirectStandardError $err -PassThru
}

& $python -m scripts.warm_autonomous_sip_store `
    --config $configPath `
    --sip-db (Join-Path $dayRoot "sip.sqlite3") `
    --lock-file (Join-Path $dayRoot "sip-warmup.lock")
if ($LASTEXITCODE -ne 0) { throw "autonomous SIP warmup failed" }

$sip = Start-AutonomousProcess "sip" @(
    "-m", "scripts.run_autonomous_sip_refresher", "--config", $configPath,
    "--sip-db", (Join-Path $dayRoot "sip.sqlite3"),
    "--lock-file", (Join-Path $dayRoot "sip-refresher.lock"),
    "--refresh-lock-file", (Join-Path $dayRoot "sip-warmup.lock"),
    "--interval-seconds", "15", "--max-seconds", "36000"
)
$agents = Start-AutonomousProcess "agents" @(
    "-m", "scripts.run_runtime_agent_cycle", "--config", $configPath,
    "--agent-root", (Join-Path $dayRoot "agents"),
    "--push-health", (Join-Path $dayRoot "push-health.json"),
    "--lock-file", (Join-Path $dayRoot "agents.lock"),
    "--interval-seconds", "15", "--max-seconds", "36000"
)
$executor = Start-AutonomousProcess "executor" @(
    "-m", "scripts.run_autonomous_paper_session", "--config", $configPath,
    "--broker-mode", "direct", "--state-db", (Join-Path $dayRoot "paper.sqlite3"),
    "--sip-db", (Join-Path $dayRoot "sip.sqlite3"),
    "--notification-db", (Join-Path $dayRoot "notifications.sqlite3"),
    "--push-health", (Join-Path $dayRoot "push-health.json"),
    "--lock-file", (Join-Path $dayRoot "executor.lock"), "--arm-paper", "--max-seconds", "36000"
)

try {
    Wait-Process -Id $executor.Id
    if ($executor.ExitCode -ne 0) { throw "autonomous Paper executor failed" }
} finally {
    foreach ($process in @($sip, $agents)) {
        if ($null -ne $process -and -not $process.HasExited) {
            Stop-Process -Id $process.Id -Force
        }
    }
}
