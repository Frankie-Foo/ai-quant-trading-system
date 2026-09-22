import os
import subprocess
from pathlib import Path

import pytest

from kernel.strategy_policy import build_strategy_policy, write_strategy_policy
from scripts.check_release_binding import validate_release_binding


def test_release_requires_same_persistent_state_and_preserves_policy(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    release, state = tmp_path / "release", tmp_path / "state"
    release.mkdir()
    state.mkdir()
    active = tmp_path / "active.json"
    write_strategy_policy(active, build_strategy_policy(
        version="existing-approved", status="active", min_rvol=3.0,
        created_at_utc=datetime(2026, 9, 22, tzinfo=UTC), approved_by="test",
        approved_at_utc=datetime(2026, 9, 22, tzinfo=UTC),
    ))
    before = active.read_bytes()
    with pytest.raises(ValueError, match="state binding"):
        validate_release_binding(release, state, active)
    (release / "runs").mkdir()
    with pytest.raises(ValueError, match="state binding"):
        validate_release_binding(release, state, active)
    with pytest.raises(ValueError, match="outside"):
        validate_release_binding(release, release / "runs", active)
    (release / "runs").rmdir()
    if os.name == "nt":
        subprocess.run([
            "powershell", "-NoProfile", "-NonInteractive", "-Command",
            "New-Item -ItemType Junction -Path $env:TEST_RUNS -Target $env:TEST_STATE | Out-Null",
        ], env={**os.environ, "TEST_RUNS": str(release / "runs"), "TEST_STATE": str(state)},
            check=True, capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        (release / "runs").symlink_to(state, target_is_directory=True)
    result = validate_release_binding(release, state, active)
    assert result["policy_version"] == "existing-approved"
    assert active.read_bytes() == before


def test_installer_never_bootstraps_or_enables_new_tasks() -> None:
    source = (Path(__file__).parents[1] / "scripts/install_local_observation_tasks.ps1").read_text()
    assert "Assert-QuiescedDeployment" in source
    assert "scripts.check_release_binding" in source
    assert "manage_strategy_policy bootstrap" not in source
    assert "Enable-ScheduledTask" not in source
    assert "-Disable `" in source


@pytest.mark.skipif(os.name != "nt", reason="Windows Task Scheduler cutover guard")
@pytest.mark.parametrize(
    "fault", ["enabled", "running", "process", "supervisor", "startup", "none"],
)
def test_cutover_guard_rejects_active_owners_without_stopping_them(fault: str) -> None:
    source = Path(__file__).parents[1] / "scripts/install_local_observation_tasks.ps1"
    command = r"""
    $tokens=$null; $errors=$null
    $ast=[System.Management.Automation.Language.Parser]::ParseFile(
        $env:TEST_INSTALLER,[ref]$tokens,[ref]$errors)
    if ($errors.Count) {exit 99}
    $function=$ast.Find({param($n)
        $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $n.Name -eq 'Assert-QuiescedDeployment'
    },$true)
    . ([scriptblock]::Create($function.Extent.Text))
    function Test-Path {return ($env:TEST_FAULT -eq 'startup')}
    function Get-ScheduledTask {
        [pscustomobject]@{
            Settings=[pscustomobject]@{Enabled=($env:TEST_FAULT -eq 'enabled')}
            State=$(if ($env:TEST_FAULT -eq 'running') {'Running'} else {'Disabled'})
        }
    }
    function Get-CimInstance {
        if ($env:TEST_FAULT -eq 'supervisor') {
            [pscustomobject]@{Name='python.exe'; CommandLine='python -m schedule.supervisor'}
        }
        if ($env:TEST_FAULT -eq 'process') {
            [pscustomobject]@{
                Name='python.exe'; CommandLine='python -m scripts.monitor_modern_momentum_paper'
            }
        }
    }
    try {Assert-QuiescedDeployment; exit 0} catch {exit 1}
    """
    result = subprocess.run([
        "powershell", "-NoProfile", "-NonInteractive", "-Command", command,
    ], env={**os.environ, "TEST_INSTALLER": str(source), "TEST_FAULT": fault},
        capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert result.returncode == (0 if fault == "none" else 1)
