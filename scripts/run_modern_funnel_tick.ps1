param(
    [Parameter(Mandatory = $true)][string]$PythonPath,
    [Parameter(Mandatory = $true)][string]$EnvironmentFile,
    [Parameter(Mandatory = $true)][string]$DataRoot,
    [Parameter(Mandatory = $true)][string]$ActivePolicyFile,
    [Parameter(Mandatory = $true)][string]$ChallengerPolicyFile,
    [switch]$ArmPaper,
    [ValidateRange(0.01, 100.0)][decimal]$PaperSmokeMaxNotional = 100.0
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
if ($ArmPaper) {
    $env:AI_QUANT_PAPER_RUNTIME_CONFIRMED = "true"
    $env:AI_QUANT_PAPER_SMOKE_MAX_NOTIONAL = `
        $PaperSmokeMaxNotional.ToString([Globalization.CultureInfo]::InvariantCulture)
} else {
    $env:AI_QUANT_PAPER_RUNTIME_CONFIRMED = "false"
    Remove-Item Env:AI_QUANT_PAPER_SMOKE_MAX_NOTIONAL -ErrorAction SilentlyContinue
}
& $python -m schedule.modern_funnel `
    1>> (Join-Path $runs "modern_funnel_scheduler.out.log") `
    2>> (Join-Path $runs "modern_funnel_scheduler.err.log")
exit $LASTEXITCODE
