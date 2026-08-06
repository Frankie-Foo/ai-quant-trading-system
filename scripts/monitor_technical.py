"""One-shot, advisory-only multi-timeframe technical monitor."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from data_plane.calendar import build_xnys_schedule
from data_plane.providers.alpaca import (
    fetch_quotes,
    fetch_sparse_bars_for_monitoring,
    stock_data_policy_from_env,
)
from kernel.technical_monitor import (
    LongGreenExpansion,
    PositionPlan,
    QuoteSnapshot,
    TimeframeSnapshot,
    build_long_green_expansion,
    build_timeframe_snapshot,
    build_trade_advisory,
    current_session_fibonacci,
    current_session_vwap,
    resample_completed_bars,
)

ROOT = Path(__file__).resolve().parents[1]
EASTERN = ZoneInfo("America/New_York")


def _utc_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone")
    return parsed.astimezone(UTC)


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 6)


def _snapshot_payload(value: TimeframeSnapshot | None) -> dict[str, Any] | None:
    if value is None:
        return None
    payload: dict[str, Any] = asdict(value)
    for key, item in payload.items():
        if isinstance(item, datetime):
            payload[key] = item.isoformat()
        elif isinstance(item, float):
            payload[key] = _rounded(item)
    return payload


def _expansion_payload(value: LongGreenExpansion | None) -> dict[str, Any] | None:
    if value is None:
        return None
    payload: dict[str, Any] = asdict(value)
    for key, item in payload.items():
        if isinstance(item, datetime):
            payload[key] = item.isoformat()
        elif isinstance(item, float):
            payload[key] = _rounded(item)
    return payload


def _wave_proxy(snapshot: TimeframeSnapshot | None) -> str:
    if snapshot is None:
        return "unavailable"
    last_top = snapshot.last_confirmed_top
    last_bottom = snapshot.last_confirmed_bottom
    prior_bottom = snapshot.prior_confirmed_bottom
    close = snapshot.close
    if last_top is None or last_bottom is None or prior_bottom is None:
        return "unconfirmed"
    if last_bottom > prior_bottom and close > last_top:
        return "ascending_impulse_confirmed"
    if last_bottom > prior_bottom:
        return "higher_low_pullback"
    if last_bottom < prior_bottom:
        return "descending_structure"
    return "range"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="RNG")
    parser.add_argument("--asof", type=_utc_timestamp)
    parser.add_argument("--history-days", type=int, default=10)
    parser.add_argument("--position-shares", type=int, default=45)
    parser.add_argument("--position-average", type=float, default=46.50)
    parser.add_argument("--new-lot-shares", type=int, default=25)
    parser.add_argument("--new-lot-entry", type=float, default=46.70)
    parser.add_argument("--new-lot-protect", type=float, default=47.20)
    parser.add_argument("--all-exit", type=float, default=46.20)
    parser.add_argument("--add-shares", type=int, default=25)
    return parser


def main() -> int:
    load_dotenv(ROOT / ".env")
    args = _build_parser().parse_args()
    symbol = str(args.symbol).strip().upper()
    if not symbol or args.history_days < 7:
        raise ValueError("symbol is required and history-days must be at least 7")

    now = datetime.now(UTC)
    as_of = args.asof or now
    if as_of > now + timedelta(seconds=1):
        raise ValueError("asof cannot be in the future")
    policy = stock_data_policy_from_env()
    local_day = as_of.astimezone(EASTERN).date()
    schedule = build_xnys_schedule(
        local_day - timedelta(days=args.history_days),
        local_day,
    )
    if schedule.is_empty():
        raise RuntimeError("no XNYS sessions are available in the requested window")
    session_rows = schedule.iter_rows(named=True)
    sessions = list(session_rows)
    current = next(
        (row for row in sessions if row["trade_date"] == local_day),
        None,
    )
    first_open = sessions[0]["market_open_utc"]
    if not isinstance(first_open, datetime):
        raise RuntimeError("invalid XNYS calendar timestamp")
    bars, bar_coverage = fetch_sparse_bars_for_monitoring(
        (symbol,),
        first_open,
        as_of,
        feed=policy.feed,
    )
    if bars.is_empty():
        raise RuntimeError("market-data API returned no bars")

    quote_start = as_of - timedelta(minutes=3)
    quotes = fetch_quotes(
        (symbol,),
        quote_start,
        as_of,
        feed=policy.feed,
    )
    if quotes.is_empty():
        raise RuntimeError("market-data API returned no recent quotes")
    quote_row = quotes.sort("ts_utc").row(-1, named=True)
    quote_timestamp = quote_row["ts_utc"]
    if not isinstance(quote_timestamp, datetime):
        raise RuntimeError("invalid quote timestamp")
    quote = QuoteSnapshot(
        observed_at_utc=quote_timestamp.astimezone(UTC),
        bid=float(quote_row["bid_price"]),
        ask=float(quote_row["ask_price"]),
        age_seconds=(as_of - quote_timestamp.astimezone(UTC)).total_seconds(),
        feed=policy.feed,
        is_realtime=policy.is_realtime,
    )

    one_bars = resample_completed_bars(
        bars,
        schedule,
        interval_minutes=1,
        as_of_utc=as_of,
    )
    five_bars = resample_completed_bars(
        bars,
        schedule,
        interval_minutes=5,
        as_of_utc=as_of,
    )
    fifteen_bars = resample_completed_bars(
        bars,
        schedule,
        interval_minutes=15,
        as_of_utc=as_of,
    )
    one = build_timeframe_snapshot(one_bars, timeframe="1m")
    five = build_timeframe_snapshot(five_bars, timeframe="5m")
    fifteen = build_timeframe_snapshot(fifteen_bars, timeframe="15m")
    session_vwap = current_session_vwap(one_bars, trade_date=local_day)
    fibonacci = current_session_fibonacci(one_bars, trade_date=local_day)
    expansion = build_long_green_expansion(one_bars, trade_date=local_day)
    latest_bar_age_seconds = (
        None
        if one is None
        else (as_of - one.completed_at_utc.astimezone(UTC)).total_seconds()
    )

    market_is_open = False
    market_open: datetime | None = None
    market_close: datetime | None = None
    if current is not None:
        candidate_open = current["market_open_utc"]
        candidate_close = current["market_close_utc"]
        if isinstance(candidate_open, datetime) and isinstance(candidate_close, datetime):
            market_open = candidate_open.astimezone(UTC)
            market_close = candidate_close.astimezone(UTC)
            market_is_open = (
                market_open <= as_of < market_close
                and latest_bar_age_seconds is not None
                and -1 <= latest_bar_age_seconds <= 180
            )

    coverage_rows = bar_coverage.get("symbols")
    symbol_coverage = (
        coverage_rows[0]
        if isinstance(coverage_rows, list)
        and coverage_rows
        and isinstance(coverage_rows[0], dict)
        else {}
    )

    plan = PositionPlan(
        position_shares=args.position_shares,
        position_average=args.position_average,
        new_lot_shares=args.new_lot_shares,
        new_lot_entry=args.new_lot_entry,
        new_lot_protect=args.new_lot_protect,
        all_exit=args.all_exit,
        add_shares=args.add_shares,
    )
    advisory = build_trade_advisory(
        quote=quote,
        one_minute=one,
        five_minute=five,
        fifteen_minute=fifteen,
        long_green_expansion=expansion,
        session_vwap=session_vwap,
        market_is_open=market_is_open,
        plan=plan,
    )
    payload = {
        "schema_version": "technical_monitor.v1",
        "observed_at_utc": as_of.isoformat(),
        "observed_at_et": as_of.astimezone(EASTERN).isoformat(),
        "symbol": symbol,
        "market": {
            "regular_session_open": market_is_open,
            "market_open_utc": market_open.isoformat() if market_open else None,
            "market_close_utc": market_close.isoformat() if market_close else None,
            "feed": policy.feed,
            "realtime": policy.is_realtime,
            "bars": {
                "coverage_status": bar_coverage.get("status"),
                "missing_minute_count": symbol_coverage.get("missing_minute_count"),
                "latest_completed_bar_age_seconds": _rounded(
                    latest_bar_age_seconds
                ),
                "sparse_minutes_preserved": True,
            },
        },
        "quote": {
            "observed_at_utc": quote.observed_at_utc.isoformat(),
            "bid": _rounded(quote.bid),
            "ask": _rounded(quote.ask),
            "midpoint": _rounded(quote.midpoint),
            "spread_ratio": _rounded(quote.spread_ratio),
            "age_seconds": _rounded(quote.age_seconds),
        },
        "position_plan": asdict(plan),
        "timeframes": {
            "1m": _snapshot_payload(one),
            "5m": _snapshot_payload(five),
            "15m": _snapshot_payload(fifteen),
        },
        "structure": {
            "chan_fractal_5m": {
                "last_top": None if five is None else _rounded(five.last_confirmed_top),
                "last_bottom": (
                    None if five is None else _rounded(five.last_confirmed_bottom)
                ),
                "prior_bottom": (
                    None if five is None else _rounded(five.prior_confirmed_bottom)
                ),
            },
            "fibonacci_intraday": {
                key: round(value, 6) for key, value in fibonacci.items()
            },
            "wave_proxy_5m": _wave_proxy(five),
            "session_vwap": _rounded(session_vwap),
            "long_green_expansion": _expansion_payload(expansion),
        },
        "advisory": asdict(advisory),
        "safety": {
            "advisory_only": True,
            "order_endpoint_present": False,
            "automatic_order_authorized": False,
            "point_in_time_cutoff_utc": as_of.isoformat(),
            "incomplete_buckets_excluded": True,
        },
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
