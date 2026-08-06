from __future__ import annotations

from datetime import UTC, date
from importlib.metadata import version

import pandas_market_calendars as mcal
import polars as pl

CALENDAR_SCHEMA_VERSION = "exchange_calendar.v1"


def build_xnys_schedule(start_date: date, end_date: date) -> pl.DataFrame:
    """Build the NYSE schedule, including early closes, with UTC timestamps."""
    if end_date < start_date:
        raise ValueError("end_date must be on or after start_date")

    schedule = mcal.get_calendar("NYSE").schedule(
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
    )
    package_version = version("pandas-market-calendars")
    rows: list[dict[str, object]] = []
    for trade_date, row in schedule.iterrows():
        market_open = row["market_open"].to_pydatetime().astimezone(UTC)
        market_close = row["market_close"].to_pydatetime().astimezone(UTC)
        session_minutes = int((market_close - market_open).total_seconds() // 60)
        rows.append(
            {
                "trade_date": trade_date.date(),
                "market_open_utc": market_open,
                "market_close_utc": market_close,
                "session_minutes": session_minutes,
                "is_half_day": session_minutes < 390,
                "source": "pandas_market_calendars.NYSE",
                "source_version": package_version,
            }
        )

    return pl.DataFrame(
        rows,
        schema={
            "trade_date": pl.Date,
            "market_open_utc": pl.Datetime("ms", "UTC"),
            "market_close_utc": pl.Datetime("ms", "UTC"),
            "session_minutes": pl.Int64,
            "is_half_day": pl.Boolean,
            "source": pl.String,
            "source_version": pl.String,
        },
    )
