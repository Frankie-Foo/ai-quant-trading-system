from datetime import UTC, date, datetime

import polars as pl

from scripts.backfill_etf_intraday import _rth_only


def test_rth_filter_respects_early_close() -> None:
    bars = pl.DataFrame(
        {
            "symbol": ["SPY", "SPY", "SPY"],
            "ts_utc": [
                datetime(2026, 7, 3, 13, 29, tzinfo=UTC),
                datetime(2026, 7, 3, 13, 30, tzinfo=UTC),
                datetime(2026, 7, 3, 17, 0, tzinfo=UTC),
            ],
            "close": [1.0, 2.0, 3.0],
        }
    )
    schedule = pl.DataFrame(
        {
            "trade_date": [date(2026, 7, 3)],
            "market_open_utc": [datetime(2026, 7, 3, 13, 30, tzinfo=UTC)],
            "market_close_utc": [datetime(2026, 7, 3, 17, 0, tzinfo=UTC)],
        }
    )

    filtered = _rth_only(bars, schedule)

    assert filtered["close"].to_list() == [2.0]
