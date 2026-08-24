from __future__ import annotations

from datetime import UTC, datetime, timedelta

import polars as pl

from research.open_source_orb import (
    OrbConfig,
    evaluate_orb,
    evaluate_stock_in_play_orb,
)

OPEN = datetime(2026, 8, 17, 13, 30, tzinfo=UTC)


def _bars(*, stop_and_target_same_bar: bool = False) -> pl.DataFrame:
    rows: list[dict[str, object]] = []
    previous = 100.0
    for minute in range(90):
        bucket = minute // 5
        close = 100.0 + 0.1 * bucket if bucket < 6 else 101.2
        high = max(previous, close) + 0.05
        low = min(previous, close) - 0.05
        if bucket == 7 and stop_and_target_same_bar:
            high, low = 103.0, 98.0
        rows.append(
            {
                "symbol": "TEST",
                "ts_utc": OPEN + timedelta(minutes=minute),
                "open": previous,
                "high": high,
                "low": low,
                "close": close,
                "volume": 1_000,
                "vwap": (previous + close) / 2,
            }
        )
        previous = close
    return pl.DataFrame(rows).with_columns(
        pl.col("ts_utc").cast(pl.Datetime("us", "UTC"))
    )


def test_orb_enters_on_first_close_above_h30() -> None:
    trade = evaluate_orb(_bars(), session_open_utc=OPEN, config=OrbConfig())

    assert trade is not None
    assert trade.entry_ts_utc == OPEN + timedelta(minutes=35)
    assert trade.production_eligible is False


def test_orb_resolves_ambiguous_bar_stop_first() -> None:
    trade = evaluate_orb(
        _bars(stop_and_target_same_bar=True),
        session_open_utc=OPEN,
        config=OrbConfig(target_r=2.0),
    )

    assert trade is not None
    assert trade.exit_reason == "stop"


def test_system_stop_is_capped_at_two_percent_all_in() -> None:
    trade = evaluate_orb(
        _bars(stop_and_target_same_bar=True),
        session_open_utc=OPEN,
        config=OrbConfig(max_price_stop_pct=0.015),
    )

    assert trade is not None
    assert trade.return_pct >= -0.0201


def test_system_target_uses_capped_risk_not_full_opening_range() -> None:
    trade = evaluate_orb(
        _bars(),
        session_open_utc=OPEN,
        config=OrbConfig(target_r=2.0, max_price_stop_pct=0.015),
    )

    assert trade is not None
    effective_risk = trade.entry_px - trade.stop_level
    assert abs(trade.target_level - (trade.entry_px + 2 * effective_risk)) < 1e-9


def test_stock_in_play_long_branch_requires_bullish_opening_bar() -> None:
    bearish = _bars().with_columns(
        pl.when(pl.col("ts_utc") < OPEN + timedelta(minutes=5))
        .then(pl.lit(99.0))
        .otherwise(pl.col("close"))
        .alias("close")
    )

    trade = evaluate_stock_in_play_orb(
        bearish, session_open_utc=OPEN, atr_dollars=4.0
    )

    assert trade is None
