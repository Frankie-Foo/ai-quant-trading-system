"""Fixed 10:00 soft-rejection recovery rule; research only."""

from __future__ import annotations

from datetime import datetime, timedelta

import polars as pl

from research.h30_challenger import _five_minute_bars

MIN_CATALYST_TIER = 2
MIN_RELATIVE_SPY = 0.01
MIN_CLOSE_LOCATION = 0.70


def h30_recovery_features(
    bars: pl.DataFrame,
    spy_bars: pl.DataFrame,
    *,
    session_open_utc: datetime,
) -> dict[str, float | bool] | None:
    """Use only six complete five-minute bars ending at 10:00 ET."""
    fives = _five_minute_bars(bars, session_open_utc=session_open_utc)
    spy_fives = _five_minute_bars(spy_bars, session_open_utc=session_open_utc)
    expected = [session_open_utc + timedelta(minutes=5 * index) for index in range(6)]
    if len(fives) < 6 or len(spy_fives) < 6:
        return None
    if [bar.ts_utc for bar in fives[:6]] != expected:
        return None
    opening = fives[:6]
    last = opening[-1]
    spy_last = spy_fives[5]
    h30 = max(bar.high for bar in opening)
    l30 = min(bar.low for bar in opening)
    return {
        "h30_return": last.close / opening[0].open - 1,
        "h30_relative_spy": (
            last.close / opening[0].open - spy_last.close / spy_fives[0].open
        ),
        "h30_close_location": (last.close - l30) / (h30 - l30) if h30 > l30 else 0.0,
        "h30_above_vwap": last.close > last.session_vwap,
    }


def recovery_reasons(
    catalyst_tier: int, features: dict[str, float | bool]
) -> tuple[str, ...]:
    reasons: list[str] = []
    if catalyst_tier < MIN_CATALYST_TIER:
        reasons.append("catalyst_tier_below_2")
    if float(features["h30_return"]) <= 0:
        reasons.append("h30_return_not_positive")
    if float(features["h30_relative_spy"]) < MIN_RELATIVE_SPY:
        reasons.append("relative_spy_below_1pct")
    if float(features["h30_close_location"]) < MIN_CLOSE_LOCATION:
        reasons.append("h30_close_location_below_70pct")
    if features["h30_above_vwap"] is not True:
        reasons.append("h30_not_above_vwap")
    return tuple(reasons)
