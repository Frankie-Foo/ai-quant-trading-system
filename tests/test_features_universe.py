from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import polars as pl
import pytest

from kernel.config import load_config
from kernel.features.liquidity import (
    average_dollar_volume,
    corwin_schultz_spread,
    zero_trade_fraction,
)
from kernel.features.momentum import atr, beta, days_in_play
from kernel.features.overnight_intraday import decompose
from kernel.universe import REQUIRED_UNIVERSE_COLUMNS, _build_universe_from_daily

ROOT = Path(__file__).resolve().parents[1]


def test_atr_is_zero_for_constant_prices() -> None:
    frame = pl.DataFrame(
        {
            "high": [10.0] * 20,
            "low": [10.0] * 20,
            "close": [10.0] * 20,
        }
    )
    result = atr(frame, n=14)
    assert result[-1] == pytest.approx(0.0)


def test_beta_recovers_known_market_loading() -> None:
    dates = [date(2025, 1, 1) + timedelta(days=index) for index in range(270)]
    market_prices = [100.0]
    stock_prices = [50.0]
    for index in range(1, len(dates)):
        market_return = 0.001 + ((index % 7) - 3) * 0.0007
        market_prices.append(market_prices[-1] * (1 + market_return))
        stock_prices.append(stock_prices[-1] * (1 + 2 * market_return))
    market = pl.DataFrame({"trade_date": dates, "close": market_prices})
    stock = pl.DataFrame({"trade_date": dates, "close": stock_prices})
    assert beta(stock, market, n=252) == pytest.approx(2.0, abs=0.01)


def test_average_dollar_volume_uses_only_requested_trailing_rows() -> None:
    frame = pl.DataFrame(
        {
            "close": [5.0, 10.0, 20.0],
            "volume": [100.0, 200.0, 300.0],
        }
    )
    assert average_dollar_volume(frame, n=2) == pytest.approx(4_000.0)


def test_days_in_play_counts_only_consecutive_extreme_rvol_tail() -> None:
    assert days_in_play(pl.Series([1.0, 4.0, 2.0, 3.1, 5.0]), min_rvol=3.0) == 2
    assert days_in_play(pl.Series([4.0, 4.0, 4.0, 4.0]), min_rvol=3.0) == 4


def test_zero_trade_fraction_never_fills_absent_minutes() -> None:
    assert zero_trade_fraction(observed_minutes=300, expected_minutes=390) == pytest.approx(
        90 / 390
    )
    with pytest.raises(ValueError):
        zero_trade_fraction(observed_minutes=391, expected_minutes=390)


def test_corwin_schultz_is_zero_without_a_high_low_range() -> None:
    frame = pl.DataFrame({"high": [10.0] * 4, "low": [10.0] * 4})
    assert corwin_schultz_spread(frame, n=3) == pytest.approx(0.0)


def test_overnight_intraday_decomposition_recomposes_close_return() -> None:
    frame = pl.DataFrame(
        {
            "trade_date": [date(2025, 1, 1), date(2025, 1, 2)],
            "open": [100.0, 110.0],
            "close": [105.0, 121.0],
        }
    )
    result = decompose(frame)
    row = result.row(1, named=True)
    recomposed = (1 + row["r_overnight"]) * (1 + row["r_intraday"]) - 1
    assert recomposed == pytest.approx(121 / 105 - 1)


def _daily_fixture() -> tuple[pl.DataFrame, date]:
    dates = [date(2025, 1, 1) + timedelta(days=index) for index in range(270)]
    market = [100.0]
    fast = [20.0]
    low_price = [1.5]
    for index in range(1, len(dates)):
        market_return = 0.001 + ((index % 7) - 3) * 0.0007
        market.append(market[-1] * (1 + market_return))
        fast.append(fast[-1] * (1 + 2 * market_return))
        low_price.append(1.5)

    rows: list[dict[str, object]] = []
    for symbol, prices, band in (
        ("SPY", market, 0.005),
        ("FAST", fast, 0.02),
        ("LOWP", low_price, 0.01),
    ):
        for trade_date, close in zip(dates, prices, strict=True):
            rows.append(
                {
                    "symbol": symbol,
                    "trade_date": trade_date,
                    "open": close,
                    "high": close * (1 + band),
                    "low": close * (1 - band),
                    "close": close,
                    "volume": 1_000_000.0,
                }
            )
    return pl.DataFrame(rows), dates[-1] + timedelta(days=1)


def test_daily_universe_is_point_in_time_and_fails_closed_without_rvol() -> None:
    frame, trade_date = _daily_fixture()
    cfg = load_config(ROOT / "config.yaml")
    result = _build_universe_from_daily(
        frame,
        trade_date=trade_date,
        cfg=cfg,
        provenance="test.daily",
    )
    assert set(REQUIRED_UNIVERSE_COLUMNS).issubset(result.columns)
    fast = result.filter(pl.col("symbol") == "FAST").row(0, named=True)
    low_price = result.filter(pl.col("symbol") == "LOWP").row(0, named=True)
    assert fast["beta"] == pytest.approx(2.0, abs=0.02)
    assert fast["pass_gate"] is False
    assert fast["reject_reason"] == "pending:rvol,market_cap,earnings,luld"
    assert fast["rvol"] is None
    assert fast["market_cap"] is None
    assert "price_below_min" in low_price["reject_reason"]

    future = pl.DataFrame(
        {
            "symbol": ["FAST"],
            "trade_date": [trade_date],
            "open": [999.0],
            "high": [1_000.0],
            "low": [998.0],
            "close": [999.0],
            "volume": [999_999_999.0],
        }
    )
    with_future = _build_universe_from_daily(
        pl.concat([frame, future]),
        trade_date=trade_date,
        cfg=cfg,
        provenance="test.daily",
    )
    future_fast = with_future.filter(pl.col("symbol") == "FAST").row(0, named=True)
    assert future_fast["price"] == pytest.approx(fast["price"])
    assert future_fast["adv_usd"] == pytest.approx(fast["adv_usd"])


def test_candidate_reference_excludes_non_common_stock_symbols() -> None:
    frame, trade_date = _daily_fixture()
    cfg = load_config(ROOT / "config.yaml")
    result = _build_universe_from_daily(
        frame,
        trade_date=trade_date,
        cfg=cfg,
        provenance="test.daily",
        candidate_symbols={"FAST"},
        reference_provenance="test.common_stocks",
    )
    assert result.get_column("symbol").to_list() == ["FAST"]
    assert result.get_column("security_type").to_list() == ["CS"]
    assert result.get_column("security_type_provenance").to_list() == [
        "test.common_stocks"
    ]


def test_candidate_optimized_universe_matches_full_history_features() -> None:
    frame, trade_date = _daily_fixture()
    cfg = load_config(ROOT / "config.yaml")
    full = _build_universe_from_daily(
        frame,
        trade_date=trade_date,
        cfg=cfg,
        provenance="test.daily",
    ).filter(pl.col("symbol") == "FAST")
    candidate = _build_universe_from_daily(
        frame,
        trade_date=trade_date,
        cfg=cfg,
        provenance="test.daily",
        candidate_symbols={"FAST"},
        reference_provenance="test.reference",
    )

    for column in ("price", "adv_usd", "beta", "atr_pct", "max_abs_return"):
        assert candidate.get_column(column)[0] == pytest.approx(full.get_column(column)[0])


def test_daily_universe_rejects_probable_ticker_identity_discontinuity() -> None:
    frame, trade_date = _daily_fixture()
    cfg = load_config(ROOT / "config.yaml")
    reuse = (
        frame.filter(pl.col("symbol") == "FAST")
        .with_columns(
            pl.lit("REUSE").alias("symbol"),
            pl.when(pl.int_range(pl.len()) >= 200)
            .then(pl.col("close") * 20)
            .otherwise(pl.col("close"))
            .alias("close"),
        )
        .with_columns(
            pl.col("close").alias("open"),
            (pl.col("close") * 1.02).alias("high"),
            (pl.col("close") * 0.98).alias("low"),
        )
    )
    result = _build_universe_from_daily(
        pl.concat([frame, reuse]),
        trade_date=trade_date,
        cfg=cfg,
        provenance="test.daily",
    )
    row = result.filter(pl.col("symbol") == "REUSE").row(0, named=True)
    assert row["max_abs_return"] > 0.9
    assert "suspected_identity_discontinuity" in row["reject_reason"]
    assert row["precheck_pass"] is False
