param(
    [Parameter(Mandatory = $true)][string]$PythonPath,
    [Parameter(Mandatory = $true)][string]$EnvironmentFile,
    [Parameter(Mandatory = $true)][string]$DataRoot
)

$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$python = (Resolve-Path -LiteralPath $PythonPath).Path
$runtimeEnvironment = (Resolve-Path -LiteralPath $EnvironmentFile).Path
$sharedData = (Resolve-Path -LiteralPath $DataRoot).Path
$runs = Join-Path $root "runs"
New-Item -ItemType Directory -Force -Path $runs | Out-Null
Set-Location -LiteralPath $root
$env:AI_QUANT_RUNTIME_ENV_FILE = $runtimeEnvironment
$env:AI_QUANT_DATA_ROOT = $sharedData
$env:AI_QUANT_PAPER_RUNTIME_CONFIRMED = "false"
& $python -m schedule.postmarket `
    1>> (Join-Path $runs "postmarket_scheduler.out.log") `
    2>> (Join-Path $runs "postmarket_scheduler.err.log")
exit $LASTEXITCODE
