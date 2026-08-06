from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl

from scripts.run_multisignal_shadow_pipeline import (
    shadow_pipeline_commands,
    stage_snapshot_reusable,
)


def test_shadow_pipeline_orders_dependencies_and_never_calls_execution() -> None:
    commands = shadow_pipeline_commands(
        trade_date=date(2026, 7, 28),
        data_root=Path("D:/quant/data"),
        asof_utc=datetime(2026, 7, 28, 14, 20, tzinfo=UTC),
    )

    assert [stage for stage, _ in commands] == [
        "cross_asset_sentiment",
        "factor_rvol",
        "factor_candidates",
        "order_flow",
        "unified_arbitration",
    ]
    flattened = " ".join(argument for _, command in commands for argument in command)
    assert "--pool factor" in flattened
    assert "scripts.build_cross_asset_sentiment_snapshot" in flattened
    assert "scripts.build_order_flow_snapshot" in flattened
    assert "scripts.build_unified_shadow_selection" in flattened
    assert "order" not in {
        argument.lower() for _, command in commands for argument in command
    }


def test_live_cross_asset_stage_is_never_reused_and_historical_requires_exact_cutoff() -> None:
    cutoff = datetime(2026, 7, 28, 14, 20, tzinfo=UTC)
    frame = pl.DataFrame({"data_cutoff_utc": [cutoff]}).with_columns(
        pl.col("data_cutoff_utc").cast(pl.Datetime("us", "UTC"))
    )

    assert not stage_snapshot_reusable(
        "cross_asset_sentiment",
        frame,
        requested_asof_utc=None,
    )
    assert stage_snapshot_reusable(
        "cross_asset_sentiment",
        frame,
        requested_asof_utc=cutoff,
    )
    assert not stage_snapshot_reusable(
        "cross_asset_sentiment",
        frame,
        requested_asof_utc=datetime(2026, 7, 28, 14, 21, tzinfo=UTC),
    )
    assert stage_snapshot_reusable(
        "factor_candidates",
        frame,
        requested_asof_utc=None,
    )
