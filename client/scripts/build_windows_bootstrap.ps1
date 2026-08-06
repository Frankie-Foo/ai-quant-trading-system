$ErrorActionPreference = "Stop"

$clientRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$repoRoot = (Resolve-Path (Join-Path $clientRoot "..")).Path
$sourceDataRoot = $env:BOOTSTRAP_DATA_ROOT
if (-not $sourceDataRoot) {
    $sourceDataRoot = Join-Path $repoRoot "data"
}
$output = Join-Path $clientRoot "build\bootstrap\research-bootstrap.zip"
$arguments = @(
    "-m", "scripts.build_desktop_bootstrap",
    "--source-data-root", $sourceDataRoot,
    "--output", $output
)

Push-Location $repoRoot
try {
    & python @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "desktop bootstrap build failed with exit code $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}
