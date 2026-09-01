"""Causal daily-liquidity prefilter for the current event cohort."""

from __future__ import annotations

import polars as pl

MIN_PRICE = 2.0
MIN_ADV20_USD = 20_000_000.0
MIN_HISTORY_SESSIONS = 10


def daily_event_prefilter(
    cohort: pl.DataFrame,
    daily_bars: pl.DataFrame,
) -> pl.DataFrame:
    """Join only prior-session price and trailing liquidity to event symbol-days."""
    daily = (
        daily_bars.with_columns(
            pl.col("ts_utc")
            .dt.convert_time_zone("America/New_York")
            .dt.date()
            .alias("bar_date"),
            (pl.col("close") * pl.col("volume")).alias("dollar_volume"),
        )
        .sort("symbol", "bar_date")
        .with_columns(
            pl.col("close").shift(1).over("symbol").alias("prior_close"),
            pl.col("dollar_volume")
            .rolling_mean(20, min_samples=MIN_HISTORY_SESSIONS)
            .shift(1)
            .over("symbol")
            .alias("prior_adv20_usd"),
            pl.col("volume")
            .rolling_mean(20, min_samples=MIN_HISTORY_SESSIONS)
            .shift(1)
            .over("symbol")
            .alias("prior_avg_volume20"),
        )
        .select(
            pl.col("bar_date").alias("session_date"),
            "symbol",
            "prior_close",
            "prior_adv20_usd",
            "prior_avg_volume20",
        )
    )
    return (
        cohort.join(daily, on=("session_date", "symbol"), how="left", validate="1:1")
        .filter(
            (pl.col("catalyst_tier") >= 1)
            & (pl.col("prior_close") >= MIN_PRICE)
            & (pl.col("prior_adv20_usd") >= MIN_ADV20_USD)
        )
        .sort("session_date", "catalyst_tier", "prior_adv20_usd", descending=[False, True, True])
    )
