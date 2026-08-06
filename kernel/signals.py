"""Long-only, point-in-time ORB-5 signal generation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

import polars as pl


@dataclass(frozen=True)
class OrbSignal:
    symbol: str | None
    triggered: bool
    reason: str
    opening_range_high: float | None
    opening_range_low: float | None
    opening_range_open: float | None
    opening_range_close: float | None
    trigger_ts_utc: datetime | None
    entry_ts_utc: datetime | None
    entry_px: float | None
    provenance: str


@dataclass(frozen=True)
class OrbIntent:
    """Causal live decision; it never consumes the future fill bar."""

    symbol: str | None
    triggered: bool
    reason: str
    opening_range_high: float | None
    opening_range_low: float | None
    trigger_ts_utc: datetime | None
    planned_entry_ts_utc: datetime | None
    provenance: str


def orb5_intent(
    bars: pl.DataFrame,
    *,
    session_open_utc: datetime,
    decision_at_utc: datetime,
    rvol: float,
    min_rvol: float,
) -> OrbIntent:
    """Emit only a just-completed ORB breakout for immediate next-bar submission.

    A bar stamped ``t`` covers the minute beginning at ``t``. At decision time ``d``
    only bars with ``t < d`` are visible. A valid intent requires the first breakout
    to be the bar ending exactly at ``d``; replaying an older breakout fails closed.
    """

    required = {"symbol", "ts_utc", "open", "high", "low", "close"}
    missing = required - set(bars.columns)
    if missing:
        raise ValueError(f"bars missing required columns: {sorted(missing)}")
    if session_open_utc.tzinfo is None or decision_at_utc.tzinfo is None:
        raise ValueError("session_open_utc and decision_at_utc must be timezone-aware")
    if not math.isfinite(rvol) or not math.isfinite(min_rvol):
        raise ValueError("RVOL inputs must be finite")
    symbols = bars.get_column("symbol").unique().to_list()
    if len(symbols) > 1:
        raise ValueError("orb5_intent accepts one symbol at a time")
    symbol = str(symbols[0]) if symbols else None
    provenance = (
        f"kernel.signals.orb5_intent@{decision_at_utc.isoformat()}|"
        "range=[open,open+5m)|entry=submit_at_next_bar_boundary"
    )

    def result(reason: str, **values: object) -> OrbIntent:
        return OrbIntent(
            symbol=symbol,
            triggered=bool(values.get("triggered", False)),
            reason=reason,
            opening_range_high=_float_or_none(values.get("opening_range_high")),
            opening_range_low=_float_or_none(values.get("opening_range_low")),
            trigger_ts_utc=_datetime_or_none(values.get("trigger_ts_utc")),
            planned_entry_ts_utc=_datetime_or_none(values.get("planned_entry_ts_utc")),
            provenance=provenance,
        )

    if rvol <= min_rvol:
        return result("rvol_below_or_equal_min")
    range_end = session_open_utc + timedelta(minutes=5)
    if decision_at_utc < range_end + timedelta(minutes=1):
        return result("no_completed_breakout_bar")
    visible = bars.filter(
        (pl.col("ts_utc") >= session_open_utc) & (pl.col("ts_utc") < decision_at_utc)
    ).sort("ts_utc")
    if visible.get_column("ts_utc").n_unique() != visible.height:
        return result("duplicate_minute_bars")
    expected_count = int((decision_at_utc - session_open_utc).total_seconds() // 60)
    expected_times = [
        session_open_utc + timedelta(minutes=minute) for minute in range(expected_count)
    ]
    if visible.get_column("ts_utc").to_list() != expected_times:
        return result("minute_path_incomplete")
    opening = visible.head(5)
    opening_open = float(opening.get_column("open")[0])
    opening_close = float(opening.get_column("close")[-1])
    opening_high_value = opening.get_column("high").max()
    opening_low_value = opening.get_column("low").min()
    if not isinstance(opening_high_value, (int, float)) or not isinstance(
        opening_low_value, (int, float)
    ):
        raise ValueError("opening range high or low is unavailable")
    opening_high = float(opening_high_value)
    opening_low = float(opening_low_value)
    common = {"opening_range_high": opening_high, "opening_range_low": opening_low}
    if opening_close <= opening_open:
        return result("opening_range_not_bullish", **common)
    breakout = visible.filter(
        (pl.col("ts_utc") >= range_end) & (pl.col("high") > opening_high)
    ).head(1)
    if breakout.is_empty():
        return result("no_breakout_at_decision", **common)
    trigger_ts = breakout.get_column("ts_utc")[0]
    if not isinstance(trigger_ts, datetime):
        raise ValueError("trigger timestamp is invalid")
    planned_entry = trigger_ts + timedelta(minutes=1)
    if planned_entry != decision_at_utc:
        return result(
            "first_breakout_is_stale",
            trigger_ts_utc=trigger_ts,
            planned_entry_ts_utc=planned_entry,
            **common,
        )
    return result(
        "triggered",
        triggered=True,
        trigger_ts_utc=trigger_ts,
        planned_entry_ts_utc=planned_entry,
        **common,
    )


def orb5(
    bars: pl.DataFrame,
    *,
    session_open_utc: datetime,
    asof_utc: datetime,
    rvol: float,
    min_rvol: float,
) -> OrbSignal:
    """Return the first bullish ORB-5 breakout with a next-bar VWAP fill.

    ``asof_utc`` is exclusive because a minute bar stamped at that instant has only
    just started. This makes the historical and live definitions agree.
    """
    required = {"symbol", "ts_utc", "open", "high", "low", "close", "vwap"}
    missing = required - set(bars.columns)
    if missing:
        raise ValueError(f"bars missing required columns: {sorted(missing)}")
    if session_open_utc.tzinfo is None or asof_utc.tzinfo is None:
        raise ValueError("session_open_utc and asof_utc must be timezone-aware")
    if not math.isfinite(rvol) or not math.isfinite(min_rvol):
        raise ValueError("RVOL inputs must be finite")
    symbols = bars.get_column("symbol").unique().to_list()
    if len(symbols) > 1:
        raise ValueError("orb5 accepts one symbol at a time")
    symbol = str(symbols[0]) if symbols else None
    provenance = (
        f"kernel.signals.orb5@{asof_utc.isoformat()}|"
        "range=[open,open+5m)|entry=next_complete_bar_vwap"
    )

    def result(reason: str, **values: object) -> OrbSignal:
        return OrbSignal(
            symbol=symbol,
            triggered=bool(values.get("triggered", False)),
            reason=reason,
            opening_range_high=_float_or_none(values.get("opening_range_high")),
            opening_range_low=_float_or_none(values.get("opening_range_low")),
            opening_range_open=_float_or_none(values.get("opening_range_open")),
            opening_range_close=_float_or_none(values.get("opening_range_close")),
            trigger_ts_utc=_datetime_or_none(values.get("trigger_ts_utc")),
            entry_ts_utc=_datetime_or_none(values.get("entry_ts_utc")),
            entry_px=_float_or_none(values.get("entry_px")),
            provenance=provenance,
        )

    if rvol <= min_rvol:
        return result("rvol_below_or_equal_min")
    range_end = session_open_utc + timedelta(minutes=5)
    visible = bars.filter(pl.col("ts_utc") < asof_utc).sort("ts_utc")
    if visible.get_column("ts_utc").n_unique() != visible.height:
        return result("duplicate_minute_bars")
    opening = visible.filter(
        (pl.col("ts_utc") >= session_open_utc) & (pl.col("ts_utc") < range_end)
    )
    if asof_utc < range_end:
        return result("opening_range_incomplete")
    expected_opening_times = [
        session_open_utc + timedelta(minutes=minute) for minute in range(5)
    ]
    if opening.get_column("ts_utc").to_list() != expected_opening_times:
        return result("opening_range_missing_bars")
    opening_open = float(opening.get_column("open")[0])
    opening_close = float(opening.get_column("close")[-1])
    opening_high_value = opening.get_column("high").max()
    opening_low_value = opening.get_column("low").min()
    if not isinstance(opening_high_value, (int, float)) or not isinstance(
        opening_low_value, (int, float)
    ):
        raise ValueError("opening range high or low is unavailable")
    opening_high = float(opening_high_value)
    opening_low = float(opening_low_value)
    common = {
        "opening_range_high": opening_high,
        "opening_range_low": opening_low,
        "opening_range_open": opening_open,
        "opening_range_close": opening_close,
    }
    if opening_close <= opening_open:
        return result("opening_range_not_bullish", **common)
    after_range = visible.filter(pl.col("ts_utc") >= range_end)
    breakout = after_range.filter(pl.col("high") > opening_high).head(1)
    if breakout.is_empty():
        return result("no_breakout_at_asof", **common)
    trigger_ts = breakout.get_column("ts_utc")[0]
    if not isinstance(trigger_ts, datetime):
        raise ValueError("trigger timestamp is invalid")
    expected_entry_ts = trigger_ts + timedelta(minutes=1)
    if expected_entry_ts >= asof_utc:
        return result(
            "next_bar_unavailable_at_asof", trigger_ts_utc=trigger_ts, **common
        )
    next_bar = after_range.filter(pl.col("ts_utc") == expected_entry_ts)
    if next_bar.is_empty():
        return result(
            "next_minute_bar_missing", trigger_ts_utc=trigger_ts, **common
        )
    entry_ts = next_bar.get_column("ts_utc")[0]
    entry_px = next_bar.get_column("vwap")[0]
    if not isinstance(entry_ts, datetime) or not isinstance(entry_px, (int, float)):
        return result("next_bar_vwap_unavailable", trigger_ts_utc=trigger_ts, **common)
    return result(
        "triggered",
        triggered=True,
        trigger_ts_utc=trigger_ts,
        entry_ts_utc=entry_ts,
        entry_px=float(entry_px),
        **common,
    )


def _float_or_none(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _datetime_or_none(value: object) -> datetime | None:
    return value if isinstance(value, datetime) else None
