param(
    [string]$Start = "2024-07-17",
    [string]$End = "2026-07-16",
    [int]$MaxAttempts = 5
)

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $Root

for ($Attempt = 1; $Attempt -le $MaxAttempts; $Attempt++) {
    Write-Output "backfill attempt $Attempt/$MaxAttempts"
    & .\.venv\Scripts\python.exe -m data_plane.cli massive-grouped-daily `
        --start $Start --end $End
    if ($LASTEXITCODE -eq 0) {
        Write-Output "backfill complete"
        exit 0
    }
    if ($Attempt -lt $MaxAttempts) {
        Write-Output "attempt failed; retrying from accepted snapshots in 90 seconds"
        Start-Sleep -Seconds 90
    }
}

Write-Error "backfill did not complete after $MaxAttempts attempts"
exit 1
