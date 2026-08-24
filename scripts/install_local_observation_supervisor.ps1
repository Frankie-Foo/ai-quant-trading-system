$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$runner = Join-Path $PSScriptRoot "run_local_observation_supervisor.ps1"
$legacyTasks = @(
    "Trading System V2 - AI Quant Funnel",
    "Trading System V2 - Premarket",
    "Trading System V2 - Paper Session",
    "Trading System V2 - Postmarket Review"
)

foreach ($taskName in $legacyTasks) {
    if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        Disable-ScheduledTask -TaskName $taskName | Out-Null
    }
}

$startup = [Environment]::GetFolderPath("Startup")
$shortcutPath = Join-Path $startup "Trading System V2 - Local Observation Supervisor.lnk"
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = "powershell.exe"
$shortcut.Arguments = `
    "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$runner`""
$shortcut.WorkingDirectory = $root
$shortcut.WindowStyle = 7
$shortcut.Save()

$running = Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -match "-m schedule\.supervisor"
}
if (-not $running) {
    Start-Process `
        -FilePath "powershell.exe" `
        -ArgumentList $shortcut.Arguments `
        -WorkingDirectory $root `
        -WindowStyle Hidden
}

Write-Output "Installed and started current-user observation supervisor: $shortcutPath"
