from __future__ import annotations

import polars as pl


def filter_rth(bars: pl.DataFrame, calendar: pl.DataFrame) -> pl.DataFrame:
    """Keep only bars inside each XNYS session; never synthesize missing minutes."""
    required_calendar = {"trade_date", "market_open_utc", "market_close_utc"}
    missing = sorted(required_calendar - set(calendar.columns))
    if missing:
        raise ValueError(f"calendar missing columns: {missing}")

    original_columns = tuple(bars.columns)
    enriched = bars.with_columns(
        pl.col("ts_utc")
        .dt.convert_time_zone("America/New_York")
        .dt.date()
        .alias("trade_date")
    ).join(
        calendar.select("trade_date", "market_open_utc", "market_close_utc"),
        on="trade_date",
        how="left",
    )
    missing_calendar_rows = enriched.filter(pl.col("market_open_utc").is_null()).height
    if missing_calendar_rows:
        raise ValueError(f"{missing_calendar_rows} bars fall outside the supplied calendar")

    return (
        enriched.filter(
            (pl.col("ts_utc") >= pl.col("market_open_utc"))
            & (pl.col("ts_utc") < pl.col("market_close_utc"))
        )
        .select(*original_columns, "trade_date")
        .sort("symbol", "ts_utc")
    )


def minute_coverage(bars: pl.DataFrame, calendar: pl.DataFrame) -> pl.DataFrame:
    """Count observed RTH bars against the exchange schedule for every symbol/day."""
    rth = filter_rth(bars, calendar)
    counts = rth.group_by("symbol", "trade_date").agg(pl.len().alias("bar_count"))
    symbols = bars.select("symbol").unique().sort("symbol")
    expected = symbols.join(
        calendar.select("trade_date", "session_minutes"),
        how="cross",
    )
    return (
        expected.join(counts, on=("symbol", "trade_date"), how="left")
        .with_columns(pl.col("bar_count").fill_null(0))
        .with_columns(
            (pl.col("session_minutes") - pl.col("bar_count")).alias("missing_minutes"),
            (pl.col("bar_count") == pl.col("session_minutes")).alias("complete_session"),
        )
        .sort("symbol", "trade_date")
    )
