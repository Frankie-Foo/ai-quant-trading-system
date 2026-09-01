from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from schedule import monthly_evolution


def test_monthly_cycle_runs_proposal_sandbox_and_shadow_challenger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        commands.append(command)
        module = command[command.index("-m") + 1]
        payload = {
            "scripts.run_monthly_evolution": {
                "status": "complete",
                "proposal_ids": ["proposal-1"],
            },
            "scripts.run_rvol_sandbox": {
                "status": "complete",
                "decision": "research_champion_promoted",
                "dataset_id": "sandbox-1",
            },
            "scripts.manage_strategy_policy": {
                "version": "challenger-test",
                "status": "shadow",
                "policy_hash": "a" * 64,
            },
        }[module]
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(monthly_evolution, "is_first_xnys_session", lambda _day: True)
    monkeypatch.setattr("schedule.monthly_evolution.subprocess.run", fake_run)

    exit_code = monthly_evolution.run(
        [
            "--asof-date",
            "2026-09-01",
            "--data-root",
            str(tmp_path / "data"),
            "--state-db",
            str(tmp_path / "jobs.sqlite3"),
            "--lock-file",
            str(tmp_path / "monthly.lock"),
            "--active-policy",
            str(tmp_path / "active.json"),
            "--challenger-policy",
            str(tmp_path / "challenger.json"),
        ]
    )

    assert exit_code == 0
    assert [command[command.index("-m") + 1] for command in commands] == [
        "scripts.run_monthly_evolution",
        "scripts.run_rvol_sandbox",
        "scripts.manage_strategy_policy",
    ]
    assert all("approve" not in command for command in commands)
    policy_command = commands[-1]
    assert policy_command[policy_command.index("--decision-dataset-id") + 1] == "sandbox-1"


def test_monthly_cycle_retains_champion_without_creating_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        commands.append(command)
        module = command[command.index("-m") + 1]
        payload = (
            {"status": "complete", "proposal_ids": []}
            if module == "scripts.run_monthly_evolution"
            else {
                "status": "complete",
                "decision": "champion_retained",
                "dataset_id": "sandbox-1",
            }
        )
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(monthly_evolution, "is_first_xnys_session", lambda _day: True)
    monkeypatch.setattr("schedule.monthly_evolution.subprocess.run", fake_run)

    assert monthly_evolution.run(
        [
            "--asof-date",
            date(2026, 9, 1).isoformat(),
            "--data-root",
            str(tmp_path / "data"),
            "--state-db",
            str(tmp_path / "jobs.sqlite3"),
            "--lock-file",
            str(tmp_path / "monthly.lock"),
            "--active-policy",
            str(tmp_path / "active.json"),
            "--challenger-policy",
            str(tmp_path / "challenger.json"),
        ]
    ) == 0
    assert len(commands) == 1
