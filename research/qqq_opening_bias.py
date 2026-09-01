"""Long-only QQQ opening-bias strategy; research only."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import cast

import polars as pl


@dataclass(frozen=True)
class QqqTrade:
    entry_time: datetime
    entry_px: float
    exit_time: datetime
    exit_px: float
    stop_exit_px: float
    exit_reason: str
    return_pct: float


def evaluate_qqq_opening_bias(
    bars: pl.DataFrame, *, session_open_utc: datetime
) -> QqqTrade | None:
    """Bullish first five minutes, enter next open, stop first, target 10R."""
    ordered = bars.sort("ts_utc")
    expected = [session_open_utc + timedelta(minutes=index) for index in range(6)]
    if ordered.head(6).get_column("ts_utc").to_list() != expected:
        return None
    opening = ordered.head(5)
    if float(opening.row(-1, named=True)["close"]) <= float(
        opening.row(0, named=True)["open"]
    ):
        return None
    entry_row = ordered.row(5, named=True)
    nominal_entry = float(entry_row["open"])
    entry_px = nominal_entry + 0.02
    opening_low = cast(float, opening.get_column("low").min())
    stop_exit_px = opening_low - 0.04
    if (entry_px - stop_exit_px + 0.007) / entry_px > 0.02:
        return None
    target = nominal_entry + 10 * (nominal_entry - opening_low)
    for row in ordered.slice(5).iter_rows(named=True):
        timestamp = row["ts_utc"]
        if float(row["low"]) <= opening_low:
            exit_px = stop_exit_px
            return QqqTrade(
                entry_row["ts_utc"], entry_px, timestamp + timedelta(minutes=1),
                exit_px, stop_exit_px, "2pct_all_in_stop", exit_px / entry_px - 1,
            )
        if float(row["high"]) >= target:
            exit_px = target - 0.005
            return QqqTrade(
                entry_row["ts_utc"], entry_px, timestamp + timedelta(minutes=1),
                exit_px, stop_exit_px, "10r_target", exit_px / entry_px - 1,
            )
    last = ordered.row(-1, named=True)
    exit_px = float(last["close"]) - 0.005
    return QqqTrade(
        entry_row["ts_utc"], entry_px, last["ts_utc"] + timedelta(minutes=1),
        exit_px, stop_exit_px, "end_of_day", exit_px / entry_px - 1,
    )
