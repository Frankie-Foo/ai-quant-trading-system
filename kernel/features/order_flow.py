"""Deterministic consolidated-tape order-flow features with explicit degradation."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import polars as pl

TRADE_COLUMNS = {
    "symbol",
    "ts_utc",
    "trade_id",
    "exchange",
    "price",
    "size",
    "conditions",
    "tape",
    "source",
    "feed",
}
QUOTE_COLUMNS = {
    "symbol",
    "ts_utc",
    "bid_price",
    "ask_price",
    "bid_size",
    "ask_size",
    "source",
    "feed",
}


def _require(
    frame: pl.DataFrame, *, name: str, columns: set[str]
) -> None:
    missing = columns - set(frame.columns)
    if missing:
        raise ValueError(f"{name} missing required columns: {sorted(missing)}")
    timestamp_type = frame.schema["ts_utc"]
    if not isinstance(timestamp_type, pl.Datetime) or timestamp_type.time_zone != "UTC":
        raise ValueError(f"{name} timestamps must be timezone-aware UTC")


def _number(value: object) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _tick_rule(
    rows: list[dict[str, object]],
) -> tuple[int, int, int, int]:
    buy_volume = 0
    sell_volume = 0
    classified_trades = 0
    last_price: float | None = None
    last_direction = 0
    for row in rows:
        price = _number(row.get("price"))
        raw_size = row.get("size")
        if (
            price is None
            or price <= 0
            or not isinstance(raw_size, int)
            or isinstance(raw_size, bool)
            or raw_size <= 0
        ):
            continue
        direction = last_direction
        if last_price is not None:
            if price > last_price:
                direction = 1
            elif price < last_price:
                direction = -1
        if last_price is None:
            direction = 0
        if price != last_price:
            last_price = price
        if direction > 0:
            buy_volume += raw_size
            classified_trades += 1
        elif direction < 0:
            sell_volume += raw_size
            classified_trades += 1
        last_direction = direction
    return buy_volume, sell_volume, classified_trades, buy_volume + sell_volume


def _vpoc(rows: list[dict[str, object]]) -> float | None:
    volume_by_cent: dict[int, int] = {}
    for row in rows:
        price = _number(row.get("price"))
        raw_size = row.get("size")
        if (
            price is None
            or price <= 0
            or not isinstance(raw_size, int)
            or isinstance(raw_size, bool)
            or raw_size <= 0
        ):
            continue
        cents = round(price * 100)
        volume_by_cent[cents] = volume_by_cent.get(cents, 0) + raw_size
    if not volume_by_cent:
        return None
    cents = min(
        volume_by_cent,
        key=lambda value: (-volume_by_cent[value], value),
    )
    return cents / 100


def _latest_quote_metrics(
    quotes: pl.DataFrame,
    *,
    symbol: str,
    start_utc: datetime,
    asof_utc: datetime,
) -> dict[str, object]:
    selected = quotes.filter(
        (pl.col("symbol") == symbol)
        & (pl.col("ts_utc") >= start_utc)
        & (pl.col("ts_utc") <= asof_utc)
    ).sort("ts_utc")
    if selected.is_empty():
        return {
            "quote_availability": "no_quote",
            "quote_ts_utc": None,
            "quote_size_imbalance": None,
            "microprice": None,
            "spread_bps": None,
        }
    row = selected.row(-1, named=True)
    bid = _number(row.get("bid_price"))
    ask = _number(row.get("ask_price"))
    bid_size = _number(row.get("bid_size"))
    ask_size = _number(row.get("ask_size"))
    if (
        bid is None
        or ask is None
        or bid <= 0
        or ask <= bid
        or bid_size is None
        or ask_size is None
        or bid_size < 0
        or ask_size < 0
        or bid_size + ask_size <= 0
    ):
        return {
            "quote_availability": "invalid_nbbo",
            "quote_ts_utc": row["ts_utc"],
            "quote_size_imbalance": None,
            "microprice": None,
            "spread_bps": None,
        }
    midpoint = (bid + ask) / 2
    return {
        "quote_availability": "available",
        "quote_ts_utc": row["ts_utc"],
        "quote_size_imbalance": (bid_size - ask_size) / (bid_size + ask_size),
        "microprice": (ask * bid_size + bid * ask_size)
        / (bid_size + ask_size),
        "spread_bps": (ask - bid) / midpoint * 10_000,
    }


def order_flow_features(
    trades: pl.DataFrame,
    quotes: pl.DataFrame,
    *,
    symbols: tuple[str, ...],
    asof_utc: datetime,
    window: timedelta,
    provenance: str,
) -> pl.DataFrame:
    """Return one causal Tick-Rule/NBBO row per requested symbol.

    This is consolidated-tape order flow, not Level-2 or market-by-order depth.
    Future rows are ignored mechanically and missing evidence stays unavailable.
    """

    if asof_utc.tzinfo is None or asof_utc.utcoffset() != UTC.utcoffset(asof_utc):
        raise ValueError("asof_utc must be timezone-aware UTC")
    if window <= timedelta(0) or window > timedelta(hours=24):
        raise ValueError("order-flow window must be in (0, 24h]")
    normalized = tuple(dict.fromkeys(symbol.strip().upper() for symbol in symbols))
    if not normalized or any(not symbol for symbol in normalized):
        raise ValueError("at least one valid symbol is required")
    if not provenance.strip():
        raise ValueError("order-flow provenance is required")
    _require(trades, name="trades", columns=TRADE_COLUMNS)
    _require(quotes, name="quotes", columns=QUOTE_COLUMNS)

    start_utc = asof_utc - window
    causal_trades = (
        trades.filter(
            pl.col("symbol").is_in(normalized)
            & (pl.col("ts_utc") >= start_utc)
            & (pl.col("ts_utc") <= asof_utc)
        )
        .unique(
            subset=["symbol", "ts_utc", "trade_id", "tape"],
            keep="first",
            maintain_order=True,
        )
        .sort("symbol", "ts_utc", "trade_id")
    )
    rows: list[dict[str, object]] = []
    for symbol in normalized:
        selected = causal_trades.filter(pl.col("symbol") == symbol)
        trade_rows = selected.iter_rows(named=True)
        materialized = list(trade_rows)
        total_volume = sum(
            int(size)
            for size in selected.get_column("size").to_list()
            if isinstance(size, int) and not isinstance(size, bool) and size > 0
        )
        buy_volume, sell_volume, classified_trades, classified_volume = (
            _tick_rule(materialized)
        )
        if selected.is_empty():
            availability = "no_trades"
        elif classified_volume <= 0:
            availability = "insufficient_directional_trades"
        else:
            availability = "available"
        imbalance = (
            (buy_volume - sell_volume) / classified_volume
            if classified_volume > 0
            else None
        )
        pressure_ratio = (
            buy_volume / sell_volume if buy_volume > 0 and sell_volume > 0 else None
        )
        quote_metrics = _latest_quote_metrics(
            quotes,
            symbol=symbol,
            start_utc=start_utc,
            asof_utc=asof_utc,
        )
        quote_imbalance = _number(quote_metrics["quote_size_imbalance"])
        confirmation_score = (
            None
            if imbalance is None
            else min(
                max(
                    50.0
                    + 40.0 * imbalance
                    + 10.0 * (quote_imbalance or 0.0),
                    0.0,
                ),
                100.0,
            )
        )
        rows.append(
            {
                "symbol": symbol,
                "availability": availability,
                "window_start_utc": start_utc,
                "data_cutoff_utc": asof_utc,
                "trade_count": selected.height,
                "classified_trade_count": classified_trades,
                "total_volume": total_volume,
                "classified_volume": classified_volume,
                "classified_volume_ratio": (
                    classified_volume / total_volume if total_volume > 0 else None
                ),
                "buy_volume": buy_volume,
                "sell_volume": sell_volume,
                "order_imbalance": imbalance,
                "buy_sell_pressure_ratio": pressure_ratio,
                "vpoc": _vpoc(materialized),
                **quote_metrics,
                "order_flow_confirmation_score": confirmation_score,
                "order_flow_provenance": (
                    f"{provenance}|tick_rule.v1|nbbo_top_of_book.v1|"
                    f"window={int(window.total_seconds())}s"
                ),
                "production_eligible": False,
            }
        )
    return pl.DataFrame(rows).sort("symbol")
