from __future__ import annotations

from datetime import UTC, datetime, timedelta

import polars as pl

from research.qqq_opening_bias import evaluate_qqq_opening_bias

OPEN = datetime(2026, 8, 17, 13, 30, tzinfo=UTC)


def test_qqq_opening_bias_uses_ten_r_target() -> None:
    rows = []
    for minute in range(20):
        close = 100 + minute * 0.5
        rows.append(
            {
                "ts_utc": OPEN + timedelta(minutes=minute),
                "open": 100 + max(0, minute - 1) * 0.5,
                "high": close + 1,
                "low": 99.9 + minute * 0.4,
                "close": close,
            }
        )
    rows[10]["high"] = 120.0
    rows[5]["open"] = 100.5
    trade = evaluate_qqq_opening_bias(pl.DataFrame(rows), session_open_utc=OPEN)

    assert trade is not None
    assert trade.exit_reason == "10r_target"


def test_qqq_opening_bias_rejects_stop_beyond_two_percent() -> None:
    rows = [
        {
            "ts_utc": OPEN + timedelta(minutes=minute),
            "open": 100.0,
            "high": 101.0,
            "low": 97.0 if minute < 5 else 99.0,
            "close": 100.5,
        }
        for minute in range(6)
    ]

    assert evaluate_qqq_opening_bias(pl.DataFrame(rows), session_open_utc=OPEN) is None
