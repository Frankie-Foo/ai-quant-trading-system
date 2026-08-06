from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

from data_plane.storage import persist_snapshot
from kernel.config import load_config
from kernel.factor_selection import FactorSelectionPolicy, select_factor_candidates
from scripts.build_factor_candidates import build_factor_candidate_snapshot

ASOF = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
TRADE_DATE = date(2026, 7, 28)


def test_factor_policy_is_explicitly_versioned_in_system_config() -> None:
    cfg = load_config("config.yaml")
    assert cfg.factor_selection.max_candidates == 50
    assert cfg.factor_selection.min_score == 60.0
    assert cfg.factor_selection.rvol_full_score == 8.0


def _daily() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": ["FAST", "LOWRV", "NOBARS", "BLOCKED"],
            "precheck_pass": [True, True, True, False],
            "reject_reason": [
                "pending:rvol,market_cap,earnings,luld",
                "pending:rvol,market_cap,earnings,luld",
                "pending:rvol,market_cap,earnings,luld",
                "beta_below_min",
            ],
            "price": [10.0, 10.0, 10.0, 10.0],
            "adv_usd": [50_000_000.0] * 4,
            "beta": [3.0, 3.0, 3.0, 3.0],
            "atr_pct": [0.08, 0.08, 0.08, 0.08],
            "price_provenance": ["daily@test"] * 4,
            "adv_usd_provenance": ["daily@test"] * 4,
            "beta_provenance": ["daily@test"] * 4,
            "atr_pct_provenance": ["daily@test"] * 4,
        }
    )


def _premarket() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": ["FAST", "LOWRV", "BLOCKED"],
            "session_date": [TRADE_DATE] * 3,
            "availability": ["available"] * 3,
            "rvol": [8.0, 2.0, 8.0],
            "premarket_return": [0.08, 0.08, 0.08],
            "premarket_close": [10.8, 10.8, 10.8],
            "premarket_vwap": [10.48, 10.48, 10.48],
            "premarket_close_location": [1.0, 1.0, 1.0],
            "premarket_price_confirmation": [True, True, True],
            "data_cutoff_utc": [ASOF] * 3,
            "rvol_provenance": ["premarket@test"] * 3,
            "premarket_price_provenance": ["premarket@test"] * 3,
        }
    )


def test_pure_factor_selector_can_select_a_stock_without_a_catalyst() -> None:
    result = select_factor_candidates(
        _daily(),
        _premarket(),
        trade_date=TRADE_DATE,
        asof_utc=ASOF,
        policy=FactorSelectionPolicy(),
    )
    rows = {row["symbol"]: row for row in result.iter_rows(named=True)}

    assert rows["FAST"]["factor_pass"] is True
    assert rows["FAST"]["factor_rank"] == 1
    assert rows["FAST"]["factor_score"] == pytest.approx(100.0)
    assert rows["FAST"]["candidate_source"] == "factor"
    assert rows["LOWRV"]["factor_reject_reason"] == "rvol_below_min"
    assert rows["NOBARS"]["factor_reject_reason"] == "missing_premarket_features"
    assert rows["BLOCKED"]["factor_reject_reason"] == "daily_precheck:beta_below_min"


def test_factor_selector_rejects_future_or_wrong_session_features() -> None:
    future = _premarket().with_columns(
        pl.when(pl.col("symbol") == "FAST")
        .then(pl.lit(ASOF + timedelta(minutes=1)))
        .otherwise(pl.col("data_cutoff_utc"))
        .alias("data_cutoff_utc")
    )
    with pytest.raises(ValueError, match="after asof"):
        select_factor_candidates(
            _daily(),
            future,
            trade_date=TRADE_DATE,
            asof_utc=ASOF,
            policy=FactorSelectionPolicy(),
        )

    wrong_session = _premarket().with_columns(
        pl.lit(date(2026, 7, 27)).alias("session_date")
    )
    with pytest.raises(ValueError, match="target trade date"):
        select_factor_candidates(
            _daily(),
            wrong_session,
            trade_date=TRADE_DATE,
            asof_utc=ASOF,
            policy=FactorSelectionPolicy(),
        )


def test_factor_selector_is_invariant_to_rows_after_the_declared_cutoff() -> None:
    baseline = select_factor_candidates(
        _daily(),
        _premarket(),
        trade_date=TRADE_DATE,
        asof_utc=ASOF,
        policy=FactorSelectionPolicy(),
    )
    unrelated_future_bars = pl.DataFrame(
        {
            "symbol": ["FUTURE"],
            "session_date": [TRADE_DATE],
            "availability": ["available"],
            "rvol": [999.0],
            "premarket_return": [9.0],
            "premarket_close": [999.0],
            "premarket_vwap": [1.0],
            "premarket_close_location": [1.0],
            "premarket_price_confirmation": [True],
            "data_cutoff_utc": [ASOF + timedelta(minutes=1)],
            "rvol_provenance": ["future@test"],
            "premarket_price_provenance": ["future@test"],
        }
    )
    contaminated = pl.concat([_premarket(), unrelated_future_bars])
    with pytest.raises(ValueError, match="after asof"):
        select_factor_candidates(
            _daily(),
            contaminated,
            trade_date=TRADE_DATE,
            asof_utc=ASOF,
            policy=FactorSelectionPolicy(),
        )

    assert baseline.filter(pl.col("factor_pass")).get_column("symbol").to_list() == [
        "FAST"
    ]


def test_factor_candidate_snapshot_is_auditable_and_never_production_eligible(
    tmp_path: Path,
) -> None:
    daily_snapshot, _ = persist_snapshot(
        _daily(),
        root=tmp_path,
        source="kernel.universe.daily_precheck",
        schema_version="test.v1",
        checks=(),
    )
    rvol_snapshot, _ = persist_snapshot(
        _premarket(),
        root=tmp_path,
        source="kernel.premarket.factor_rvol_candidates",
        schema_version="test.v1",
        checks=(),
    )

    frame, snapshot, path = build_factor_candidate_snapshot(
        _daily(),
        _premarket(),
        daily_snapshot=daily_snapshot,
        rvol_snapshot=rvol_snapshot,
        data_root=tmp_path,
        trade_date=TRADE_DATE,
        asof_utc=ASOF,
        policy=FactorSelectionPolicy(),
    )

    assert snapshot.usable
    assert path.exists()
    assert frame.get_column("symbol").n_unique() == frame.height
    assert frame.filter(pl.col("production_eligible")).is_empty()
    assert snapshot.parent_snapshot_ids == (
        daily_snapshot.dataset_id,
        rvol_snapshot.dataset_id,
    )
