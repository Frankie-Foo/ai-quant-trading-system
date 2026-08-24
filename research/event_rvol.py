"""Exact point-in-time premarket RVOL for event-gap candidates."""

from __future__ import annotations

from datetime import date

import polars as pl

HISTORY_SESSIONS = 20
MIN_RVOL = 1.5
MAX_DAILY_CANDIDATES = 10


def build_event_rvol_cohort(
    gap: pl.DataFrame,
    history_bars: pl.DataFrame,
    *,
    schedule: pl.DataFrame,
) -> pl.DataFrame:
    volumes = {
        (row["session_date"], row["symbol"]): float(row["volume"])
        for row in history_bars.with_columns(
            pl.col("ts_utc")
            .dt.convert_time_zone("America/New_York")
            .dt.date()
            .alias("session_date")
        )
        .group_by("session_date", "symbol")
        .agg(pl.col("volume").sum())
        .iter_rows(named=True)
    }
    sessions = schedule.get_column("trade_date").to_list()
    positions = {value: index for index, value in enumerate(sessions)}
    rows: list[dict[str, object]] = []
    for row in gap.iter_rows(named=True):
        target = row["session_date"]
        symbol = str(row["symbol"])
        if not isinstance(target, date) or target not in positions:
            continue
        position = positions[target]
        prior = sessions[max(0, position - HISTORY_SESSIONS) : position]
        if len(prior) != HISTORY_SESSIONS:
            continue
        average = sum(volumes.get((session, symbol), 0.0) for session in prior) / len(prior)
        if average <= 0:
            continue
        enriched = dict(row)
        enriched["premarket_avg_volume20"] = average
        enriched["premarket_rvol"] = float(row["premarket_volume"]) / average
        rows.append(enriched)
    if not rows:
        return pl.DataFrame()
    return (
        pl.DataFrame(rows, infer_schema_length=None)
        .filter(pl.col("premarket_rvol") >= MIN_RVOL)
        .sort(
            "session_date",
            "catalyst_tier",
            "premarket_gap_return",
            "premarket_rvol",
            descending=[False, True, True, True],
        )
        .group_by("session_date", maintain_order=True)
        .head(MAX_DAILY_CANDIDATES)
        .with_columns(
            pl.int_range(1, pl.len() + 1).over("session_date").alias("selection_rank")
        )
        .sort("session_date", "selection_rank")
    )
