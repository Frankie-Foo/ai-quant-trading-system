param(
    [string]$End = "2026-07-16",
    [int]$Sessions = 252,
    [int]$MaxAttempts = 8
)

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
Set-Location -LiteralPath $Root

function Invoke-PythonStage {
    param(
        [string]$Name,
        [string]$Module,
        [string[]]$StageArguments,
        [int]$Attempts = $MaxAttempts
    )
    for ($Attempt = 1; $Attempt -le $Attempts; $Attempt++) {
        Write-Output "stage=$Name attempt=$Attempt/$Attempts"
        & $Python -m $Module @StageArguments
        if ($LASTEXITCODE -eq 0) {
            Write-Output "stage=$Name status=complete"
            return
        }
        if ($Attempt -lt $Attempts) {
            Write-Output "stage=$Name status=retry_wait"
            Start-Sleep -Seconds 30
        }
    }
    throw "stage failed after retries: $Name"
}

# An independently started reference backfill may already own the provider-rate-limit
# lock. Wait for it to finish, then re-run the stage; accepted snapshots make this a
# fast integrity check rather than a second download.
while (
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            $_.CommandLine -like "*scripts.backfill_massive_reference_weekly*" -and
            $_.ProcessId -ne $PID
        }
) {
    Write-Output "stage=reference status=waiting_for_existing_process"
    Start-Sleep -Seconds 30
}

Invoke-PythonStage `
    -Name "reference" `
    -Module "scripts.backfill_massive_reference_weekly" `
    -StageArguments @("--end", $End, "--sessions", "$Sessions")
Invoke-PythonStage `
    -Name "pit_selection" `
    -Module "scripts.build_historical_selection" `
    -StageArguments @("--end", $End, "--sessions", "$Sessions")
Invoke-PythonStage `
    -Name "premarket_rvol" `
    -Module "scripts.backfill_historical_premarket" `
    -StageArguments @("--end", $End)
Invoke-PythonStage `
    -Name "selection_gates" `
    -Module "scripts.build_historical_selection_gates" `
    -StageArguments @("--end", $End)
Invoke-PythonStage `
    -Name "net_labels_oos" `
    -Module "scripts.backfill_historical_labels" `
    -StageArguments @("--end", $End)
Invoke-PythonStage `
    -Name "sandbox_evolution" `
    -Module "scripts.run_rvol_sandbox" `
    -StageArguments @()

Write-Output "research backfill complete"
