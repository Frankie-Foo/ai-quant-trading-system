import json
import sys
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl
import pytest
from pydantic import ValidationError

from research.modern_momentum import ModernMomentumConfig
from scripts import run_current_modern_backtest as backtest


def metadata() -> dict[str, object]:
    return {
        "experiment_id": "review-remediation-offline-20260907",
        "attempted_configurations": 7,
        "blind_evaluations": 2,
        "holdout_evaluations": 4,
        "holdout_previously_viewed": True,
        "data_sha256": "a" * 64,
        "evidence_refs": ["prior-research-ledger:experiment-42"],
    }


def test_research_requires_explicit_experiment_metadata_before_loading_data(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "backtest",
            "--signals",
            str(tmp_path / "missing.parquet"),
            "--data-root",
            str(tmp_path),
            "--output",
            str(tmp_path / "new.json"),
        ],
    )
    with pytest.raises(SystemExit) as exc:
        backtest.main()
    assert exc.value.code == 2
    assert not (tmp_path / "new.json").exists()


def test_reused_holdout_retains_declared_counts_and_never_claims_new_blind_evidence() -> None:
    declared = backtest.ModernExperimentMetadata.model_validate(metadata())
    audit = backtest.modern_research_audit(declared)
    assert audit["experiment"]["attempted_configurations"] == 7
    assert audit["experiment"]["blind_evaluations"] == 2
    assert audit["experiment"]["holdout_evaluations"] == 4
    assert audit["holdout_status"] == "reused_holdout"
    assert audit["new_blind_evaluation"] is False
    assert audit["decision"]["stage"] == "invalid_evidence"
    assert audit["production_eligible"] is False
    assert len(audit["code_sha256"]) == 64
    assert len(audit["feature_sha256"]) == 64
    assert audit["strategy_manifest"]["effective_config"]["minimum_premarket_rvol"] == 1.5
    json.dumps(audit, allow_nan=False)


@pytest.mark.parametrize(
    "field,value",
    [
        ("attempted_configurations", 0),
        ("attempted_configurations", True),
        ("blind_evaluations", -1),
        ("holdout_evaluations", 0),
        ("holdout_previously_viewed", False),
        ("data_sha256", "unknown"),
        ("evidence_refs", []),
    ],
)
def test_invalid_or_fabricated_experiment_identity_is_rejected(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        backtest.ModernExperimentMetadata.model_validate({**metadata(), field: value})


def test_backtest_refuses_to_overwrite_historical_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    metadata_path = tmp_path / "experiment.json"
    metadata_path.write_text(json.dumps(metadata()), encoding="utf-8")
    output = tmp_path / "past.json"
    output.write_text("immutable evidence", encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "backtest",
            "--signals",
            str(tmp_path / "missing.parquet"),
            "--data-root",
            str(tmp_path),
            "--output",
            str(output),
            "--experiment-metadata",
            str(metadata_path),
        ],
    )
    with pytest.raises(FileExistsError):
        backtest.main()
    assert output.read_text(encoding="utf-8") == "immutable evidence"


def test_historical_audit_is_read_only_and_distinguishes_10_from_25_basis_points(
    tmp_path: Path,
) -> None:
    path = tmp_path / "old.trades.parquet"
    # DST-aware: July 19:00 UTC is 15:00 ET; January 19:00 UTC is only 14:00 ET.
    timestamps = [
        datetime(2026, 7, 1, 18, 59, tzinfo=UTC),
        datetime(2026, 7, 1, 19, 0, tzinfo=UTC),
        datetime(2026, 1, 5, 19, 0, tzinfo=UTC),
        datetime(2026, 7, 1, 17, 5, tzinfo=UTC),
    ]
    pl.DataFrame(
        {
            "trade_date": [date(2026, 7, 1), date(2026, 7, 1), date(2026, 1, 5), date(2026, 7, 1)],
            "symbol": ["FIRST", "LATE", "TEN", "WIDE"],
            "attempt": [1, 2, 2, 2],
            "entry_ts_utc": timestamps,
            "signal_ts_utc": timestamps,
            "entry_relative_spread": [0.0025, 0.001, 0.0011478859766595594, 0.0026350461133069266],
            "all_in_stop_pct": [0.02] * 4,
            "premarket_rvol": [1.5] * 4,
            "exit_ts_utc": [datetime(2026, 7, 1, 19, 45, tzinfo=UTC)] * 4,
            "exit_reason": ["time_exit", "stop", "stop", "stop"],
        }
    ).write_parquet(path)
    before = path.read_bytes()
    audit = backtest.audit_historical_modern_eligibility(path)
    assert audit["attempts"] == 4 and audit["reentries"] == 3
    assert audit["entry_violation_count"] == 2
    assert audit["reentry_violation_count"] == 2
    assert audit["entry_violations_by_reason"]["at_or_after_cutoff"] == 1
    assert audit["entry_violations_by_reason"]["spread_exceeds_maximum"] == 1
    assert audit["time_exit_labels_requiring_replay"] == 1
    assert audit["historical_performance_status"] == "invalidated_for_current_strategy"
    assert audit["new_performance"] is None and audit["new_blind_evaluation"] is False
    ten = backtest.audit_historical_modern_eligibility(
        path, replace(ModernMomentumConfig(), maximum_entry_relative_spread=0.001)
    )
    assert ten["entry_violation_count"] == 4
    assert ten["reentry_violation_count"] == 3
    assert path.read_bytes() == before
