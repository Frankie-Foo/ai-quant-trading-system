"""Warm the adaptive client store with observed historical SIP events."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

from data_plane.calendar import build_xnys_schedule
from data_plane.providers.alpaca import (
    fetch_quotes,
    fetch_sparse_bars_for_monitoring,
    fetch_trades,
    stock_data_policy_from_env,
)
from execution.sip_store import SipEventStore
from operations.adaptive_plan_config import AdaptivePlanConfig, load_adaptive_plan_config
from operations.adaptive_sip_warmup import build_warmup_events
from schedule.runtime import ProcessLock

ROOT = Path(__file__).resolve().parents[1]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--sip-db",
        type=Path,
        default=ROOT / "runs" / "sip-stream.sqlite3",
    )
    parser.add_argument(
        "--lock-file",
        type=Path,
        default=ROOT / "runs" / "adaptive-sip-warmup.lock",
    )
    return parser


def _symbols(config: AdaptivePlanConfig) -> tuple[tuple[str, ...], tuple[str, ...]]:
    market_symbols = {
        symbol
        for plan in config.plans
        for symbol in (
            plan.symbol,
            config.evidence[plan.plan_id].benchmark_symbol,
            config.evidence[plan.plan_id].sector_symbol,
        )
    }
    plan_symbols = {plan.symbol for plan in config.plans}
    return tuple(sorted(market_symbols)), tuple(sorted(plan_symbols))


def main() -> int:
    load_dotenv(ROOT / ".env")
    args = _parser().parse_args()
    config = load_adaptive_plan_config(args.config)
    policy = stock_data_policy_from_env()
    if not policy.is_realtime or policy.feed != "sip":
        raise RuntimeError("adaptive client requires CLOUD_MARKET_DATA_FEED=sip")
    market_symbols, plan_symbols = _symbols(config)
    first_date = min(plan.trade_date for plan in config.plans) - timedelta(days=10)
    last_date = max(plan.trade_date for plan in config.plans)
    schedule = build_xnys_schedule(first_date, last_date)
    if schedule.is_empty():
        raise RuntimeError("XNYS warmup schedule is unavailable")
    first_open = schedule.get_column("market_open_utc")[0]
    last_close = schedule.get_column("market_close_utc")[-1]
    if not isinstance(first_open, datetime) or not isinstance(last_close, datetime):
        raise RuntimeError("XNYS warmup schedule contains invalid timestamps")
    now_utc = datetime.now(UTC)
    last_completed_minute = now_utc.replace(second=0, microsecond=0)
    bars_end = min(last_completed_minute, last_close)
    if bars_end <= first_open:
        raise RuntimeError("historical warmup window has not opened")
    bars, coverage = fetch_sparse_bars_for_monitoring(
        market_symbols,
        first_open,
        bars_end,
        feed=policy.feed,
    )
    micro_end = now_utc
    micro_start = micro_end - timedelta(minutes=10)
    quotes = fetch_quotes(
        plan_symbols,
        micro_start,
        micro_end,
        feed=policy.feed,
    )
    trades = fetch_trades(
        plan_symbols,
        micro_start,
        micro_end,
        feed=policy.feed,
    )
    events = build_warmup_events(bars=bars, quotes=quotes, trades=trades)
    with ProcessLock(args.lock_file):
        store = SipEventStore(args.sip_db)
        store.append_many(events)
        counts = store.counts()
    print(
        json.dumps(
            {
                "ok": True,
                "schema_version": "adaptive_sip_warmup.v1",
                "symbol_count": len(market_symbols),
                "events_observed": len(events),
                "bar_coverage_status": coverage.get("status"),
                "store_counts": counts,
                "orders_submitted": 0,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
