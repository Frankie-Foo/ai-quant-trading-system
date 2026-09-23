from __future__ import annotations

from datetime import UTC, date, datetime

import polars as pl
import pytest

from scripts.backfill_loop_outcome_daily import (
    _symbols_from_assignments,
    _symbols_from_outbox,
    normalize_alpaca_daily_bars,
)


def _bars(ts: datetime) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": ["SPY"],
            "ts_utc": [ts],
            "open": [100.0],
            "high": [102.0],
            "low": [99.0],
            "close": [101.0],
            "volume": [1000],
            "trade_count": [100],
            "vwap": [100.5],
            "source": ["alpaca.sip.rest.bars.1day"],
            "feed": ["sip"],
            "adjustment": ["split_adjusted"],
        }
    )


def test_alpaca_daily_backfill_uses_new_york_session_date_and_truthful_source() -> None:
    frame = normalize_alpaca_daily_bars(
        _bars(datetime(2026, 9, 22, 13, 30, tzinfo=UTC)),
        trade_date=date(2026, 9, 22),
    )

    assert frame["trade_date"].item() == date(2026, 9, 22)
    assert frame["provider_ts_utc"].item() == datetime(2026, 9, 22, 13, 30, tzinfo=UTC)
    assert frame["source"].item() == "alpaca.sip.daily_event_session"
    assert frame["feed"].item() == "sip"
    assert frame["adjustment"].item() == "split_adjusted"


def test_alpaca_daily_backfill_rejects_bars_outside_requested_session() -> None:
    with pytest.raises(ValueError, match="no Alpaca daily bars for requested XNYS session"):
        normalize_alpaca_daily_bars(
            _bars(datetime(2026, 9, 23, 13, 30, tzinfo=UTC)),
            trade_date=date(2026, 9, 22),
        )


def test_backfill_symbols_are_derived_read_only_from_pending_outcomes(tmp_path) -> None:
    import json
    import sqlite3

    path = tmp_path / "outbox.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE loop_outbox (event_type TEXT, status TEXT, payload_json TEXT)"
        )
        connection.executemany(
            "INSERT INTO loop_outbox VALUES (?, ?, ?)",
            [
                (
                    "outcome",
                    "pending",
                    json.dumps({"instrument": "aapl", "evidence": {"benchmark_id": "spy"}}),
                ),
                (
                    "daily_review",
                    "pending",
                    json.dumps({"instrument": "SHOULD_NOT_INCLUDE"}),
                ),
                (
                    "outcome",
                    "delivered",
                    json.dumps({"instrument": "ALREADY_DELIVERED"}),
                ),
            ],
        )

    assert _symbols_from_outbox(path) == ("AAPL", "SPY")


def test_backfill_symbols_include_loop_assignments_and_benchmark() -> None:
    class Assignment:
        def __init__(self, instrument: str):
            self.instrument = instrument

    assert _symbols_from_assignments(
        (Assignment("AAPL"), Assignment("MSFT"), Assignment("AAPL")), "SPY"
    ) == ("AAPL", "MSFT", "SPY")
