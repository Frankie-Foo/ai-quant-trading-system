$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
$runs = Join-Path $root "runs"
$stdout = Join-Path $runs "observation_supervisor.out.log"
$stderr = Join-Path $runs "observation_supervisor.err.log"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Trading system virtual environment is missing: $python"
}

New-Item -ItemType Directory -Path $runs -Force | Out-Null
Set-Location -LiteralPath $root
& $python -m schedule.supervisor 1>> $stdout 2>> $stderr
exit $LASTEXITCODE
