from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl

from data_plane.contracts import DatasetSnapshot
from scripts.build_h30_candidate_cohort import build_candidate_cohort


def _snapshot(dataset_id: str) -> DatasetSnapshot:
    return DatasetSnapshot.model_validate(
        {
            "dataset_id": dataset_id,
            "source": "kernel.universe.selection_gates",
            "schema_version": "selection_gates.v1",
            "asof_utc": datetime(2026, 8, 18, tzinfo=UTC),
            "row_count": 2,
            "content_sha256": "0" * 64,
            "checks": [],
            "parent_snapshot_ids": [],
        }
    )


def test_build_candidate_cohort_keeps_only_passed_billion_dollar_names(
    tmp_path: Path,
) -> None:
    path = tmp_path / "data.parquet"
    pl.DataFrame(
        {
            "session_date": [date(2026, 8, 17)] * 3,
            "symbol": ["KEEP", "SMALL", "FAIL"],
            "selection_rank": [1, 2, None],
            "market_cap": [2e9, 5e8, 3e9],
            "market_cap_asof_date": [date(2026, 8, 14)] * 3,
            "market_cap_provenance": ["pit"] * 3,
            "rvol": [2.0, 3.0, 4.0],
            "rvol_provenance": ["premarket"] * 3,
            "catalyst_categories": [["earnings"]] * 3,
            "evidence_sources": [["news"]] * 3,
            "evidence_event_ids": [["event"]] * 3,
            "gate_asof_utc": [datetime(2026, 8, 17, 12, tzinfo=UTC)] * 3,
            "pass_gate": [True, True, False],
        }
    ).write_parquet(path)
    snapshot = _snapshot("kernel.universe.selection_gates-test")

    cohort, parents = build_candidate_cohort(
        {date(2026, 8, 17): (path, snapshot)}
    )

    assert cohort.get_column("symbol").to_list() == ["KEEP"]
    assert cohort.get_column("gate_snapshot_id").to_list() == [snapshot.dataset_id]
    assert parents == (snapshot.dataset_id,)
