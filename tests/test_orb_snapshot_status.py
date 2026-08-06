from __future__ import annotations

from datetime import UTC, datetime, timedelta

import polars as pl

from scripts.build_orb5_signals import _snapshot_status

OPEN = datetime(2026, 7, 21, 13, 30, tzinfo=UTC)
CLOSE = OPEN + timedelta(hours=6, minutes=30)


def test_intraday_pending_fill_is_not_reported_as_a_complete_no_trade_session() -> None:
    signals = pl.DataFrame(
        {
            "symbol": ["AMC", "GREE"],
            "triggered": [False, False],
            "reason": ["next_bar_unavailable_at_asof", "no_breakout_at_asof"],
        }
    )

    assert _snapshot_status(signals, query_end=OPEN + timedelta(minutes=7), market_close=CLOSE) == (
        "in_progress_pending_confirmation"
    )


def test_intraday_no_breakout_is_explicitly_in_progress_until_close() -> None:
    signals = pl.DataFrame(
        {
            "symbol": ["GREE"],
            "triggered": [False],
            "reason": ["no_breakout_at_asof"],
        }
    )

    assert _snapshot_status(signals, query_end=OPEN + timedelta(minutes=7), market_close=CLOSE) == (
        "in_progress_no_trigger_yet"
    )
    assert _snapshot_status(signals, query_end=CLOSE, market_close=CLOSE) == "complete_session"
