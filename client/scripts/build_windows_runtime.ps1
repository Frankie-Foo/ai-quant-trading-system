$ErrorActionPreference = "Stop"

$clientRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$repoRoot = (Resolve-Path (Join-Path $clientRoot "..")).Path
$buildRoot = [System.IO.Path]::GetFullPath((Join-Path $clientRoot "build"))
$runtimeOutput = [System.IO.Path]::GetFullPath((Join-Path $buildRoot "runtime"))
$runtimeWork = [System.IO.Path]::GetFullPath((Join-Path $buildRoot "runtime-work"))
$specFile = [System.IO.Path]::GetFullPath(
    (Join-Path $buildRoot "windows-research-runtime.spec")
)

foreach ($target in @($runtimeOutput, $runtimeWork, $specFile)) {
    if (-not $target.StartsWith($buildRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to clean a path outside the client build directory: $target"
    }
    if (Test-Path -LiteralPath $target) {
        Remove-Item -LiteralPath $target -Recurse -Force
    }
}

New-Item -ItemType Directory -Path $runtimeOutput -Force | Out-Null

$arguments = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    "--onefile",
    "--name", "windows-research-runtime",
    "--paths", $repoRoot,
    "--distpath", $runtimeOutput,
    "--workpath", $runtimeWork,
    "--specpath", $buildRoot,
    "--collect-data", "pandas_market_calendars",
    "--copy-metadata", "pandas-market-calendars",
    "--add-data", "$($repoRoot)\config.yaml;.",
    "--add-data", "$repoRoot\config;config",
    "--hidden-import", "data_plane.cli",
    "--hidden-import", "ibapi.client",
    "--hidden-import", "ibapi.contract",
    "--hidden-import", "ibapi.order",
    "--hidden-import", "ibapi.wrapper",
    "--hidden-import", "scripts.build_catalyst_snapshot",
    "--hidden-import", "scripts.backfill_massive_news",
    "--hidden-import", "scripts.backfill_massive_reference_weekly",
    "--hidden-import", "scripts.build_cross_asset_sentiment_snapshot",
    "--hidden-import", "scripts.build_daily_universe",
    "--hidden-import", "scripts.build_factor_candidates",
    "--hidden-import", "scripts.build_order_flow_snapshot",
    "--hidden-import", "scripts.build_orb5_signals",
    "--hidden-import", "scripts.build_postmarket_episode",
    "--hidden-import", "scripts.build_premarket_rvol",
    "--hidden-import", "scripts.build_selection_gates",
    "--hidden-import", "scripts.build_unified_shadow_selection",
    "--hidden-import", "scripts.review_postmarket_episode",
    "--hidden-import", "scripts.run_multisignal_shadow_pipeline",
    "--hidden-import", "scripts.run_postclose_missed_movers_review",
    "--hidden-import", "scripts.run_structured_pdca",
    "$repoRoot\scripts\macos_research_entry.py"
)

Push-Location $clientRoot
try {
    & python @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}

$runtimeBinary = Join-Path $runtimeOutput "windows-research-runtime.exe"
if (-not (Test-Path -LiteralPath $runtimeBinary -PathType Leaf)) {
    throw "Windows research runtime was not created"
}
