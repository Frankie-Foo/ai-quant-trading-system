$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$runs = Join-Path $root "runs"
New-Item -ItemType Directory -Force -Path $runs | Out-Null
Set-Location -LiteralPath $root
& ".\.venv\Scripts\python.exe" -m schedule.paper `
    1>> (Join-Path $runs "paper_scheduler.out.log") `
    2>> (Join-Path $runs "paper_scheduler.err.log")
exit $LASTEXITCODE
