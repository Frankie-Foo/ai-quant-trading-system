from __future__ import annotations

import sqlite3
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


def test_health_rejects_an_unversioned_existing_ledger(tmp_path: Path) -> None:
    state_db = tmp_path / "jobs.sqlite3"
    with sqlite3.connect(state_db) as connection:
        connection.execute("CREATE TABLE legacy_marker (value TEXT NOT NULL)")

    result = evaluate_health(
        data_root=tmp_path / "data",
        state_db=state_db,
        check_credentials=False,
        require_success=False,
    )

    assert result["status"] == "not_ready"
    checks = result["checks"]
    assert isinstance(checks, list)
    migration_check = next(item for item in checks if item["name"] == "ledger_migrations")
    assert migration_check["status"] == "fail"


def test_health_reports_missing_credential_names_without_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MARKET_DATA_PROVIDER", "cloud_proxy")
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


def test_health_accepts_direct_alpaca_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MARKET_DATA_PROVIDER", "alpaca_direct")
    monkeypatch.setenv("ALPACA_API_KEY_ID", "key-id")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "secret-key")

    result = evaluate_health(
        data_root=tmp_path / "data",
        state_db=tmp_path / "jobs.sqlite3",
        check_credentials=True,
        require_success=False,
    )

    assert result["status"] == "ready"
    checks = result["checks"]
    assert isinstance(checks, list)
    provider = next(item for item in checks if item["name"] == "provider_credentials")
    assert provider["status"] == "pass"


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
