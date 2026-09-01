from datetime import UTC, date, datetime, timedelta

import polars as pl

from research.event_prefilter import daily_event_prefilter


def test_daily_prefilter_never_uses_target_day_volume() -> None:
    start = datetime(2026, 7, 1, 4, 0, tzinfo=UTC)
    rows = []
    for index in range(12):
        rows.append(
            {
                "symbol": "TEST",
                "ts_utc": start + timedelta(days=index),
                "close": 10.0,
                "volume": 3_000_000 if index < 11 else 1,
            }
        )
    target = date(2026, 7, 12)
    cohort = pl.DataFrame(
        {"session_date": [target], "symbol": ["TEST"], "catalyst_tier": [2]}
    )

    result = daily_event_prefilter(cohort, pl.DataFrame(rows))

    assert result.height == 1
    assert result.row(0, named=True)["prior_adv20_usd"] == 30_000_000.0
