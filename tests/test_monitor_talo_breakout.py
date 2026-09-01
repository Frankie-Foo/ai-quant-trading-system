from datetime import UTC, datetime, time, timedelta

import polars as pl

from scripts.monitor_talo_breakout import (
    TaloConfig,
    _evaluate,
    _session_vwap,
)

NOW = datetime(2026, 8, 5, 14, 20, 30, tzinfo=UTC)
CONFIG = TaloConfig(
    symbol="TALO",
    trigger=15.26,
    hard_stop=15.20,
    scout_shares=600,
    full_shares=1900,
    poll_seconds=15,
    max_spread_ratio=0.003,
    max_chase_ratio=0.005,
    volume_multiple=1.5,
    no_new_high_seconds=600,
    entry_deadline_et=time(11, 30),
    channel_id="channel",
)


def _bars(*, closes: list[float], highs: list[float] | None = None) -> pl.DataFrame:
    highs = highs or closes
    timestamps = [
        NOW.replace(second=0, microsecond=0) - timedelta(minutes=len(closes) - i)
        for i in range(len(closes))
    ]
    return pl.DataFrame(
        {
            "symbol": ["TALO"] * len(closes),
            "ts_utc": timestamps,
            "open": closes,
            "high": highs,
            "low": [close - 0.02 for close in closes],
            "close": closes,
            "volume": [100_000] * (len(closes) - 1) + [200_000],
            "vwap": closes,
        }
    )


def _quote(*, bid: float = 15.30, ask: float = 15.31) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": ["TALO"],
            "ts_utc": [NOW - timedelta(seconds=5)],
            "bid_price": [bid],
            "ask_price": [ask],
        }
    )


def test_session_vwap_uses_volume_weighted_prices() -> None:
    bars = _bars(closes=[15.0, 15.2, 15.4, 15.3, 15.5, 15.6])
    assert _session_vwap(bars) == 15.371428571428572


def test_two_closes_above_trigger_emit_scout_signal() -> None:
    bars = _bars(
        closes=[15.0, 15.0, 15.0, 15.0, 15.27, 15.30],
        highs=[15.02, 15.02, 15.02, 15.02, 15.28, 15.34],
    )
    signals = _evaluate(
        CONFIG,
        {"phase": "watching", "notified": {}},
        bars,
        _quote(),
        now_utc=NOW,
        coverage_usable=True,
    )
    assert [signal.event for signal in signals] == ["scout"]
    assert "600" in signals[0].message


def test_hard_stop_blocks_follow_up_signal() -> None:
    bars = _bars(closes=[15.0, 15.0, 15.0, 15.0, 15.27, 15.18])
    signals = _evaluate(
        CONFIG,
        {
            "phase": "scout",
            "scout_alerted": True,
            "scout_alerted_at_utc": (NOW - timedelta(minutes=2)).isoformat(),
            "breakout_high": 15.34,
        },
        bars,
        _quote(bid=15.18, ask=15.19),
        now_utc=NOW,
        coverage_usable=True,
    )
    assert [signal.reason for signal in signals] == ["hard_stop"]


def test_hard_stop_still_works_when_bar_coverage_is_degraded() -> None:
    signals = _evaluate(
        CONFIG,
        {
            "phase": "scout",
            "scout_alerted": True,
        },
        pl.DataFrame(),
        _quote(bid=15.18, ask=15.19),
        now_utc=NOW,
        coverage_usable=False,
    )
    assert [signal.reason for signal in signals] == ["hard_stop"]
