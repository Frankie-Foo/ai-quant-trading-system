from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl

from data_plane.contracts import DatasetSnapshot
from scripts.build_counterfactual_candidate_cohort import build_counterfactual_cohort


def test_counterfactual_cohort_recovers_soft_rejection_but_keeps_hard_safety(
    tmp_path: Path,
) -> None:
    day = date(2026, 8, 17)
    path = tmp_path / "data.parquet"
    pl.DataFrame(
        {
            "session_date": [day] * 3,
            "symbol": ["SOFT", "PASS", "HALT"],
            "catalyst_categories": [["earnings"], ["general_news"], ["earnings"]],
            "market_cap": [2e9] * 3,
            "market_cap_asof_date": [day] * 3,
            "market_cap_provenance": ["pit"] * 3,
            "current_halt": [False, False, True],
            "luld_risk": [False] * 3,
            "rvol": [3.0, 4.0, 9.0],
            "rvol_provenance": ["premarket"] * 3,
            "premarket_return": [0.05, 0.02, 0.10],
            "premarket_gap_return": [0.04, 0.01, 0.08],
            "premarket_close_location": [0.9, 0.8, 0.9],
            "premarket_above_vwap": [True] * 3,
            "premarket_price_confirmation": [True] * 3,
            "pass_gate": [False, True, False],
            "reject_reason": ["soft", None, "halt"],
            "gate_asof_utc": [datetime(2026, 8, 17, 12, tzinfo=UTC)] * 3,
        }
    ).write_parquet(path)
    snapshot = DatasetSnapshot.model_validate(
        {
            "dataset_id": "kernel.universe.selection_gates-test",
            "source": "kernel.universe.selection_gates",
            "schema_version": "selection_gates.v2",
            "asof_utc": datetime(2026, 8, 18, tzinfo=UTC),
            "row_count": 3,
            "content_sha256": "0" * 64,
            "checks": [],
            "parent_snapshot_ids": [],
        }
    )

    cohort, _ = build_counterfactual_cohort({day: (path, snapshot)})

    assert cohort.get_column("symbol").to_list() == ["SOFT", "PASS"]
    assert cohort.get_column("counterfactual_rank").to_list() == [1, 2]
    assert cohort.get_column("pass_gate").to_list() == [False, True]
