from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import run_hidden_task


def test_hidden_launcher_keeps_powershell_off_desktop_and_preserves_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    runner = scripts / "run_modern_funnel_tick.ps1"
    runner.write_text("exit 0", encoding="utf-8")
    monkeypatch.setattr(run_hidden_task, "__file__", str(scripts / "run_hidden_task.py"))
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 17)

    monkeypatch.setattr("scripts.run_hidden_task.subprocess.run", fake_run)

    assert run_hidden_task.run([str(runner), "-ArmPaper"]) == 17
    command, kwargs = calls[0]
    assert command[:7] == [
        "powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle",
        "Hidden", "-ExecutionPolicy", "Bypass",
    ]
    assert command[7:] == ["-File", str(runner), "-ArmPaper"]
    assert kwargs["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["stderr"] == subprocess.STDOUT
    assert kwargs["cwd"] == tmp_path
    assert "launcher exit_code=17" in (
        tmp_path / "runs" / "run_modern_funnel_tick.launcher.log"
    ).read_text(encoding="ascii")


def test_hidden_launcher_rejects_unowned_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    runner = tmp_path / "run_modern_funnel_tick.ps1"
    runner.write_text("exit 0", encoding="utf-8")
    monkeypatch.setattr(run_hidden_task, "__file__", str(scripts / "run_hidden_task.py"))

    def unexpected_run(*args: object, **kwargs: object) -> None:
        raise AssertionError("unowned script must not execute")

    monkeypatch.setattr("scripts.run_hidden_task.subprocess.run", unexpected_run)
    assert run_hidden_task.run([str(runner)]) != 0
