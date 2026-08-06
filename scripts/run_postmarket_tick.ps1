$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$runs = Join-Path $root "runs"
New-Item -ItemType Directory -Force -Path $runs | Out-Null
Set-Location -LiteralPath $root
& ".\.venv\Scripts\python.exe" -m schedule.postmarket `
    1>> (Join-Path $runs "postmarket_scheduler.out.log") `
    2>> (Join-Path $runs "postmarket_scheduler.err.log")
exit $LASTEXITCODE
