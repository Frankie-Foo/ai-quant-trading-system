"""Long-only triple-barrier labels."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

import polars as pl


@dataclass(frozen=True)
class BarrierEvent:
    which: Literal["tp", "sl", "time"]
    exit_ts: datetime
    exit_px: float
    ret: float
    provenance: str


def triple_barrier(
    df_1m: pl.DataFrame,
    entry_ts: datetime,
    entry_px: float,
    tp_px: float,
    sl_px: float,
    time_stop: datetime,
) -> BarrierEvent:
    """Scan bars after entry; if both price barriers hit together, SL wins."""
    required = {"ts_utc", "high", "low", "close"}
    missing = required - set(df_1m.columns)
    if missing:
        raise ValueError(f"minute bars missing required columns: {sorted(missing)}")
    if entry_ts.tzinfo is None or time_stop.tzinfo is None:
        raise ValueError("entry and time-stop timestamps must be timezone-aware")
    if time_stop <= entry_ts:
        raise ValueError("time_stop must be after entry_ts")
    if not all(math.isfinite(value) and value > 0 for value in (entry_px, tp_px, sl_px)):
        raise ValueError("barrier prices must be finite and positive")
    if not sl_px < entry_px < tp_px:
        raise ValueError("long barriers require sl < entry < tp")
    future = df_1m.filter(
        (pl.col("ts_utc") > entry_ts) & (pl.col("ts_utc") <= time_stop)
    ).sort("ts_utc")
    if future.is_empty():
        raise ValueError("no post-entry bar is available through the time stop")
    provenance = (
        f"kernel.labels.triple_barrier@{entry_ts.isoformat()}|"
        "same_bar=sl_first|entry_bar_excluded"
    )
    expected_timestamp = entry_ts + timedelta(minutes=1)
    for row in future.iter_rows(named=True):
        timestamp = row["ts_utc"]
        if not isinstance(timestamp, datetime):
            raise ValueError("bar timestamp is invalid")
        if timestamp != expected_timestamp:
            raise ValueError(
                f"missing post-entry minute bar at {expected_timestamp.isoformat()}"
            )
        expected_timestamp += timedelta(minutes=1)
        hit_sl = float(row["low"]) <= sl_px
        hit_tp = float(row["high"]) >= tp_px
        if hit_sl:
            return BarrierEvent("sl", timestamp, sl_px, sl_px / entry_px - 1, provenance)
        if hit_tp:
            return BarrierEvent("tp", timestamp, tp_px, tp_px / entry_px - 1, provenance)
        if timestamp >= time_stop:
            exit_px = float(row["close"])
            return BarrierEvent(
                "time", timestamp, exit_px, exit_px / entry_px - 1, provenance
            )
    raise ValueError(
        f"missing post-entry minute bar at {expected_timestamp.isoformat()}"
    )
