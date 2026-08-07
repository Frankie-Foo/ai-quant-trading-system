from __future__ import annotations

from pathlib import Path

import pytest

from schedule import child_process


def test_run_child_is_shell_free_and_returns_bounded_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    class Completed:
        returncode = 0
        stdout = "ok\n"
        stderr = ""

    def fake_run(command: tuple[str, ...], **kwargs: object) -> Completed:
        calls.append((command, kwargs))
        return Completed()

    monkeypatch.setattr(child_process.subprocess, "run", fake_run)  # type: ignore[attr-defined]

    result = child_process.run_child(
        ("python", "-m", "scripts.example"),
        cwd=tmp_path,
        timeout_seconds=30,
    )

    assert result.return_code == 0
    assert result.stdout == "ok\n"
    assert calls == [
        (
            ("python", "-m", "scripts.example"),
            {
                "cwd": tmp_path,
                "capture_output": True,
                "text": True,
                "timeout": 30,
                "check": False,
            },
        )
    ]


def test_run_child_rejects_invalid_commands_and_timeouts(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        child_process.run_child(("python", ""), cwd=tmp_path, timeout_seconds=30)
    with pytest.raises(ValueError, match="positive"):
        child_process.run_child(("python",), cwd=tmp_path, timeout_seconds=0)
