from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from schedule.health import evaluate_health
from schedule.state import JobLedger


def test_health_is_ready_before_first_run_when_storage_parent_is_writable(
    tmp_path: Path,
) -> None:
    result = evaluate_health(
        data_root=tmp_path / "data",
        state_db=tmp_path / "state" / "jobs.sqlite3",
        check_credentials=False,
        require_success=False,
    )

    assert result["status"] == "ready"
    assert result["critical_failures"] == 0


def test_health_rejects_a_corrupt_existing_ledger(tmp_path: Path) -> None:
    state_db = tmp_path / "jobs.sqlite3"
    state_db.write_text("not sqlite", encoding="utf-8")

    result = evaluate_health(
        data_root=tmp_path / "data",
        state_db=state_db,
        check_credentials=False,
        require_success=False,
    )

    assert result["status"] == "not_ready"
    assert result["critical_failures"] == 1


def test_health_reports_missing_credential_names_without_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLOUD_PLATFORM_BASE_URL", raising=False)
    monkeypatch.delenv("CLOUD_MARKET_DATA_API_TOKEN", raising=False)

    result = evaluate_health(
        data_root=tmp_path / "data",
        state_db=tmp_path / "jobs.sqlite3",
        check_credentials=True,
        require_success=False,
    )

    assert result["status"] == "not_ready"
    rendered = str(result)
    assert "CLOUD_PLATFORM_BASE_URL" in rendered
    assert "CLOUD_MARKET_DATA_API_TOKEN" in rendered


def test_health_rejects_an_exhausted_latest_job(tmp_path: Path) -> None:
    state_db = tmp_path / "jobs.sqlite3"
    ledger = JobLedger(state_db)
    for _ in range(5):
        lease = ledger.acquire(
            "postmarket_review", date(2026, 7, 20), "postmarket_review.v2"
        )
        assert lease is not None
        ledger.fail(lease, error_code="RuntimeError")

    result = evaluate_health(
        data_root=tmp_path / "data",
        state_db=state_db,
        check_credentials=False,
        require_success=False,
    )

    assert result["status"] == "not_ready"
    assert result["critical_failures"] == 1
