"""Point-in-time NBBO observations used by conservative transaction-cost labels."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

import polars as pl


@dataclass(frozen=True)
class QuoteSpread:
    symbol: str
    requested_at_utc: datetime
    quote_ts_utc: datetime
    bid_price: float
    ask_price: float
    relative_spread: float
    age_seconds: float
    provenance: str


@dataclass(frozen=True)
class QuoteWindowSpread:
    symbol: str
    start_utc: datetime
    end_utc: datetime
    relative_spread: float
    sample_count: int
    quantile: float
    provenance: str


def latest_nbbo_spread(
    quotes: pl.DataFrame,
    *,
    symbol: str,
    at_utc: datetime,
    max_age: timedelta = timedelta(seconds=30),
) -> QuoteSpread | None:
    """Return the last valid nonfuture NBBO, or N/A when quote coverage is stale."""
    required = {"symbol", "ts_utc", "bid_price", "ask_price", "source", "feed"}
    missing = required - set(quotes.columns)
    if missing:
        raise ValueError(f"quotes missing required columns: {sorted(missing)}")
    if at_utc.tzinfo is None or at_utc.utcoffset() is None:
        raise ValueError("at_utc must be timezone-aware")
    if max_age <= timedelta(0):
        raise ValueError("max_age must be positive")
    eligible = (
        quotes.filter(
            (pl.col("symbol") == symbol)
            & (pl.col("ts_utc") <= at_utc)
            & (pl.col("ts_utc") >= at_utc - max_age)
            & pl.col("bid_price").is_finite()
            & pl.col("ask_price").is_finite()
            & (pl.col("bid_price") > 0)
            & (pl.col("ask_price") >= pl.col("bid_price"))
        )
        .sort("ts_utc")
        .tail(1)
    )
    if eligible.is_empty():
        return None
    row = eligible.row(0, named=True)
    quote_ts = row["ts_utc"]
    if not isinstance(quote_ts, datetime):
        raise ValueError("quote timestamp is invalid")
    bid = float(row["bid_price"])
    ask = float(row["ask_price"])
    midpoint = (bid + ask) / 2
    spread = (ask - bid) / midpoint
    age = (at_utc - quote_ts).total_seconds()
    if not math.isfinite(spread) or spread < 0 or age < 0:
        raise ValueError("quote spread observation is invalid")
    return QuoteSpread(
        symbol=symbol,
        requested_at_utc=at_utc,
        quote_ts_utc=quote_ts,
        bid_price=bid,
        ask_price=ask,
        relative_spread=spread,
        age_seconds=age,
        provenance=(
            f"{row['source']}.{row['feed']}.nbbo@{quote_ts.isoformat()}|"
            f"requested={at_utc.isoformat()}|max_age={max_age.total_seconds():g}s"
        ),
    )


def window_nbbo_spread(
    quotes: pl.DataFrame,
    *,
    symbol: str,
    start_utc: datetime,
    end_utc: datetime,
    quantile: float = 0.95,
) -> QuoteWindowSpread | None:
    """Return a conservative observed spread across an execution minute."""
    required = {"symbol", "ts_utc", "bid_price", "ask_price", "source", "feed"}
    missing = required - set(quotes.columns)
    if missing:
        raise ValueError(f"quotes missing required columns: {sorted(missing)}")
    if (
        start_utc.tzinfo is None
        or end_utc.tzinfo is None
        or end_utc <= start_utc
    ):
        raise ValueError("a valid timezone-aware window is required")
    if not 0.5 <= quantile <= 1:
        raise ValueError("quantile must be in [0.5, 1]")
    eligible = quotes.filter(
        (pl.col("symbol") == symbol)
        & (pl.col("ts_utc") >= start_utc)
        & (pl.col("ts_utc") < end_utc)
        & pl.col("bid_price").is_finite()
        & pl.col("ask_price").is_finite()
        & (pl.col("bid_price") > 0)
        & (pl.col("ask_price") >= pl.col("bid_price"))
    ).with_columns(
        (
            (pl.col("ask_price") - pl.col("bid_price"))
            / ((pl.col("ask_price") + pl.col("bid_price")) / 2)
        ).alias("relative_spread")
    )
    if eligible.is_empty():
        return None
    spread = eligible["relative_spread"].quantile(quantile, interpolation="higher")
    if not isinstance(spread, (int, float)) or not math.isfinite(float(spread)):
        raise ValueError("quote window spread is invalid")
    sources = eligible.select("source", "feed").unique().rows()
    source_text = ",".join(f"{source}.{feed}" for source, feed in sources)
    return QuoteWindowSpread(
        symbol=symbol,
        start_utc=start_utc,
        end_utc=end_utc,
        relative_spread=float(spread),
        sample_count=eligible.height,
        quantile=quantile,
        provenance=(
            f"{source_text}.nbbo_window@{start_utc.isoformat()}..{end_utc.isoformat()}|"
            f"quantile={quantile}|samples={eligible.height}"
        ),
    )
