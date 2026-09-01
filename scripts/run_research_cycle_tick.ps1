param(
    [Parameter(Mandatory = $true)][string]$PythonPath,
    [Parameter(Mandatory = $true)][string]$EnvironmentFile,
    [Parameter(Mandatory = $true)][string]$DataRoot,
    [Parameter(Mandatory = $true)][string]$ActivePolicyFile,
    [Parameter(Mandatory = $true)][string]$ChallengerPolicyFile
)

$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$python = (Resolve-Path -LiteralPath $PythonPath).Path
$runtimeEnvironment = (Resolve-Path -LiteralPath $EnvironmentFile).Path
$sharedData = (Resolve-Path -LiteralPath $DataRoot).Path
$activePolicy = (Resolve-Path -LiteralPath $ActivePolicyFile).Path
$challengerPolicy = [IO.Path]::GetFullPath($ChallengerPolicyFile)
$runs = Join-Path $root "runs"
New-Item -ItemType Directory -Force -Path $runs | Out-Null
Set-Location -LiteralPath $root
$env:AI_QUANT_RUNTIME_ENV_FILE = $runtimeEnvironment
$env:AI_QUANT_DATA_ROOT = $sharedData
$env:AI_QUANT_ACTIVE_POLICY_FILE = $activePolicy
$env:AI_QUANT_CHALLENGER_POLICY_FILE = $challengerPolicy
$env:AI_QUANT_PAPER_RUNTIME_CONFIRMED = "false"
& $python -m schedule.research_cycle `
    --data-root $sharedData `
    --state-root $runs `
    --lock-file (Join-Path $runs "research-cycle.lock") `
    1>> (Join-Path $runs "research_cycle.out.log") `
    2>> (Join-Path $runs "research_cycle.err.log")
exit $LASTEXITCODE
