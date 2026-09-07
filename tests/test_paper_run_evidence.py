"""Startup evidence never invents account identity or overwrites a prior start."""

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from execution.alpaca_paper import PaperAccount
from operations.paper_run_evidence import capture_startup


def test_startup_is_content_addressed_and_keeps_unknown_identity(tmp_path: Path) -> None:
    plan = tmp_path / "plan.json"
    confirmation = tmp_path / "confirmation.json"
    plan.write_text('{}', encoding="utf-8")
    confirmation.write_text('{}', encoding="utf-8")
    account = PaperAccount(status="ACTIVE", account_blocked=False, trading_blocked=False,
                           equity="100000", last_equity="100000", buying_power="400000")
    def capture() -> Path:
        return capture_startup(directory=tmp_path, trade_date=date(2026, 9, 8), account=account,
                positions=(), open_orders=(), plan_path=plan, confirmation_path=confirmation,
                ledger_path=tmp_path / "paper-state.sqlite3",
                observed_start_utc=datetime(2026, 9, 8, 13, 36, tzinfo=UTC),
                observed_end_utc=datetime(2026, 9, 8, 13, 36, 1, tzinfo=UTC))
    path = capture()
    assert capture() == path
    evidence = json.loads(path.read_text())
    assert evidence["account"]["id"] is None
    assert evidence["positions"] == []
    assert "opening_positions_flat" not in evidence
    assert "reconciled_complete" not in evidence
    assert evidence["ledger_path"] == str((tmp_path / "paper-state.sqlite3").resolve())
    plan.write_text('{"changed":true}', encoding="utf-8")
    assert capture() != path
    assert path.exists()


def test_startup_rejects_naive_observation_window(tmp_path: Path) -> None:
    account = PaperAccount(status="ACTIVE", account_blocked=False, trading_blocked=False,
                           equity="1", last_equity="1", buying_power="1")
    with pytest.raises(ValueError, match="UTC"):
        capture_startup(directory=tmp_path, trade_date=date(2026, 9, 8), account=account,
                        positions=(), open_orders=(), plan_path=tmp_path / "absent",
                        confirmation_path=tmp_path / "absent", ledger_path=tmp_path / "db",
                        observed_start_utc=datetime(2026, 9, 8),
                        observed_end_utc=datetime(2026, 9, 8))
