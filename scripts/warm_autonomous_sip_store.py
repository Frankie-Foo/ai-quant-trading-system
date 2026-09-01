"""Warm the autonomous local SIP store directly from Alpaca's licensed REST API."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from data_plane.calendar import build_xnys_schedule
from data_plane.providers.alpaca_direct import DirectAlpacaMarketDataClient
from execution.alpaca_sip_stream import SipEvent
from execution.sip_store import SipEventStore
from operations.autonomous_paper_config import (
    AutonomousPaperRuntimeConfig,
    load_autonomous_paper_config,
)
from operations.local_env import alpaca_paper_credentials, load_project_env
from schedule.runtime import ProcessLock

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class FetchWindows:
    bars_start_utc: datetime
    bars_end_utc: datetime
    micro_start_utc: datetime
    micro_end_utc: datetime


def build_fetch_windows(
    *,
    trade_date: date,
    observed_at_utc: datetime,
    history_days: int,
    incremental: bool,
) -> FetchWindows:
    if (
        observed_at_utc.tzinfo is None
        or observed_at_utc.utcoffset() != timedelta(0)
    ):
        raise ValueError("observed_at_utc must be timezone-aware UTC")
    if history_days < 7:
        raise ValueError("history_days must be at least 7")
    schedule = build_xnys_schedule(
        trade_date - timedelta(days=history_days),
        trade_date,
    )
    if schedule.is_empty():
        raise RuntimeError("autonomous SIP warmup schedule is unavailable")
    current = schedule.filter(schedule["trade_date"] == trade_date)
    if current.height != 1:
        raise RuntimeError("autonomous SIP trade date is not an XNYS session")
    first_open = schedule.get_column("market_open_utc")[0]
    current_open = current.get_column("market_open_utc")[0]
    current_close = current.get_column("market_close_utc")[0]
    if not all(
        isinstance(value, datetime)
        for value in (first_open, current_open, current_close)
    ):
        raise RuntimeError("autonomous SIP schedule timestamps are invalid")
    last_completed_minute = observed_at_utc.replace(second=0, microsecond=0)
    return FetchWindows(
        bars_start_utc=current_open if incremental else first_open,
        bars_end_utc=min(last_completed_minute, current_close),
        micro_start_utc=observed_at_utc - timedelta(minutes=10),
        micro_end_utc=observed_at_utc,
    )


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
        default=ROOT / "runs" / "autonomous-sip-warmup.lock",
    )
    parser.add_argument("--history-days", type=int, default=10)
    parser.add_argument("--incremental", action="store_true")
    return parser


def _symbols(
    config: AutonomousPaperRuntimeConfig,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    plan_symbols = tuple(
        sorted({bundle.plan.symbol for bundle in config.plans})
    )
    market_symbols = tuple(
        sorted(
            {
                symbol
                for bundle in config.plans
                for symbol in (
                    bundle.plan.symbol,
                    bundle.benchmark_symbol,
                    bundle.sector_symbol,
                )
            }
        )
    )
    return market_symbols, plan_symbols


def main() -> int:
    load_project_env(ROOT)
    args = _parser().parse_args()
    config = load_autonomous_paper_config(args.config)
    trade_dates = {bundle.plan.trade_date for bundle in config.plans}
    if len(trade_dates) != 1:
        raise ValueError("autonomous SIP warmup requires one shared trade date")
    trade_date = next(iter(trade_dates))
    observed_at = datetime.now(UTC)
    windows = build_fetch_windows(
        trade_date=trade_date,
        observed_at_utc=observed_at,
        history_days=int(args.history_days),
        incremental=bool(args.incremental),
    )
    market_symbols, plan_symbols = _symbols(config)
    key_id, secret_key = alpaca_paper_credentials(os.environ)
    client = DirectAlpacaMarketDataClient(
        key_id=key_id,
        secret_key=secret_key,
    )
    try:
        events: list[SipEvent] = []
        if windows.bars_end_utc > windows.bars_start_utc:
            events.extend(
                client.fetch_bars(
                    market_symbols,
                    start_utc=windows.bars_start_utc,
                    end_utc=windows.bars_end_utc,
                )
            )
        events.extend(
            client.fetch_quotes(
                plan_symbols,
                start_utc=windows.micro_start_utc,
                end_utc=windows.micro_end_utc,
            )
        )
        events.extend(
            client.fetch_trades(
                plan_symbols,
                start_utc=windows.micro_start_utc,
                end_utc=windows.micro_end_utc,
            )
        )
    finally:
        client.close()
    with ProcessLock(args.lock_file):
        store = SipEventStore(args.sip_db)
        store.append_many(tuple(events))
        counts = store.counts()
    print(
        json.dumps(
            {
                "ok": True,
                "schema_version": "autonomous_sip_warmup.v1",
                "mode": "incremental" if args.incremental else "full",
                "market_symbol_count": len(market_symbols),
                "plan_symbol_count": len(plan_symbols),
                "events_observed": len(events),
                "store_counts": counts,
                "orders_submitted": 0,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
