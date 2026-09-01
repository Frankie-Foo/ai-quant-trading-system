"""Read-only per-symbol trade-plan watcher with event-only pushes."""

from __future__ import annotations

import argparse
import json
import math
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from datetime import time as clock_time
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import polars as pl
from dotenv import load_dotenv
from pydantic import SecretStr

from data_plane.providers.alpaca import fetch_quotes, stock_data_policy_from_env
from operations.feishu_base import FeishuBaseError, FeishuBaseEventClient
from operations.feishu_investment_events import record_monitor_trigger
from operations.livermore_push import LivermorePushClient
from schedule.runtime import ProcessLock

ROOT = Path(__file__).resolve().parents[1]
EASTERN = ZoneInfo("America/New_York")
DEFAULT_CHANNEL_ID = ""


@dataclass(frozen=True)
class WatchPlan:
    symbol: str
    entry_mode: Literal["below", "above"]
    entry_trigger: float
    first_shares: int
    full_shares: int
    add_trigger: float | None
    add_mode: Literal["retest", "reclaim"] | None
    hard_stop: float
    tp1: float
    tp2: float

    def __post_init__(self) -> None:
        if not self.symbol.strip() or self.entry_trigger <= 0:
            raise ValueError("symbol and entry_trigger are required")
        if self.full_shares < self.first_shares or self.first_shares <= 0:
            raise ValueError("share quantities are invalid")
        if self.hard_stop <= 0 or self.tp1 <= self.entry_trigger or self.tp2 <= self.tp1:
            raise ValueError("stop and target levels are invalid")


@dataclass(frozen=True)
class Signal:
    event: str
    reason: str
    message: str
    dedupe_key: str


def _latest_quote(quotes: pl.DataFrame) -> tuple[float, float, datetime] | None:
    if quotes.is_empty():
        return None
    row = quotes.sort("ts_utc").row(-1, named=True)
    try:
        bid = float(row["bid_price"])
        ask = float(row["ask_price"])
    except (TypeError, ValueError, OverflowError):
        return None
    observed = row["ts_utc"]
    if (
        not math.isfinite(bid)
        or not math.isfinite(ask)
        or bid <= 0
        or ask < bid
        or not isinstance(observed, datetime)
        or observed.tzinfo is None
    ):
        return None
    return bid, ask, observed


def _read_state(path: Path, symbol: str) -> dict[str, Any]:
    if not path.exists():
        return {"symbol": symbol, "phase": "watching", "notified": {}}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("symbol") != symbol:
        return {"symbol": symbol, "phase": "watching", "notified": {}}
    if not isinstance(value.get("notified"), dict):
        value["notified"] = {}
    return value


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _append_log(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")


def evaluate(
    plan: WatchPlan, state: dict[str, Any], quotes: pl.DataFrame, now_utc: datetime
) -> Signal | None:
    latest = _latest_quote(quotes)
    if latest is None:
        return None
    bid, ask, observed = latest
    if (now_utc - observed).total_seconds() > 30:
        return None
    if state.get("phase") in {"halted", "complete"}:
        return None
    if not state.get("active"):
        triggered = (
            ask <= plan.entry_trigger if plan.entry_mode == "below" else ask >= plan.entry_trigger
        )
        if not triggered:
            return None
        order_text = (
            f"触及 {plan.entry_trigger:.2f} 附近，"
            if plan.entry_mode == "below"
            else f"向上触发 {plan.entry_trigger:.2f}，"
        )
        return Signal(
            "entry",
            "entry_triggered",
            f"【利弗莫尔｜{plan.symbol}入场提醒】\n"
            f"{plan.symbol} {order_text}建议先执行 {plan.first_shares} 股。\n"
            f"当前买一 {bid:.2f}，卖一 {ask:.2f}。\n"
            "请确认券商是否成交；本监控不自动下单。",
            f"{plan.symbol}:entry",
        )
    if bid <= plan.hard_stop:
        return Signal(
            "stop",
            "hard_stop",
            f"【利弗莫尔｜{plan.symbol}硬止损提醒】\n"
            f"最新买一 {bid:.2f}，已触及硬止损 {plan.hard_stop:.2f}。\n"
            "按预案退出，不补仓。",
            f"{plan.symbol}:stop",
        )
    if plan.add_trigger is not None and not state.get("add_alerted"):
        if plan.add_mode == "retest" and not state.get("pullback_seen"):
            if bid <= plan.add_trigger:
                state["pullback_seen"] = True
        elif plan.add_mode == "reclaim" and not state.get("pullback_seen"):
            if bid >= plan.add_trigger:
                state["pullback_seen"] = True
        if state.get("pullback_seen"):
            reclaimed = bid >= plan.entry_trigger
            if reclaimed:
                return Signal(
                    "add",
                    "pullback_confirmed",
                    f"【利弗莫尔｜{plan.symbol}允许加仓】\n"
                    f"回踩/回收条件满足，建议追加 {plan.full_shares - plan.first_shares} 股，"
                    f"合计 {plan.full_shares} 股。\n"
                    f"当前买一 {bid:.2f}，硬止损 {plan.hard_stop:.2f}。\n"
                    "请确认上一笔已成交；本监控不自动下单。",
                    f"{plan.symbol}:add",
                )
    if not state.get("tp1_alerted") and bid >= plan.tp1:
        return Signal(
            "tp1",
            "first_target",
            f"【利弗莫尔｜{plan.symbol}第一档止盈】\n"
            f"最新买一 {bid:.2f}，达到 TP1 {plan.tp1:.2f}。\n"
            "按计划兑现第一档，剩余仓位止损抬到成本附近。",
            f"{plan.symbol}:tp1",
        )
    if not state.get("tp2_alerted") and bid >= plan.tp2:
        return Signal(
            "tp2",
            "second_target",
            f"【利弗莫尔｜{plan.symbol}第二档止盈】\n"
            f"最新买一 {bid:.2f}，达到 TP2 {plan.tp2:.2f}。\n"
            "兑现第二档，剩余仓位使用跟踪止盈。",
            f"{plan.symbol}:tp2",
        )
    return None


def _apply_signal(state: dict[str, Any], signal: Signal, now_utc: datetime) -> None:
    if signal.event == "entry":
        state["active"] = True
        state["entry_alerted_at_utc"] = now_utc.isoformat()
        state["phase"] = "active"
    elif signal.event == "add":
        state["add_alerted"] = True
    elif signal.event == "tp1":
        state["tp1_alerted"] = True
    elif signal.event == "tp2":
        state["tp2_alerted"] = True
    elif signal.event == "stop":
        state["phase"] = "halted"


def _push_client(channel_id: str) -> LivermorePushClient:
    secret = os.getenv("VPS_LIVERMORE_APP_SECRET", "").strip()
    if not secret:
        raise RuntimeError("VPS_LIVERMORE_APP_SECRET is not configured")
    app_id = os.getenv("VPS_LIVERMORE_APP_ID", "").strip()
    return LivermorePushClient(
        app_id=app_id,
        app_secret=SecretStr(secret),
        channel_id=channel_id,
    )


def run_once(
    plan: WatchPlan,
    *,
    state_path: Path,
    log_path: Path,
    channel_id: str,
    push: bool,
    now_utc: datetime,
    feishu: FeishuBaseEventClient | None = None,
) -> tuple[Signal, ...]:
    state = _read_state(state_path, plan.symbol)
    market_error: str | None = None
    quotes = pl.DataFrame()
    try:
        policy = stock_data_policy_from_env()
        quotes = fetch_quotes(
            (plan.symbol,),
            now_utc - timedelta(seconds=45),
            now_utc,
            feed=policy.feed,
        )
    except Exception as exc:
        market_error = f"{type(exc).__name__}: {str(exc)[:120]}"
    signal = evaluate(plan, state, quotes, now_utc) if market_error is None else None
    delivered: list[Signal] = []
    client: LivermorePushClient | None = None
    try:
        if signal is not None and signal.dedupe_key not in state["notified"]:
            if push:
                client = _push_client(channel_id)
                message_id = client.push(signal.message)
                state["notified"][signal.dedupe_key] = {
                    "event": signal.event,
                    "reason": signal.reason,
                    "message_id": message_id,
                    "pushed_at_utc": now_utc.isoformat(),
                }
                _apply_signal(state, signal, now_utc)
            delivered.append(signal)
    finally:
        if client is not None:
            client.close()
    if feishu is not None:
        for item in delivered:
            try:
                record_monitor_trigger(
                    feishu,
                    event_id=(
                        f"monitor:{now_utc.astimezone(EASTERN).date()}:"
                        f"{plan.symbol}:{item.dedupe_key}"
                    ),
                    symbol=plan.symbol,
                    trade_date=now_utc.astimezone(EASTERN).date(),
                    triggered_at_utc=now_utc,
                    trigger_type=item.event,
                    operation=item.reason,
                    reason=item.reason,
                    message=item.message,
                    source="scripts.monitor_watch_plan",
                )
            except FeishuBaseError as exc:
                print(json.dumps({"feishu_error": type(exc).__name__}), flush=True)
    state["last_poll_utc"] = now_utc.isoformat()
    state["last_market_error"] = market_error
    _write_state(state_path, state)
    _append_log(
        log_path,
        {
            "event": "poll",
            "observed_at_utc": now_utc,
            "symbol": plan.symbol,
            "market_error": market_error,
            "signals": [item.event for item in delivered],
        },
    )
    return tuple(delivered)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only per-symbol trade-plan watcher")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--entry-mode", choices=("below", "above"), required=True)
    parser.add_argument("--entry-trigger", type=float, required=True)
    parser.add_argument("--first-shares", type=int, required=True)
    parser.add_argument("--full-shares", type=int, required=True)
    parser.add_argument("--add-trigger", type=float)
    parser.add_argument("--add-mode", choices=("retest", "reclaim"))
    parser.add_argument("--hard-stop", type=float, required=True)
    parser.add_argument("--tp1", type=float, required=True)
    parser.add_argument("--tp2", type=float, required=True)
    parser.add_argument("--poll-seconds", type=int, default=1)
    parser.add_argument("--channel-id")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--state-path", type=Path, required=True)
    parser.add_argument("--log-path", type=Path, required=True)
    parser.add_argument("--lock-path", type=Path, required=True)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--no-push", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    load_dotenv(args.env_file or ROOT / ".env")
    plan = WatchPlan(
        symbol=args.symbol.upper(),
        entry_mode=args.entry_mode,
        entry_trigger=args.entry_trigger,
        first_shares=args.first_shares,
        full_shares=args.full_shares,
        add_trigger=args.add_trigger,
        add_mode=args.add_mode,
        hard_stop=args.hard_stop,
        tp1=args.tp1,
        tp2=args.tp2,
    )
    channel_id = (
        args.channel_id or os.getenv("VPS_LIVERMORE_CHANNEL_ID", "").strip() or DEFAULT_CHANNEL_ID
    )
    with ProcessLock(args.lock_path):
        feishu = FeishuBaseEventClient.from_environment(os.environ)
        while True:
            started = time.monotonic()
            now_utc = datetime.now(UTC)
            if now_utc.astimezone(EASTERN).time() >= clock_time(16, 0):
                return 0
            delivered = run_once(
                plan,
                state_path=args.state_path,
                log_path=args.log_path,
                channel_id=channel_id,
                push=not args.no_push,
                now_utc=now_utc,
                feishu=feishu,
            )
            for signal in delivered:
                print(
                    json.dumps(
                        {"event": signal.event, "reason": signal.reason},
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            if args.once:
                return 0
            time.sleep(max(0.0, args.poll_seconds - (time.monotonic() - started)))


if __name__ == "__main__":
    raise SystemExit(main())
