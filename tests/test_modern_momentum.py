from __future__ import annotations

from datetime import UTC, datetime, timedelta

import polars as pl

from research.modern_momentum import (
    ModernMomentumConfig,
    ModernMomentumTrade,
    evaluate_modern_momentum,
    evaluate_modern_momentum_reentry,
)


def _bars(*, breakout_low: float = 100.35) -> pl.DataFrame:
    opened = datetime(2026, 8, 17, 13, 30, tzinfo=UTC)
    rows: list[dict[str, object]] = []
    for minute in range(90):
        close = 99.4 + min(minute, 14) * 0.035
        if 15 <= minute < 26:
            close = 99.85
        if minute >= 26:
            close = 100.45 + (minute - 26) * 0.08
        low = close - 0.08
        if minute >= 26 and breakout_low < 100:
            low = breakout_low
        rows.append(
            {
                "symbol": "TEST",
                "ts_utc": opened + timedelta(minutes=minute),
                "open": close - 0.02,
                "high": close + 0.08,
                "low": low,
                "close": close,
                "volume": 10_000,
            }
        )
    return pl.DataFrame(rows)


def test_modern_momentum_requires_causal_breakout_and_caps_all_in_stop() -> None:
    opened = datetime(2026, 8, 17, 13, 30, tzinfo=UTC)
    trade = evaluate_modern_momentum(
        _bars(),
        session_open_utc=opened,
        prior_close=96.0,
        market_cap=2_000_000_000,
        premarket_rvol=2.0,
        config=ModernMomentumConfig(),
    )

    assert trade is not None
    assert trade.entry_ts_utc >= opened + timedelta(minutes=27)
    assert trade.target_level > trade.entry_px
    assert trade.all_in_stop_pct <= 0.02
    assert trade.all_in_stop_pct >= 0.005
    assert trade.production_eligible is False


def test_modern_momentum_rejects_wide_structural_stop() -> None:
    opened = datetime(2026, 8, 17, 13, 30, tzinfo=UTC)
    trade = evaluate_modern_momentum(
        _bars(breakout_low=97.0),
        session_open_utc=opened,
        prior_close=96.0,
        market_cap=2_000_000_000,
        premarket_rvol=2.0,
        config=ModernMomentumConfig(),
    )

    assert trade is None


def test_modern_momentum_rejects_entry_spread_above_ten_basis_points() -> None:
    opened = datetime(2026, 8, 17, 13, 30, tzinfo=UTC)

    trade = evaluate_modern_momentum(
        _bars(),
        session_open_utc=opened,
        prior_close=96.0,
        market_cap=2_000_000_000,
        premarket_rvol=2.0,
        config=ModernMomentumConfig(),
        relative_spread=0.0011,
    )

    assert trade is None


def test_modern_momentum_reentry_waits_for_three_completed_five_minute_bars() -> None:
    opened = datetime(2026, 8, 18, 13, 30, tzinfo=UTC)
    rows: list[dict[str, object]] = []
    for minute in range(100):
        close = 30.0
        low = 29.95
        high = 30.05
        volume = 10_000
        if 80 <= minute < 85:
            close, low, high, volume = 30.70, 30.40, 31.00, 20_000
        elif 85 <= minute < 90:
            close, low, high, volume = 31.20, 30.95, 31.24, 15_000
        elif 90 <= minute < 95:
            close, low, high, volume = 31.52, 31.14, 31.56, 18_000
        elif minute == 95:
            close, low, high, volume = 31.52, 31.50, 31.60, 20_000
        elif minute > 95:
            close, low, high, volume = 33.10, 31.50, 33.20, 20_000
        rows.append(
            {
                "symbol": "TEST",
                "ts_utc": opened + timedelta(minutes=minute),
                "open": close,
                "high": high,
                "low": low,
                "close": close,
                "vwap": close,
                "volume": volume,
            }
        )
    first_trade = ModernMomentumTrade(
        "TEST",
        opened + timedelta(minutes=70),
        opened + timedelta(minutes=71),
        31.0,
        30.5,
        32.5,
        opened + timedelta(minutes=79),
        30.5,
        "stop",
        0.02,
        30.69,
        0.1,
        2.0,
    )

    trade = evaluate_modern_momentum_reentry(
        pl.DataFrame(rows),
        session_open_utc=opened,
        first_trade=first_trade,
        config=ModernMomentumConfig(),
        relative_spread=0.001,
    )

    assert trade is not None
    assert trade.entry_ts_utc == opened + timedelta(minutes=95)
    assert trade.exit_reason == "target_3r"
    assert trade.all_in_stop_pct <= 0.02
