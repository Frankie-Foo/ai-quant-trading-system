from datetime import UTC, date, datetime

import polars as pl
import pytest

from scripts.build_event_premarket_gap import build_gap_cohort


def test_gap_cohort_uses_last_available_premarket_bar() -> None:
    prefilter = pl.DataFrame(
        {
            "session_date": [date(2026, 8, 18)],
            "symbol": ["TEST"],
            "prior_close": [100.0],
        }
    )
    bars = pl.DataFrame(
        {
            "symbol": ["TEST", "TEST"],
            "ts_utc": [
                datetime(2026, 8, 18, 11, 0, tzinfo=UTC),
                datetime(2026, 8, 18, 12, 0, tzinfo=UTC),
            ],
            "high": [103.0, 106.0],
            "low": [101.0, 103.0],
            "close": [102.0, 105.0],
            "volume": [1_000, 2_000],
            "vwap": [102.0, 104.5],
        }
    )

    result = build_gap_cohort(prefilter, bars)

    assert result.height == 1
    assert result.row(0, named=True)["premarket_gap_return"] == pytest.approx(0.05)
