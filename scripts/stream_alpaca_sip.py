"""Run the single centralized SIP stream and persist bars/NBBO samples."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import aclosing
from datetime import UTC, datetime
from pathlib import Path

from execution.alpaca_sip_stream import AlpacaSipStream, SipBar
from execution.settings import ExecutionSettings
from execution.sip_store import SipEventStore
from schedule.runtime import JsonEventLogger, ProcessLock


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", required=True, help="Comma-separated locked symbols")
    parser.add_argument("--state-db", type=Path, default=Path("runs/sip-stream.sqlite3"))
    parser.add_argument("--lock-file", type=Path, default=Path("runs/alpaca-sip.lock"))
    parser.add_argument("--max-events", type=int, default=0)
    parser.add_argument("--max-seconds", type=float, default=0.0)
    return parser.parse_args()


async def _run(args: argparse.Namespace, logger: JsonEventLogger) -> None:
    settings = ExecutionSettings()  # type: ignore[call-arg]
    symbols = tuple(item.strip().upper() for item in args.symbols.split(",") if item.strip())
    stream = AlpacaSipStream(
        api_key=settings.alpaca_api_key_id.get_secret_value(),
        api_secret=settings.alpaca_api_secret_key.get_secret_value(),
        symbols=symbols,
    )
    store = SipEventStore(args.state_db)
    started = asyncio.get_running_loop().time()
    event_count = 0
    logger.emit(
        "sip_stream_starting",
        symbol_count=len(symbols),
        feed="sip",
        orders_submitted=0,
    )
    async with aclosing(stream.events()) as events:
        while True:
            remaining = args.max_seconds - (asyncio.get_running_loop().time() - started)
            if args.max_seconds > 0 and remaining <= 0:
                break
            try:
                event = (
                    await asyncio.wait_for(anext(events), timeout=remaining)
                    if args.max_seconds > 0
                    else await anext(events)
                )
            except (StopAsyncIteration, TimeoutError):
                break
            store.append(event)
            event_count += 1
            if isinstance(event, SipBar):
                logger.emit(
                    "sip_bar_persisted",
                    symbol=event.symbol,
                    market_ts_utc=event.ts_utc.isoformat(),
                )
            if args.max_events > 0 and event_count >= args.max_events:
                break
    logger.emit(
        "sip_stream_stopped",
        event_count=event_count,
        stopped_at_utc=datetime.now(UTC).isoformat(),
        counts=store.counts(),
        orders_submitted=0,
    )


def main() -> int:
    args = _parse_args()
    logger = JsonEventLogger(service="alpaca_sip_stream")
    with ProcessLock(args.lock_file):
        asyncio.run(_run(args, logger))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
