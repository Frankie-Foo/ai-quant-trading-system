from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl

from data_plane.storage import persist_snapshot
from kernel.selection_arbitration import ShadowArbitrationPolicy
from scripts.build_unified_shadow_selection import build_unified_shadow_snapshot

TRADE_DATE = date(2026, 7, 28)
ASOF = datetime(2026, 7, 28, 14, 20, tzinfo=UTC)


def _catalyst() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": ["BOTH", "CAT"],
            "session_date": [TRADE_DATE] * 2,
            "pass_gate": [True, True],
            "selection_rank": [1, 2],
            "gate_asof_utc": [ASOF] * 2,
        }
    ).with_columns(pl.col("gate_asof_utc").cast(pl.Datetime("ms", "UTC")))


def _factor() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": ["BOTH", "FACTOR"],
            "session_date": [TRADE_DATE] * 2,
            "factor_pass": [True, True],
            "factor_rank": [2, 1],
            "factor_score": [80.0, 90.0],
            "factor_asof_utc": [ASOF] * 2,
        }
    ).with_columns(pl.col("factor_asof_utc").cast(pl.Datetime("ms", "UTC")))


def _flow() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": ["BOTH", "CAT", "FACTOR"],
            "session_date": [TRADE_DATE] * 3,
            "availability": ["available", "available", "no_trades"],
            "order_flow_confirmation_score": [60.0, 80.0, None],
            "data_cutoff_utc": [ASOF] * 3,
            "order_flow_provenance": ["test.sip"] * 3,
        }
    ).with_columns(pl.col("data_cutoff_utc").cast(pl.Datetime("ns", "UTC")))


def test_unified_snapshot_preserves_lineage_and_cannot_execute(tmp_path: Path) -> None:
    parents = []
    for source, frame in (
        ("kernel.universe.selection_gates", _catalyst()),
        ("kernel.selection.factor_candidates_shadow", _factor()),
        ("kernel.features.order_flow_shadow", _flow()),
    ):
        snapshot, _ = persist_snapshot(
            frame,
            root=tmp_path,
            source=source,
            schema_version="test.v1",
            checks=(),
        )
        parents.append(snapshot)

    frame, snapshot, path = build_unified_shadow_snapshot(
        _catalyst(),
        _factor(),
        _flow(),
        catalyst_snapshot=parents[0],
        factor_snapshot=parents[1],
        order_flow_snapshot=parents[2],
        data_root=tmp_path,
        trade_date=TRADE_DATE,
        asof_utc=ASOF,
        policy=ShadowArbitrationPolicy(),
    )

    assert snapshot.usable
    assert path.exists()
    assert snapshot.parent_snapshot_ids == tuple(
        parent.dataset_id for parent in parents
    )
    assert frame.get_column("session_date").unique().to_list() == [TRADE_DATE]
    assert frame.filter(
        pl.col("production_eligible") | pl.col("execution_eligible")
    ).is_empty()
    assert frame.sort("unified_rank").get_column("symbol").to_list() == [
        "BOTH",
        "FACTOR",
        "CAT",
    ]
