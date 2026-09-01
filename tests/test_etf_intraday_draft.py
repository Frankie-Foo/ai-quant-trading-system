from datetime import UTC, datetime, timedelta

import polars as pl

from research.etf_intraday_draft import DraftConfig, FiveBar, find_trades
from scripts.run_etf_intraday_draft_backtest import _aggregate_five

OPEN = datetime(2026, 8, 18, 13, 30, tzinfo=UTC)


def _bar(index: int, **changes: float) -> FiveBar:
    values = {
        "open": 99.8,
        "high": 100.0,
        "low": 99.7,
        "close": 99.9,
        "volume": 100.0,
        "vwap": 99.8,
        "ema9": 99.8,
        "ema20": 99.7,
        "ema50": 99.6,
    }
    values.update(changes)
    return FiveBar(ts_utc=OPEN + timedelta(minutes=5 * index), **values)


def test_orb_uses_completed_signal_then_next_bar_open() -> None:
    bars = [_bar(index) for index in range(25)]
    bars[20] = _bar(
        20, open=99.9, high=100.5, low=99.95, close=100.4, volume=160, vwap=99.9
    )
    bars[21] = _bar(21, open=100.5, high=101.8, low=100.2, close=101.5, volume=200)
    benchmark = [_bar(index, close=100, vwap=99.9) for index in range(25)]

    trades = find_trades(
        "SPY", bars, benchmark, session_open_utc=OPEN, config=DraftConfig(),
        allow_pullback=False,
    )

    assert trades[0].signal_ts_utc == bars[21].ts_utc
    assert trades[0].entry_ts_utc == bars[21].ts_utc
    assert trades[0].entry_px > bars[21].open


def test_pullback_requires_reclaim_before_next_bar_entry() -> None:
    bars = [
        _bar(index, ema20=99 + index * 0.02, ema50=98 + index * 0.01)
        for index in range(18)
    ]
    bars[13] = _bar(
        13, open=100.0, high=100.5, low=99.8, close=100.4, volume=130,
        vwap=99.9, ema9=100.0, ema20=99.8, ema50=98.8,
    )
    bars[14] = _bar(14, open=100.45, high=101.8, low=100.2, close=101.4, volume=150)
    benchmark = [_bar(index, close=100, vwap=99.9) for index in range(18)]

    trades = find_trades(
        "QQQ", bars, benchmark, session_open_utc=OPEN, config=DraftConfig(),
        allow_orb=False,
    )

    assert trades[0].strategy == "vwap_trend_pullback"
    assert trades[0].entry_ts_utc == bars[14].ts_utc


def test_five_minute_aggregation_uses_completed_consecutive_minutes() -> None:
    minute = pl.DataFrame(
        {
            "ts_utc": [OPEN + timedelta(minutes=index) for index in range(5)],
            "open": [100.0] * 5,
            "high": [101.0] * 5,
            "low": [99.0] * 5,
            "close": [100.5] * 5,
            "volume": [10] * 5,
            "vwap": [100.2] * 5,
        }
    )

    bars = _aggregate_five(minute, OPEN)

    assert len(bars) == 1
    assert bars[0][5] == 50
