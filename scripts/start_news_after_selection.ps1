$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$selectionError = Join-Path $root "runs\selection_gates_prefetch.err.log"
$python = Join-Path $root ".venv\Scripts\python.exe"

while (
    Get-CimInstance Win32_Process |
        Where-Object { $_.CommandLine -like "*scripts.build_selection_gates*" }
) {
    Start-Sleep -Seconds 15
}

if ((Test-Path -LiteralPath $selectionError) -and (Get-Item $selectionError).Length -gt 0) {
    throw "selection-gate prefetch failed; historical news backfill was not started"
}

Set-Location -LiteralPath $root
& $python -m scripts.backfill_massive_news `
    --start 2024-07-17 `
    --end 2026-07-18

if ($LASTEXITCODE -ne 0) {
    throw "historical Massive news backfill exited with code $LASTEXITCODE"
}
