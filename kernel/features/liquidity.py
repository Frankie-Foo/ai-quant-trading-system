"""Point-in-time liquidity features shared by research and daily selection."""

from __future__ import annotations

import math

import polars as pl


def average_dollar_volume(daily: pl.DataFrame, n: int = 20) -> float:
    """Return trailing mean close times volume from the rows supplied by the caller.

    The function deliberately has no date filtering of its own.  The universe builder
    passes only rows strictly earlier than the target trading date, which keeps the
    same calculation usable in both backtests and the daily run.
    """
    if n <= 0:
        raise ValueError("n must be positive")
    missing = {"close", "volume"} - set(daily.columns)
    if missing:
        raise ValueError(f"missing required columns: {sorted(missing)}")
    if daily.is_empty():
        return math.nan

    values = daily.tail(n).select((pl.col("close") * pl.col("volume")).mean()).item()
    return float(values) if values is not None else math.nan


def turnover(dollar_volume: float, market_cap: float | None) -> float:
    """Return dollar turnover, or NaN when market capitalisation is unavailable."""
    if market_cap is None or not math.isfinite(market_cap) or market_cap <= 0:
        return math.nan
    return dollar_volume / market_cap


def zero_trade_fraction(*, observed_minutes: int, expected_minutes: int) -> float:
    """Return the explicit no-emitted-bar share for a completed provider request."""
    if expected_minutes <= 0:
        raise ValueError("expected_minutes must be positive")
    if observed_minutes < 0 or observed_minutes > expected_minutes:
        raise ValueError("observed_minutes must be in [0, expected_minutes]")
    return (expected_minutes - observed_minutes) / expected_minutes


def corwin_schultz_spread(daily: pl.DataFrame, n: int = 20) -> float:
    """Estimate the trailing proportional bid-ask spread from daily high/low pairs."""
    if n <= 0:
        raise ValueError("n must be positive")
    missing = {"high", "low"} - set(daily.columns)
    if missing:
        raise ValueError(f"missing required columns: {sorted(missing)}")
    frame = daily.tail(n + 1)
    if frame.height < 2:
        return math.nan
    highs = frame.get_column("high").to_list()
    lows = frame.get_column("low").to_list()
    spreads: list[float] = []
    denominator = 3 - 2 * math.sqrt(2)
    for index in range(1, frame.height):
        high_previous = float(highs[index - 1])
        high_current = float(highs[index])
        low_previous = float(lows[index - 1])
        low_current = float(lows[index])
        if min(high_previous, high_current, low_previous, low_current) <= 0:
            raise ValueError("daily high/low prices must be positive")
        beta_value = math.log(high_previous / low_previous) ** 2 + math.log(
            high_current / low_current
        ) ** 2
        gamma = math.log(max(high_previous, high_current) / min(low_previous, low_current)) ** 2
        alpha = (
            (math.sqrt(2 * beta_value) - math.sqrt(beta_value)) / denominator
            - math.sqrt(gamma / denominator)
        )
        exponential = math.exp(alpha)
        spreads.append(max(0.0, 2 * (exponential - 1) / (1 + exponential)))
    return sum(spreads) / len(spreads)
