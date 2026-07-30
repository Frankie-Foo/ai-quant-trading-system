from __future__ import annotations

from datetime import UTC, datetime, timedelta

import polars as pl
import pytest

from kernel.selection_arbitration import (
    ShadowArbitrationPolicy,
    arbitrate_shadow_candidates,
)

ASOF = datetime(2026, 7, 28, 14, 20, tzinfo=UTC)


def _catalyst() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": ["BOTH", "CAT", "REJECTED"],
            "pass_gate": [True, True, False],
            "selection_rank": [1, 2, None],
            "gate_asof_utc": [ASOF] * 3,
        }
    ).with_columns(
        pl.col("selection_rank").cast(pl.Int64),
        pl.col("gate_asof_utc").cast(pl.Datetime("ms", "UTC")),
    )


def _factor() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": ["BOTH", "FACTOR", "FAILED"],
            "factor_pass": [True, True, False],
            "factor_rank": [2, 1, None],
            "factor_score": [80.0, 90.0, 99.0],
            "factor_asof_utc": [ASOF] * 3,
        }
    ).with_columns(
        pl.col("factor_rank").cast(pl.Int64),
        pl.col("factor_asof_utc").cast(pl.Datetime("ms", "UTC")),
    )


def _order_flow() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": ["BOTH", "CAT", "FACTOR"],
            "availability": ["available", "available", "no_trades"],
            "order_flow_confirmation_score": [60.0, 80.0, None],
            "data_cutoff_utc": [ASOF] * 3,
            "order_flow_provenance": ["test.sip"] * 3,
        }
    ).with_columns(
        pl.col("data_cutoff_utc").cast(pl.Datetime("ns", "UTC")),
    )


def test_arbitration_keeps_factor_only_candidates_and_uses_flow_as_confirmation() -> None:
    result = arbitrate_shadow_candidates(
        _catalyst(),
        _factor(),
        _order_flow(),
        asof_utc=ASOF,
        policy=ShadowArbitrationPolicy(),
    )
    rows = {row["symbol"]: row for row in result.iter_rows(named=True)}

    assert result.sort("unified_rank").get_column("symbol").to_list() == [
        "BOTH",
        "FACTOR",
        "CAT",
    ]
    assert rows["BOTH"]["candidate_source"] == "catalyst+factor"
    assert rows["BOTH"]["catalyst_score"] == pytest.approx(100.0)
    assert rows["BOTH"]["intersection_bonus"] == pytest.approx(5.0)
    assert rows["BOTH"]["order_flow_adjustment"] == pytest.approx(3.0)
    assert rows["BOTH"]["unified_score"] == pytest.approx(108.0)
    assert rows["FACTOR"]["candidate_source"] == "factor"
    assert rows["FACTOR"]["unified_score"] == pytest.approx(90.0)
    assert rows["FACTOR"]["order_flow_adjustment"] == pytest.approx(0.0)
    assert rows["CAT"]["candidate_source"] == "catalyst"
    assert rows["CAT"]["unified_score"] == pytest.approx(59.0)
    assert set(result.get_column("symbol")) == {"BOTH", "CAT", "FACTOR"}
    assert result.filter(pl.col("production_eligible")).is_empty()
    assert result.filter(pl.col("execution_eligible")).is_empty()


def test_future_inputs_fail_closed() -> None:
    future = _order_flow().with_columns(
        pl.lit(ASOF + timedelta(microseconds=1))
        .cast(pl.Datetime("ns", "UTC"))
        .alias("data_cutoff_utc")
    )
    with pytest.raises(ValueError, match="after asof"):
        arbitrate_shadow_candidates(
            _catalyst(),
            _factor(),
            future,
            asof_utc=ASOF,
            policy=ShadowArbitrationPolicy(),
        )


def test_duplicate_symbols_are_rejected() -> None:
    duplicate_factor = pl.concat([_factor(), _factor().head(1)])
    with pytest.raises(ValueError, match="duplicate"):
        arbitrate_shadow_candidates(
            _catalyst(),
            duplicate_factor,
            _order_flow(),
            asof_utc=ASOF,
            policy=ShadowArbitrationPolicy(),
        )
