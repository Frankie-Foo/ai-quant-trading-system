"""Read-only NVDA limit-order watcher with event-only Livermore pushes."""

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
from typing import Any
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
DEFAULT_CHANNEL_ID = "4edcd570-603f-4c5f-a070-db88c48a5c9b"
DEFAULT_STATE = ROOT / "runs" / "nvda-limit-monitor-state.json"
DEFAULT_LOG = ROOT / "runs" / "nvda-limit-monitor.jsonl"
DEFAULT_LOCK = ROOT / "runs" / "nvda-limit-monitor.lock"


@dataclass(frozen=True)
class MonitorConfig:
    symbol: str
    limit_price: float
    shares: int
    poll_seconds: int
    channel_id: str

    def __post_init__(self) -> None:
        if self.limit_price <= 0 or self.shares <= 0:
            raise ValueError("limit_price and shares must be positive")
        if self.poll_seconds < 1:
            raise ValueError("poll_seconds must be at least one second")


@dataclass(frozen=True)
class Signal:
    event: str
    reason: str
    message: str
    dedupe_key: str


def _read_state(path: Path, symbol: str) -> dict[str, Any]:
    if not path.exists():
        return {"symbol": symbol, "notified": {}}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("symbol") != symbol:
        return {"symbol": symbol, "notified": {}}
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


def evaluate(config: MonitorConfig, quotes: pl.DataFrame, now_utc: datetime) -> Signal | None:
    latest = _latest_quote(quotes)
    if latest is None:
        return None
    bid, ask, observed = latest
    if (now_utc - observed).total_seconds() > 30:
        return None
    if ask > config.limit_price:
        return None
    message = (
        "【利弗莫尔｜NVDA限价单触发提醒】\n"
        f"NVDA 最新卖价 {ask:.2f} 已触及你的限价 {config.limit_price:.2f}。\n"
        f"挂单数量 {config.shares} 股，买一 {bid:.2f}，卖一 {ask:.2f}。\n"
        "请立即到券商确认订单是否成交；本监控不提交订单，也不假设已经成交。"
    )
    return Signal("limit_touch", "ask_at_or_below_limit", message, "nvda:limit-touch")


def _push_client(config: MonitorConfig) -> LivermorePushClient:
    secret = os.getenv("VPS_LIVERMORE_APP_SECRET", "").strip()
    if not secret:
        raise RuntimeError("VPS_LIVERMORE_APP_SECRET is not configured")
    app_id = os.getenv("VPS_LIVERMORE_APP_ID", "vbot_ROHePX5GpUs1cr9I").strip()
    return LivermorePushClient(
        app_id=app_id,
        app_secret=SecretStr(secret),
        channel_id=config.channel_id,
    )


def run_once(
    config: MonitorConfig,
    *,
    state_path: Path,
    log_path: Path,
    push: bool,
    now_utc: datetime,
    feishu: FeishuBaseEventClient | None = None,
) -> tuple[Signal, ...]:
    state = _read_state(state_path, config.symbol)
    market_error: str | None = None
    quotes = pl.DataFrame()
    try:
        policy = stock_data_policy_from_env()
        quotes = fetch_quotes(
            (config.symbol,),
            now_utc - timedelta(seconds=45),
            now_utc,
            feed=policy.feed,
        )
    except Exception as exc:
        market_error = f"{type(exc).__name__}: {str(exc)[:120]}"
    signal = evaluate(config, quotes, now_utc) if market_error is None else None
    delivered: list[Signal] = []
    client: LivermorePushClient | None = None
    try:
        if signal is not None and signal.dedupe_key not in state["notified"]:
            if push:
                client = _push_client(config)
                message_id = client.push(signal.message)
                state["notified"][signal.dedupe_key] = {
                    "event": signal.event,
                    "reason": signal.reason,
                    "message_id": message_id,
                    "pushed_at_utc": now_utc.isoformat(),
                }
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
                        f"{config.symbol}:{item.dedupe_key}"
                    ),
                    symbol=config.symbol,
                    trade_date=now_utc.astimezone(EASTERN).date(),
                    triggered_at_utc=now_utc,
                    trigger_type=item.event,
                    operation=item.reason,
                    reason=item.reason,
                    message=item.message,
                    source="scripts.monitor_nvidia_limit",
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
            "symbol": config.symbol,
            "market_error": market_error,
            "signals": [item.event for item in delivered],
        },
    )
    return tuple(delivered)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only NVDA limit-order watcher")
    parser.add_argument("--limit-price", type=float, default=216.60)
    parser.add_argument("--shares", type=int, default=40)
    parser.add_argument("--poll-seconds", type=int, default=1)
    parser.add_argument("--channel-id")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--state-path", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--log-path", type=Path, default=DEFAULT_LOG)
    parser.add_argument("--lock-path", type=Path, default=DEFAULT_LOCK)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--no-push", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    load_dotenv(args.env_file or ROOT / ".env")
    config = MonitorConfig(
        symbol="NVDA",
        limit_price=args.limit_price,
        shares=args.shares,
        poll_seconds=args.poll_seconds,
        channel_id=(
            args.channel_id
            or os.getenv("VPS_LIVERMORE_CHANNEL_ID", "").strip()
            or DEFAULT_CHANNEL_ID
        ),
    )
    with ProcessLock(args.lock_path):
        feishu = FeishuBaseEventClient.from_environment(os.environ)
        while True:
            started = time.monotonic()
            now_utc = datetime.now(UTC)
            if now_utc.astimezone(EASTERN).time() >= clock_time(16, 0):
                return 0
            delivered = run_once(
                config,
                state_path=args.state_path,
                log_path=args.log_path,
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
            time.sleep(max(0.0, config.poll_seconds - (time.monotonic() - started)))


if __name__ == "__main__":
    raise SystemExit(main())
