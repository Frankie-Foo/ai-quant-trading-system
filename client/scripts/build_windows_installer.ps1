$ErrorActionPreference = "Stop"

$userTemp = Join-Path $env:LOCALAPPDATA "Temp"
New-Item -ItemType Directory -Path $userTemp -Force | Out-Null
$env:TEMP = $userTemp
$env:TMP = $userTemp

$arguments = @(
    "electron-builder",
    "--config", "electron-builder.windows-analyst.yml",
    "--win", "nsis",
    "--x64",
    "--publish", "never"
)

& npx @arguments
if ($LASTEXITCODE -ne 0) {
    throw "electron-builder failed with exit code $LASTEXITCODE"
}
