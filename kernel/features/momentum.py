"""Point-in-time daily momentum and elasticity features."""

from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl

NEW_YORK = ZoneInfo("America/New_York")
PREMARKET_START_ET = time(4, 0)
RVOL_COLUMNS = (
    "symbol",
    "session_date",
    "premarket_start_utc",
    "data_cutoff_utc",
    "last_included_minute_utc",
    "current_premarket_volume",
    "median_historical_premarket_volume",
    "history_session_count",
    "historical_nonzero_sessions",
    "rvol",
    "rvol_pass",
    "premarket_open",
    "premarket_high",
    "premarket_low",
    "premarket_close",
    "premarket_vwap",
    "premarket_return",
    "premarket_close_location",
    "premarket_above_vwap",
    "premarket_price_confirmation",
    "availability",
    "rvol_provenance",
    "premarket_price_provenance",
)


def _positive_float(value: object) -> float:
    if (
        not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 0
    ):
        raise ValueError("bar price must be finite and positive")
    return float(value)


def _nonnegative_int(value: object) -> int:
    if not isinstance(value, int) or value < 0:
        raise ValueError("bar volume must be a nonnegative integer")
    return value


def atr(daily: pl.DataFrame, n: int = 14) -> pl.Series:
    """Wilder-style average true range for an already time-sorted daily frame."""
    if n <= 0:
        raise ValueError("n must be positive")
    missing = {"high", "low", "close"} - set(daily.columns)
    if missing:
        raise ValueError(f"missing required columns: {sorted(missing)}")

    return (
        daily.select(
            pl.max_horizontal(
                pl.col("high") - pl.col("low"),
                (pl.col("high") - pl.col("close").shift(1)).abs(),
                (pl.col("low") - pl.col("close").shift(1)).abs(),
            )
            .ewm_mean(alpha=1 / n, adjust=False, min_samples=n)
            .alias("atr")
        )
        .get_column("atr")
    )


def beta(stock: pl.DataFrame, market: pl.DataFrame, n: int = 252) -> float:
    """Return trailing close-to-close beta aligned on common trading dates."""
    if n <= 1:
        raise ValueError("n must be greater than one")
    required = {"trade_date", "close"}
    for name, frame in (("stock", stock), ("market", market)):
        missing = required - set(frame.columns)
        if missing:
            raise ValueError(f"{name} missing required columns: {sorted(missing)}")

    stock_returns = stock.sort("trade_date").select(
        "trade_date", (pl.col("close") / pl.col("close").shift(1) - 1).alias("stock_return")
    )
    market_returns = market.sort("trade_date").select(
        "trade_date", (pl.col("close") / pl.col("close").shift(1) - 1).alias("market_return")
    )
    aligned = stock_returns.join(market_returns, on="trade_date", how="inner").drop_nulls().tail(n)
    if aligned.height < n:
        return math.nan

    variance = aligned.get_column("market_return").var(ddof=1)
    covariance = aligned.select(pl.cov("stock_return", "market_return", ddof=1)).item()
    if not isinstance(variance, (int, float)) or not isinstance(covariance, (int, float)):
        return math.nan
    if variance <= 0:
        return math.nan
    return float(covariance / variance)


def days_in_play(rvol_history: pl.Series, *, min_rvol: float = 3.0) -> int:
    """Count consecutive extreme-RVOL sessions ending at the latest observation."""
    if not math.isfinite(min_rvol) or min_rvol <= 0:
        raise ValueError("min_rvol must be finite and positive")
    count = 0
    for value in reversed(rvol_history.to_list()):
        if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            break
        if float(value) <= min_rvol:
            break
        count += 1
    return count


def premarket_window_utc(trade_date: date, cutoff_et: time) -> tuple[datetime, datetime]:
    """Return the half-open ``[04:00, cutoff)`` New York window in UTC.

    The conversion is performed independently for every session so DST changes do
    not shift the same-time historical comparison.
    """
    if cutoff_et.tzinfo is not None:
        raise ValueError("cutoff_et must be a timezone-naive New York wall time")
    if cutoff_et <= PREMARKET_START_ET or cutoff_et > time(9, 30):
        raise ValueError("cutoff_et must be after 04:00 and no later than 09:30 ET")
    start_local = datetime.combine(trade_date, PREMARKET_START_ET, NEW_YORK)
    end_local = datetime.combine(trade_date, cutoff_et, NEW_YORK)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def rvol(
    bars: pl.DataFrame,
    *,
    schedule: pl.DataFrame,
    target_date: date,
    symbols: Iterable[str],
    complete_session_dates: Iterable[date],
    cutoff_et: time,
    n: int = 20,
    min_rvol: float = 3.0,
    min_premarket_return: float = 0.0,
    min_premarket_close_location: float = 0.60,
    provenance: str,
) -> pl.DataFrame:
    """Compute no-lookahead premarket relative volume for an explicit symbol pool.

    RVOL is current premarket volume divided by the median volume from the prior
    ``n`` XNYS sessions over the identical New York wall-clock interval. The end
    time is exclusive: a 07:45 cutoff includes bars stamped through 07:44.

    A successful provider query with no emitted bar represents zero eligible bar
    volume; Alpaca does not emit a stock minute bar when no qualifying trade exists.
    A session whose query did not complete is different and makes RVOL unavailable.
    No minute is forward-filled or interpolated.
    """
    if n <= 0:
        raise ValueError("n must be positive")
    if not math.isfinite(min_rvol) or min_rvol <= 0:
        raise ValueError("min_rvol must be a finite positive number")
    if not math.isfinite(min_premarket_return) or min_premarket_return < 0:
        raise ValueError("min_premarket_return must be finite and nonnegative")
    if (
        not math.isfinite(min_premarket_close_location)
        or not 0 <= min_premarket_close_location <= 1
    ):
        raise ValueError("min_premarket_close_location must be between zero and one")
    if not provenance.strip():
        raise ValueError("provenance must be non-empty")
    required_bars = {
        "symbol",
        "ts_utc",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "vwap",
    }
    missing_bars = required_bars - set(bars.columns)
    if missing_bars:
        raise ValueError(f"bars missing required columns: {sorted(missing_bars)}")
    if str(bars.schema["ts_utc"]) != "Datetime(time_unit='ms', time_zone='UTC')":
        raise ValueError("bars ts_utc must be timezone-aware UTC")
    required_schedule = {"trade_date"}
    missing_schedule = required_schedule - set(schedule.columns)
    if missing_schedule:
        raise ValueError(f"schedule missing required columns: {sorted(missing_schedule)}")

    locked_symbols = tuple(sorted({str(symbol).strip().upper() for symbol in symbols if symbol}))
    if not locked_symbols:
        return _empty_rvol_frame()
    session_dates = (
        schedule.filter(pl.col("trade_date") <= target_date)
        .get_column("trade_date")
        .unique()
        .sort()
        .tail(n + 1)
        .to_list()
    )
    if len(session_dates) != n + 1 or session_dates[-1] != target_date:
        raise ValueError(f"target session plus {n} prior XNYS sessions are required")

    historical_dates = session_dates[:-1]
    required_dates = set(session_dates)
    completed = set(complete_session_dates) & required_dates
    window_by_date = {
        trade_date: premarket_window_utc(trade_date, cutoff_et)
        for trade_date in session_dates
    }
    target_start_utc, target_cutoff_utc = window_by_date[target_date]

    relevant = bars.filter(pl.col("symbol").is_in(locked_symbols))
    duplicate_count = (
        relevant.select(pl.struct("symbol", "ts_utc").is_duplicated().sum()).item()
        if relevant.height
        else 0
    )
    if duplicate_count:
        raise ValueError(f"bars contain {duplicate_count} duplicate symbol/timestamp keys")
    if relevant.height and relevant.filter(pl.col("volume") < 0).height:
        raise ValueError("bars contain negative volume")

    cumulative: dict[tuple[str, date], int] = {
        (symbol, trade_date): 0
        for symbol in locked_symbols
        for trade_date in completed
    }
    current_bars: dict[str, list[dict[str, object]]] = {
        symbol: [] for symbol in locked_symbols
    }
    for row in relevant.select(*sorted(required_bars)).iter_rows(named=True):
        symbol = str(row["symbol"])
        timestamp = row["ts_utc"]
        volume = row["volume"]
        if not isinstance(timestamp, datetime) or not isinstance(volume, int):
            raise ValueError("bar timestamp or volume has an invalid type")
        local_date = timestamp.astimezone(NEW_YORK).date()
        if local_date not in required_dates or local_date not in completed:
            continue
        start_utc, end_utc = window_by_date[local_date]
        if start_utc <= timestamp < end_utc:
            cumulative[(symbol, local_date)] += volume
            if local_date == target_date:
                current_bars[symbol].append(row)

    history_session_count = sum(value in completed for value in historical_dates)
    current_complete = target_date in completed
    rows: list[dict[str, object]] = []
    for symbol in locked_symbols:
        historical_volumes = [
            cumulative[(symbol, trade_date)]
            for trade_date in historical_dates
            if trade_date in completed
        ]
        historical_median = (
            float(median(historical_volumes)) if historical_volumes else None
        )
        current_volume = cumulative.get((symbol, target_date)) if current_complete else None
        value: float | None = None
        if not current_complete:
            availability = "incomplete_current_session"
        elif history_session_count != n:
            availability = "incomplete_history"
        elif historical_median is None or historical_median <= 0:
            availability = "zero_historical_median"
        else:
            if current_volume is None:
                raise AssertionError("complete current session has no cumulative volume")
            value = float(current_volume / historical_median)
            availability = "available"

        target_rows = sorted(
            current_bars[symbol],
            key=lambda row: row["ts_utc"]
            if isinstance(row["ts_utc"], datetime)
            else datetime.min.replace(tzinfo=UTC),
        )
        premarket_open: float | None = None
        premarket_high: float | None = None
        premarket_low: float | None = None
        premarket_close: float | None = None
        premarket_vwap: float | None = None
        premarket_return: float | None = None
        premarket_close_location: float | None = None
        premarket_above_vwap = False
        premarket_price_confirmation = False
        price_values = [
            row[column]
            for row in target_rows
            for column in ("open", "high", "low", "close", "vwap")
        ]
        valid_prices = bool(target_rows) and all(
            isinstance(item, (int, float))
            and math.isfinite(float(item))
            and float(item) > 0
            for item in price_values
        )
        if valid_prices:
            premarket_open = _positive_float(target_rows[0]["open"])
            premarket_high = max(
                _positive_float(row["high"]) for row in target_rows
            )
            premarket_low = min(
                _positive_float(row["low"]) for row in target_rows
            )
            premarket_close = _positive_float(target_rows[-1]["close"])
            weighted_volume = sum(
                _nonnegative_int(row["volume"]) for row in target_rows
            )
            if weighted_volume > 0:
                premarket_vwap = sum(
                    _positive_float(row["vwap"])
                    * _nonnegative_int(row["volume"])
                    for row in target_rows
                ) / weighted_volume
            if premarket_vwap is not None:
                premarket_return = premarket_close / premarket_open - 1
                price_range = premarket_high - premarket_low
                premarket_close_location = (
                    (premarket_close - premarket_low) / price_range
                    if price_range > 0
                    else 0.5
                )
                premarket_above_vwap = premarket_close > premarket_vwap
                premarket_price_confirmation = (
                    premarket_return > min_premarket_return
                    and premarket_above_vwap
                    and premarket_close_location >= min_premarket_close_location
                )
        rows.append(
            {
                "symbol": symbol,
                "session_date": target_date,
                "premarket_start_utc": target_start_utc,
                "data_cutoff_utc": target_cutoff_utc,
                "last_included_minute_utc": target_cutoff_utc - timedelta(minutes=1),
                "current_premarket_volume": current_volume,
                "median_historical_premarket_volume": historical_median,
                "history_session_count": history_session_count,
                "historical_nonzero_sessions": sum(item > 0 for item in historical_volumes),
                "rvol": value,
                "rvol_pass": value is not None and value > min_rvol,
                "premarket_open": premarket_open,
                "premarket_high": premarket_high,
                "premarket_low": premarket_low,
                "premarket_close": premarket_close,
                "premarket_vwap": premarket_vwap,
                "premarket_return": premarket_return,
                "premarket_close_location": premarket_close_location,
                "premarket_above_vwap": premarket_above_vwap,
                "premarket_price_confirmation": premarket_price_confirmation,
                "availability": availability,
                "rvol_provenance": (
                    f"{provenance}|premarket_rvol_median{n}.v2|"
                    f"04:00-{cutoff_et.isoformat()}ET_end_exclusive"
                ),
                "premarket_price_provenance": (
                    f"{provenance}|premarket_direction.v1|"
                    f"return>{min_premarket_return}|close>vwap|"
                    f"close_location>={min_premarket_close_location}"
                ),
            }
        )
    return pl.DataFrame(rows, schema=_rvol_schema()).select(RVOL_COLUMNS).sort("symbol")


def _rvol_schema() -> dict[str, Any]:
    return {
        "symbol": pl.String,
        "session_date": pl.Date,
        "premarket_start_utc": pl.Datetime("ms", "UTC"),
        "data_cutoff_utc": pl.Datetime("ms", "UTC"),
        "last_included_minute_utc": pl.Datetime("ms", "UTC"),
        "current_premarket_volume": pl.Int64,
        "median_historical_premarket_volume": pl.Float64,
        "history_session_count": pl.UInt32,
        "historical_nonzero_sessions": pl.UInt32,
        "rvol": pl.Float64,
        "rvol_pass": pl.Boolean,
        "premarket_open": pl.Float64,
        "premarket_high": pl.Float64,
        "premarket_low": pl.Float64,
        "premarket_close": pl.Float64,
        "premarket_vwap": pl.Float64,
        "premarket_return": pl.Float64,
        "premarket_close_location": pl.Float64,
        "premarket_above_vwap": pl.Boolean,
        "premarket_price_confirmation": pl.Boolean,
        "availability": pl.String,
        "rvol_provenance": pl.String,
        "premarket_price_provenance": pl.String,
    }


def _empty_rvol_frame() -> pl.DataFrame:
    return pl.DataFrame(schema=_rvol_schema()).select(RVOL_COLUMNS)
