"""Run the centralized causal ORB session; writes require every coded safety gate."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import aclosing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from data_plane.calendar import build_xnys_schedule
from execution.alpaca_paper import CloudPaperBroker
from execution.alpaca_sip_stream import PlatformSipStream, SipEvent
from execution.engine import PaperExecutionEngine
from execution.ledger import OrderLedger
from execution.live_session import LiveSessionProcessor
from execution.locked_selection import load_locked_selection
from execution.order_state import OrderState
from execution.recovery import reconcile_startup
from execution.session_ledger import PaperSessionLedger, PaperSessionStatus
from execution.settings import ExecutionSettings
from execution.sip_store import SipEventStore
from execution.time_exit import TimeExitCoordinator, TimeExitLedger
from kernel.config import load_config
from operations.readiness import MaturityEvidence, assess_product_readiness
from schedule.runtime import JsonEventLogger, ProcessLock

ROOT = Path(__file__).resolve().parents[1]
NEW_YORK = ZoneInfo("America/New_York")
TIME_EXIT_POLL_SECONDS = 5.0


async def _read_next_event(events: AsyncIterator[SipEvent]) -> SipEvent:
    return await events.__anext__()


async def _poll_next_event(
    events: AsyncIterator[SipEvent],
    *,
    pending: asyncio.Task[SipEvent] | None,
    timeout: float,
) -> tuple[asyncio.Task[SipEvent] | None, SipEvent | None]:
    """Poll without cancelling an in-flight async-generator read on timeout."""
    if timeout <= 0:
        raise ValueError("event poll timeout must be positive")
    task = pending or asyncio.create_task(_read_next_event(events))
    done, _ = await asyncio.wait({task}, timeout=timeout)
    if not done:
        return task, None
    return None, task.result()


async def _cancel_pending_event(task: asyncio.Task[SipEvent] | None) -> None:
    if task is None:
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, StopAsyncIteration):
        pass


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", type=_parse_date)
    parser.add_argument("--data-root", type=Path, default=ROOT / "data")
    parser.add_argument("--sip-db", type=Path, default=ROOT / "runs/sip-stream.sqlite3")
    parser.add_argument("--order-db", type=Path, default=ROOT / "runs/paper-orders.sqlite3")
    parser.add_argument(
        "--readiness-evidence",
        type=Path,
        default=ROOT / "deploy/maturity-evidence.example.json",
    )
    parser.add_argument("--lock-file", type=Path, default=ROOT / "runs/paper-session.lock")
    parser.add_argument(
        "--sip-lock-file",
        type=Path,
        default=ROOT / "runs/alpaca-sip.lock",
    )
    parser.add_argument("--max-seconds", type=float, default=0.0)
    return parser.parse_args()


def _current_trade_date(now_utc: datetime) -> date:
    local_date = now_utc.astimezone(NEW_YORK).date()
    if build_xnys_schedule(local_date, local_date).height != 1:
        raise RuntimeError("today is not an XNYS trading session")
    return local_date


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def _emit_time_exit_results(
    time_exits: TimeExitCoordinator,
    logger: JsonEventLogger,
    *,
    now_utc: datetime,
) -> None:
    """Poll durable time stops independently of market-data traffic."""
    for exit_result in time_exits.run_due(now_utc=now_utc):
        logger.emit(
            "time_exit_checked",
            plan_id=exit_result.plan_id,
            symbol=exit_result.symbol,
            status=exit_result.status.value,
            dry_run=exit_result.dry_run,
            broker_order_id=exit_result.broker_order_id,
            cancelled_order_ids=exit_result.cancelled_order_ids,
            detail=exit_result.detail,
        )


async def _run(args: argparse.Namespace, logger: JsonEventLogger) -> None:
    cfg = load_config(ROOT / "config.yaml")
    if args.trade_date is None:
        try:
            trade_date = _current_trade_date(datetime.now(UTC))
        except RuntimeError:
            logger.emit("paper_session_skipped", reason="not_xnys_session")
            return
    else:
        trade_date = args.trade_date
    selection = load_locked_selection(
        args.data_root,
        trade_date,
        min_rvol=cfg.universe.min_rvol,
    )
    calendar = build_xnys_schedule(trade_date, trade_date)
    if calendar.height != 1:
        raise ValueError("target XNYS session is unavailable")
    row = calendar.row(0, named=True)
    market_open = row["market_open_utc"]
    market_close = row["market_close_utc"]
    if not isinstance(market_open, datetime) or not isinstance(market_close, datetime):
        raise ValueError("calendar timestamps are invalid")
    is_half_day = bool(row["is_half_day"])
    if datetime.now(UTC) >= market_close and args.trade_date is None:
        logger.emit(
            "paper_session_skipped",
            reason="persistent_timer_fired_after_market_close",
            trade_date=trade_date.isoformat(),
        )
        return
    if datetime.now(UTC) >= market_close:
        raise RuntimeError("target XNYS session has already closed")

    settings = ExecutionSettings()  # type: ignore[call-arg]
    evidence = MaturityEvidence.model_validate(_read_json(args.readiness_evidence))
    readiness = assess_product_readiness(evidence)
    broker = CloudPaperBroker(
        base_url=settings.cloud_platform_base_url,
        token=settings.cloud_paper_api_token,
        writes_enabled=settings.broker_write_enabled,
    )
    order_ledger = OrderLedger(args.order_db)
    engine = PaperExecutionEngine(
        broker=broker,
        ledger=order_ledger,
        config=cfg,
        paper_authorized=readiness.paper_eligible,
    )
    recovery = reconcile_startup(order_ledger, broker, at_utc=datetime.now(UTC))
    logger.emit(
        "paper_startup_reconciliation",
        checked_orders=recovery.checked_orders,
        matched_orders=recovery.matched_orders,
        unresolved_orders=recovery.unresolved_orders,
        position_symbols=recovery.position_symbols,
        unmatched_position_symbols=recovery.unmatched_position_symbols,
        match_rate=recovery.match_rate,
        safe_to_resume=recovery.safe_to_resume,
    )
    if not recovery.safe_to_resume:
        broker.close()
        raise RuntimeError("Paper startup reconciliation failed closed")
    time_exits = TimeExitCoordinator(
        order_ledger=order_ledger,
        exit_ledger=TimeExitLedger(args.order_db),
        broker=broker,
        paper_authorized=readiness.paper_eligible,
    )
    session_ledger = PaperSessionLedger(args.order_db)
    session_ledger.start(
        trade_date=trade_date,
        started_at_utc=datetime.now(UTC),
        expected_close_utc=market_close,
        reconciliation_match_rate=recovery.match_rate,
    )
    processor = LiveSessionProcessor(
        selection=selection,
        session_open_utc=market_open,
        session_close_utc=market_close,
        is_half_day=is_half_day,
        store=SipEventStore(args.sip_db),
        engine=engine,
        broker=broker,
        config=cfg,
        kill_switch_active=settings.trading_kill_switch,
    )
    stream = PlatformSipStream(
        base_url=settings.cloud_platform_base_url,
        token=settings.cloud_market_data_api_token,
        symbols=selection.symbols,
    )
    await stream.ensure_subscription(
        replay_from_utc=market_open,
        expires_at_utc=market_close + timedelta(minutes=5),
    )
    started = asyncio.get_running_loop().time()
    session_remaining = max(0.0, (market_close - datetime.now(UTC)).total_seconds())
    runtime_limit = (
        min(args.max_seconds, session_remaining)
        if args.max_seconds > 0
        else session_remaining
    )
    event_count = 0
    order_count = 0
    last_event_monotonic: float | None = None
    logger.emit(
        "paper_session_starting",
        trade_date=trade_date.isoformat(),
        symbol_count=len(selection.symbols),
        product_stage=readiness.stage.value,
        paper_authorized=readiness.paper_eligible,
        broker_write_enabled=settings.broker_write_enabled,
        kill_switch_active=settings.trading_kill_switch,
    )
    try:
        async with aclosing(stream.events()) as events:
            loop = asyncio.get_running_loop()
            next_exit_poll = loop.time()
            pending_event: asyncio.Task[SipEvent] | None = None
            try:
                while True:
                    now_monotonic = loop.time()
                    if now_monotonic >= next_exit_poll:
                        _emit_time_exit_results(
                            time_exits,
                            logger,
                            now_utc=datetime.now(UTC),
                        )
                        next_exit_poll = now_monotonic + TIME_EXIT_POLL_SECONDS
                    remaining = runtime_limit - (loop.time() - started)
                    if remaining <= 0:
                        break
                    until_exit_poll = max(0.05, next_exit_poll - loop.time())
                    try:
                        pending_event, event = await _poll_next_event(
                            events,
                            pending=pending_event,
                            timeout=min(until_exit_poll, remaining),
                        )
                    except StopAsyncIteration:
                        if args.max_seconds <= 0:
                            raise RuntimeError(
                                "SIP stream ended before the market close"
                            ) from None
                        break
                    if event is None:
                        if datetime.now(UTC) >= market_open:
                            last_seen = last_event_monotonic or started
                            if (
                                loop.time() - last_seen
                                > cfg.market_data.sip_event_stale_seconds
                            ):
                                raise RuntimeError(
                                    "cloud SIP collector produced no fresh market event"
                                ) from None
                        continue
                    event_count += 1
                    last_event_monotonic = loop.time()
                    result = processor.process(event, received_at_utc=datetime.now(UTC))
                    if result is None:
                        continue
                    submitted = result.lifecycle.state in {
                        OrderState.SUBMITTED,
                        OrderState.PARTIALLY_FILLED,
                        OrderState.FILLED,
                    }
                    order_count += int(submitted)
                    logger.emit(
                        "trade_plan_decided",
                        plan_id=result.lifecycle.plan_id,
                        symbol=result.lifecycle.symbol,
                        state=result.lifecycle.state.value,
                        approved=result.verdict.approved,
                        failure_code=(
                            None
                            if result.verdict.failure_code is None
                            else result.verdict.failure_code.value
                        ),
                        dry_run=result.dry_run,
                        replayed=result.replayed,
                    )
            finally:
                await _cancel_pending_event(pending_event)
    except BaseException as exc:
        logger.emit(
            "paper_session_failed",
            level="error",
            error_type=type(exc).__name__,
            event_count=event_count,
            orders_submitted=order_count,
        )
        session_ledger.finish(
            trade_date=trade_date,
            ended_at_utc=datetime.now(UTC),
            status=PaperSessionStatus.FAILED,
            event_count=event_count,
            orders_submitted=order_count,
            error_type=type(exc).__name__,
        )
        raise
    else:
        session_ledger.finish(
            trade_date=trade_date,
            ended_at_utc=datetime.now(UTC),
            status=(
                PaperSessionStatus.COMPLETED
                if args.max_seconds <= 0
                else PaperSessionStatus.BOUNDED_SMOKE
            ),
            event_count=event_count,
            orders_submitted=order_count,
        )
    finally:
        broker.close()
    logger.emit(
        "paper_session_stopped",
        event_count=event_count,
        attempted_symbols=sorted(processor.attempted_symbols),
        orders_submitted=order_count,
    )


def main() -> int:
    args = _parse_args()
    logger = JsonEventLogger(service="paper_session")
    with ProcessLock(args.lock_file):
        with ProcessLock(args.sip_lock_file):
            asyncio.run(_run(args, logger))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
