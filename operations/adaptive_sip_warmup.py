"""Convert accepted historical SIP frames into restart-safe local events."""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

import polars as pl

from execution.alpaca_sip_stream import SipBar, SipEvent, SipQuote, SipTrade


def build_warmup_events(
    *,
    bars: pl.DataFrame,
    quotes: pl.DataFrame,
    trades: pl.DataFrame,
) -> tuple[SipEvent, ...]:
    """Build validated events without filling or synthesizing missing observations."""

    events: list[SipEvent] = []
    for row in bars.iter_rows(named=True):
        events.append(
            SipBar(
                symbol=_symbol(row),
                ts_utc=_timestamp(row),
                open=_positive_float(row, "open"),
                high=_positive_float(row, "high"),
                low=_positive_float(row, "low"),
                close=_positive_float(row, "close"),
                volume=_non_negative_int(row, "volume"),
                trade_count=_non_negative_int(row, "trade_count"),
                vwap=_positive_float(row, "vwap"),
                feed=_feed(row),
                provenance=_provenance(row, kind="bar"),
            )
        )
    for row in quotes.iter_rows(named=True):
        events.append(
            SipQuote(
                symbol=_symbol(row),
                ts_utc=_timestamp(row),
                bid_price=_non_negative_float(row, "bid_price"),
                bid_size=_non_negative_int(row, "bid_size"),
                ask_price=_non_negative_float(row, "ask_price"),
                ask_size=_non_negative_int(row, "ask_size"),
                feed=_feed(row),
                provenance=_provenance(row, kind="quote"),
            )
        )
    for row in trades.iter_rows(named=True):
        events.append(
            SipTrade(
                symbol=_symbol(row),
                ts_utc=_timestamp(row),
                trade_id=_non_negative_int(row, "trade_id"),
                exchange=_required_text(row, "exchange"),
                price=_positive_float(row, "price"),
                size=_positive_int(row, "size"),
                conditions=_conditions(row),
                tape=_required_text(row, "tape"),
                feed=_feed(row),
                provenance=_provenance(row, kind="trade"),
            )
        )
    return tuple(events)


def _symbol(row: dict[str, Any]) -> str:
    symbol = _required_text(row, "symbol").upper()
    if symbol != symbol.strip():
        raise ValueError("symbol must not contain surrounding whitespace")
    return symbol


def _timestamp(row: dict[str, Any]) -> datetime:
    value = row.get("ts_utc")
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != timedelta(0)
    ):
        raise ValueError("historical SIP timestamp must be timezone-aware UTC")
    return value


def _required_text(row: dict[str, Any], name: str) -> str:
    value = row.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _feed(row: dict[str, Any]) -> str:
    feed = _required_text(row, "feed").lower()
    if feed != "sip":
        raise ValueError("adaptive client warmup requires the licensed SIP feed")
    return feed


def _provenance(row: dict[str, Any], *, kind: str) -> str:
    source = _required_text(row, "source")
    feed = _feed(row)
    adjustment = row.get("adjustment")
    suffix = (
        f":{str(adjustment).strip()}"
        if isinstance(adjustment, str) and adjustment.strip()
        else ""
    )
    return f"{source}:{feed}:historical_{kind}{suffix}"


def _number(row: dict[str, Any], name: str) -> float:
    value = row.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be an observed finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be an observed finite number")
    return number


def _positive_float(row: dict[str, Any], name: str) -> float:
    number = _number(row, name)
    if number <= 0:
        raise ValueError(f"{name} must be positive")
    return number


def _non_negative_float(row: dict[str, Any], name: str) -> float:
    number = _number(row, name)
    if number < 0:
        raise ValueError(f"{name} cannot be negative")
    return number


def _integer(row: dict[str, Any], name: str) -> int:
    number = _number(row, name)
    if not number.is_integer():
        raise ValueError(f"{name} must be an integer")
    return int(number)


def _positive_int(row: dict[str, Any], name: str) -> int:
    number = _integer(row, name)
    if number <= 0:
        raise ValueError(f"{name} must be positive")
    return number


def _non_negative_int(row: dict[str, Any], name: str) -> int:
    number = _integer(row, name)
    if number < 0:
        raise ValueError(f"{name} cannot be negative")
    return number


def _conditions(row: dict[str, Any]) -> tuple[str, ...]:
    value = row.get("conditions")
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("conditions must be a list of strings")
    return tuple(value)
