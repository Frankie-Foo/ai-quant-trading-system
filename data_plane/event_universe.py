"""Point-in-time event discovery universe with explicit market-cap coverage."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import polars as pl


@dataclass(frozen=True)
class EventUniversePolicy:
    min_market_cap_usd: float = 1_000_000_000.0
    max_market_cap_age_days: int = 31

    def __post_init__(self) -> None:
        if (
            not math.isfinite(self.min_market_cap_usd)
            or self.min_market_cap_usd <= 0
            or self.max_market_cap_age_days < 0
        ):
            raise ValueError("event-universe market-cap policy is invalid")


@dataclass(frozen=True)
class SipMarketCapPolicy:
    max_trade_age_seconds: int = 2

    def __post_init__(self) -> None:
        if self.max_trade_age_seconds < 0:
            raise ValueError("max_trade_age_seconds must be non-negative")


DEFAULT_EVENT_UNIVERSE_POLICY = EventUniversePolicy()
DEFAULT_SIP_MARKET_CAP_POLICY = SipMarketCapPolicy()


def _instant(value: object, *, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _require(frame: pl.DataFrame, *columns: str) -> None:
    missing = set(columns) - set(frame.columns)
    if missing:
        raise ValueError(f"missing required columns: {sorted(missing)}")


def _finite_positive(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    )


def derive_sip_market_caps(
    shares: pl.DataFrame,
    trades: pl.DataFrame,
    *,
    as_of: datetime,
    policy: SipMarketCapPolicy = DEFAULT_SIP_MARKET_CAP_POLICY,
    quotes: pl.DataFrame | None = None,
) -> pl.DataFrame:
    """Mark known shares to fresh Alpaca SIP prices.

    Shares are slow-moving metadata; price uses a SIP trade, or a valid SIP
    NBBO midpoint when no fresh trade exists. Future, stale, non-SIP, and
    missing prices remain explicit nulls, never a carry-forward market cap.
    """
    as_of = _instant(as_of, name="as_of")
    _require(shares, "symbol", "shares_outstanding", "available_at", "source", "provenance")
    _require(trades, "symbol", "ts_utc", "price", "available_at", "source", "feed")
    latest: dict[str, dict[str, Any]] = {}
    future_trade: set[str] = set()
    non_sip_trade: set[str] = set()
    for row in trades.iter_rows(named=True):
        symbol = str(row["symbol"]).strip().upper()
        timestamp = _instant(row["ts_utc"], name="trade ts_utc")
        available_at = _instant(row["available_at"], name="trade available_at")
        if timestamp > as_of or available_at > as_of:
            future_trade.add(symbol)
            continue
        if str(row["feed"]).strip().lower() != "sip":
            non_sip_trade.add(symbol)
            continue
        previous = latest.get(symbol)
        if previous is None or timestamp > previous["ts_utc"]:
            latest[symbol] = {**row, "ts_utc": timestamp, "available_at": available_at}

    latest_quotes: dict[str, dict[str, Any]] = {}
    if quotes is not None:
        _require(
            quotes,
            "symbol",
            "ts_utc",
            "bid_price",
            "ask_price",
            "available_at",
            "source",
            "feed",
        )
        for row in quotes.iter_rows(named=True):
            symbol = str(row["symbol"]).strip().upper()
            timestamp = _instant(row["ts_utc"], name="quote ts_utc")
            available_at = _instant(row["available_at"], name="quote available_at")
            if (
                timestamp > as_of
                or available_at > as_of
                or str(row["feed"]).strip().lower() != "sip"
            ):
                continue
            bid = row["bid_price"]
            ask = row["ask_price"]
            if not _finite_positive(bid) or not _finite_positive(ask) or float(ask) < float(bid):
                continue
            previous = latest_quotes.get(symbol)
            if previous is None or timestamp > previous["ts_utc"]:
                latest_quotes[symbol] = {
                    **row,
                    "ts_utc": timestamp,
                    "available_at": available_at,
                }

    rows: list[dict[str, Any]] = []
    for share in shares.iter_rows(named=True):
        symbol = str(share["symbol"]).strip().upper()
        share_available_at = _instant(share["available_at"], name="shares available_at")
        trade = latest.get(symbol)
        quote = latest_quotes.get(symbol)
        status = "available"
        market_cap: float | None = None
        price_source: str | None = None
        price_timestamp: datetime | None = None
        price_available_at: datetime | None = None
        if (
            share_available_at > as_of
            or not _finite_positive(share["shares_outstanding"])
        ):
            status = "shares_unavailable"
        elif (
            trade is not None
            and (as_of - trade["ts_utc"]).total_seconds() <= policy.max_trade_age_seconds
            and _finite_positive(trade["price"])
        ):
            market_cap = float(share["shares_outstanding"]) * float(trade["price"])
            price_source = str(trade["source"])
            price_timestamp = trade["ts_utc"]
            price_available_at = trade["available_at"]
        elif (
            quote is not None
            and (as_of - quote["ts_utc"]).total_seconds() <= policy.max_trade_age_seconds
        ):
            midpoint = (float(quote["bid_price"]) + float(quote["ask_price"])) / 2
            market_cap = float(share["shares_outstanding"]) * midpoint
            price_source = f"{quote['source']}.midpoint"
            price_timestamp = quote["ts_utc"]
            price_available_at = quote["available_at"]
        elif trade is None:
            status = (
                "future_trade" if symbol in future_trade else
                "price_feed_not_sip" if symbol in non_sip_trade else "trade_missing"
            )
        elif (as_of - trade["ts_utc"]).total_seconds() > policy.max_trade_age_seconds:
            status = "trade_stale"
        elif not _finite_positive(trade["price"]):
            status = "trade_price_invalid"
        else:
            market_cap = float(share["shares_outstanding"]) * float(trade["price"])
            price_source = str(trade["source"])
            price_timestamp = trade["ts_utc"]
            price_available_at = trade["available_at"]
        rows.append(
            {
                "symbol": symbol,
                "asof_date": as_of.date(),
                "market_cap": market_cap,
                "available_at": (
                    max(share_available_at, price_available_at)
                    if price_available_at is not None else None
                ),
                "source": "derived.sip_market_cap",
                "provenance": (
                    f"{share['provenance']}|{price_source}@{price_timestamp.isoformat()}"
                    if (
                        market_cap is not None
                        and price_source is not None
                        and price_timestamp is not None
                    )
                    else str(share["provenance"])
                ),
                "market_cap_status": status,
                "shares_source": str(share["source"]),
                "price_source": price_source,
                "price_timestamp": price_timestamp,
            }
        )
    return pl.DataFrame(rows).sort("symbol")


def _cap_status(
    rows: list[dict[str, Any]],
    *,
    decision_at: datetime,
    policy: EventUniversePolicy,
) -> tuple[dict[str, Any] | None, str]:
    valid: list[dict[str, Any]] = []
    future_seen = False
    unavailable_statuses: list[str] = []
    for row in rows:
        raw_available_at = row["available_at"]
        if raw_available_at is None:
            status = str(row.get("market_cap_status") or "missing").strip() or "missing"
            unavailable_statuses.append(status)
            continue
        available_at = _instant(raw_available_at, name="market-cap available_at")
        asof_date = row["asof_date"]
        if not isinstance(asof_date, date):
            raise ValueError("market-cap asof_date must be a date")
        if available_at > decision_at or asof_date > decision_at.date():
            future_seen = True
            continue
        if (decision_at.date() - asof_date).days > policy.max_market_cap_age_days:
            continue
        if _finite_positive(row["market_cap"]):
            valid.append({**row, "available_at": available_at})
    if valid:
        # Date takes priority, then direct provider evidence over a derived estimate.
        def source_rank(row: dict[str, Any]) -> int:
            return 1 if str(row["source"]).endswith(".direct") else 0

        selected = max(
            valid,
            key=lambda row: (
                row["asof_date"],
                source_rank(row),
                row["available_at"],
            ),
        )
        return selected, "available"
    if future_seen:
        return None, "future_unavailable"
    return None, unavailable_statuses[0] if unavailable_statuses else "missing"


def build_event_universe(
    reference: pl.DataFrame,
    daily_bars: pl.DataFrame,
    market_caps: pl.DataFrame,
    *,
    decision_at: datetime,
    policy: EventUniversePolicy = DEFAULT_EVENT_UNIVERSE_POLICY,
) -> pl.DataFrame:
    """Build discovery and cap-eligible subsets without dropping uncovered symbols.

    `instrument_class` is normalized by the caller. Only `common_stock` and `adr`
    can be cap-eligible; all other classes remain discovery evidence with a reason.
    """
    decision_at = _instant(decision_at, name="decision_at")
    _require(
        reference,
        "symbol",
        "instrument_class",
        "active",
        "reference_asof_date",
        "available_at",
    )
    _require(daily_bars, "symbol", "trade_date", "close", "available_at")
    _require(
        market_caps,
        "symbol",
        "asof_date",
        "market_cap",
        "available_at",
        "source",
        "provenance",
    )
    if reference.get_column("symbol").n_unique() != reference.height:
        raise ValueError("reference symbols must be unique")

    latest_daily: dict[str, dict[str, Any]] = {}
    for row in daily_bars.iter_rows(named=True):
        available_at = _instant(row["available_at"], name="daily available_at")
        trade_date = row["trade_date"]
        if not isinstance(trade_date, date):
            raise ValueError("daily trade_date must be a date")
        if available_at > decision_at or trade_date > decision_at.date():
            continue
        symbol = str(row["symbol"]).strip().upper()
        previous = latest_daily.get(symbol)
        if previous is None or trade_date > previous["trade_date"]:
            latest_daily[symbol] = {**row, "available_at": available_at}

    caps_by_symbol: dict[str, list[dict[str, Any]]] = {}
    for row in market_caps.iter_rows(named=True):
        symbol = str(row["symbol"]).strip().upper()
        caps_by_symbol.setdefault(symbol, []).append(row)

    discovered: list[dict[str, Any]] = []
    for row in reference.iter_rows(named=True):
        available_at = _instant(row["available_at"], name="reference available_at")
        reference_asof = row["reference_asof_date"]
        if not isinstance(reference_asof, date):
            raise ValueError("reference_asof_date must be a date")
        if (
            available_at > decision_at
            or reference_asof > decision_at.date()
            or row["active"] is not True
        ):
            continue
        symbol = str(row["symbol"]).strip().upper()
        cap, cap_status = _cap_status(
            caps_by_symbol.get(symbol, []),
            decision_at=decision_at,
            policy=policy,
        )
        daily = latest_daily.get(symbol)
        instrument_class = str(row["instrument_class"]).strip().lower()
        tradable_class = instrument_class in {"common_stock", "adr"}
        daily_ok = daily is not None and _finite_positive(daily["close"])
        cap_value = (
            float(cap["market_cap"])
            if cap is not None and _finite_positive(cap["market_cap"])
            else None
        )
        eligible = (
            tradable_class
            and daily_ok
            and cap_value is not None
            and cap_value >= policy.min_market_cap_usd
        )
        if not tradable_class:
            reason = "instrument_class_not_tradable"
        elif not daily_ok:
            reason = "daily_close_unavailable"
        elif cap_status != "available" or cap_value is None:
            reason = "market_cap_not_available"
        elif cap_value < policy.min_market_cap_usd:
            reason = "market_cap_below_minimum"
        else:
            reason = None
        daily_close = None
        daily_trade_date = None
        if daily is not None and _finite_positive(daily["close"]):
            daily_close = float(daily["close"])
            daily_trade_date = daily["trade_date"]
        discovered.append(
            {
                "symbol": symbol,
                "instrument_class": instrument_class,
                "reference_asof_date": reference_asof,
                "reference_available_at": available_at,
                "daily_close": daily_close,
                "daily_trade_date": daily_trade_date,
                "market_cap": cap_value,
                "market_cap_asof_date": cap["asof_date"] if cap is not None else None,
                "market_cap_available_at": cap["available_at"] if cap is not None else None,
                "market_cap_source": cap["source"] if cap is not None else None,
                "market_cap_provenance": cap["provenance"] if cap is not None else None,
                "market_cap_status": cap_status,
                "trade_eligible": eligible,
                "rejection_reason": reason,
            }
        )
    total = len(discovered)
    covered = sum(row["market_cap_status"] == "available" for row in discovered)
    return pl.DataFrame(discovered).with_columns(
        pl.lit(total).alias("discovery_count"),
        pl.lit(covered).alias("market_cap_covered_count"),
        pl.lit(covered / total if total else 0.0).alias("market_cap_coverage_ratio"),
    ).sort("symbol")
