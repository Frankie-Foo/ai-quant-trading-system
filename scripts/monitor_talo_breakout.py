"""Advisory TALO breakout monitor with deduplicated Livermore pushes.

The monitor is read-only.  It never calls a broker order endpoint and never
assumes that a push means an order was filled.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock_time
from pathlib import Path
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl
from dotenv import load_dotenv
from pydantic import SecretStr

from data_plane.providers.alpaca import (
    fetch_quotes,
    fetch_sparse_bars_for_monitoring,
    stock_data_policy_from_env,
)
from operations.feishu_base import FeishuBaseError, FeishuBaseEventClient
from operations.feishu_investment_events import record_monitor_trigger
from operations.livermore_push import LivermorePushClient
from schedule.runtime import ProcessLock

ROOT = Path(__file__).resolve().parents[1]
EASTERN = ZoneInfo("America/New_York")
DEFAULT_CHANNEL_ID = ""
DEFAULT_STATE = ROOT / "runs" / "talo-breakout-monitor-state.json"
DEFAULT_LOG = ROOT / "runs" / "talo-breakout-monitor.jsonl"
DEFAULT_LOCK = ROOT / "runs" / "talo-breakout-monitor.lock"


@dataclass(frozen=True)
class TaloConfig:
    symbol: str
    trigger: float
    hard_stop: float
    scout_shares: int
    full_shares: int
    poll_seconds: int
    max_spread_ratio: float
    max_chase_ratio: float
    volume_multiple: float
    no_new_high_seconds: int
    entry_deadline_et: clock_time
    channel_id: str

    def __post_init__(self) -> None:
        if self.full_shares <= self.scout_shares <= 0:
            raise ValueError("full_shares must exceed positive scout_shares")
        if self.hard_stop >= self.trigger:
            raise ValueError("hard_stop must be below trigger")
        if self.poll_seconds < 1:
            raise ValueError("poll_seconds must be at least one second")


@dataclass(frozen=True)
class Signal:
    event: str
    reason: str
    message: str
    dedupe_key: str


def _completed_bars(bars: pl.DataFrame, now_utc: datetime) -> pl.DataFrame:
    if bars.is_empty() or "ts_utc" not in bars.columns:
        return pl.DataFrame()
    minute_boundary = now_utc.replace(second=0, microsecond=0)
    return bars.filter(pl.col("ts_utc") < minute_boundary).sort("ts_utc")


def _session_vwap(bars: pl.DataFrame) -> float | None:
    required = {"vwap", "close", "volume"}
    if bars.is_empty() or not required.issubset(bars.columns):
        return None
    valid = bars.filter(
        pl.col("volume").is_not_null() & (pl.col("volume") > 0) & pl.col("close").is_not_null()
    )
    if valid.is_empty():
        return None
    value = valid.select(
        (
            pl.when(pl.col("vwap").is_not_null()).then(pl.col("vwap")).otherwise(pl.col("close"))
            * pl.col("volume")
        ).sum()
        / pl.col("volume").sum()
    ).item()
    return float(value) if value is not None and math.isfinite(float(value)) else None


def _volume_ratio(bars: pl.DataFrame, multiple: float) -> float | None:
    if bars.is_empty() or "volume" not in bars.columns or bars.height < 6:
        return None
    values = [float(v) for v in bars.get_column("volume").to_list() if v is not None]
    if len(values) < 6:
        return None
    baseline = median(values[-21:-1])
    if baseline <= 0:
        return None
    return values[-1] / baseline if values[-1] >= baseline * multiple else None


def _quote_values(quote: pl.DataFrame) -> tuple[float, float, datetime] | None:
    if quote.is_empty():
        return None
    row = quote.sort("ts_utc").row(-1, named=True)
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
        or ask <= 0
        or ask < bid
        or not isinstance(observed, datetime)
        or observed.tzinfo is None
    ):
        return None
    return bid, ask, observed


def _state(path: Path, trade_date: date) -> dict[str, Any]:
    if not path.exists():
        return {"trade_date": trade_date.isoformat(), "phase": "watching", "notified": {}}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("trade_date") != trade_date.isoformat():
        return {"trade_date": trade_date.isoformat(), "phase": "watching", "notified": {}}
    if not isinstance(value.get("notified"), dict):
        value["notified"] = {}
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _append_log(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")


def _message_prefix(event: str) -> str:
    return f"【利弗莫尔｜TALO盘中监控｜{event}】"


def _evaluate(
    config: TaloConfig,
    state: dict[str, Any],
    bars: pl.DataFrame,
    quote: pl.DataFrame,
    *,
    now_utc: datetime,
    coverage_usable: bool,
) -> list[Signal]:
    if state.get("phase") in {"halted", "complete"}:
        return []
    values = _quote_values(quote)
    if values is not None and state.get("scout_alerted"):
        bid, _, observed = values
        if (now_utc - observed).total_seconds() <= 30 and bid <= config.hard_stop:
            return [
                Signal(
                    "stop",
                    "hard_stop",
                    f"{_message_prefix('止损')}\n"
                    f"TALO 最新买价 {bid:.2f}，已触及硬止损 {config.hard_stop:.2f}。\n"
                    "清仓，不补仓，不等待反弹。",
                    "talo:stop:hard",
                )
            ]
    if not coverage_usable:
        return []
    regular = _completed_bars(bars, now_utc)
    if values is None or regular.is_empty() or regular.height < 6:
        return []
    bid, ask, observed = values
    if (now_utc - observed).total_seconds() > 30:
        return []
    midpoint = (bid + ask) / 2
    spread = (ask - bid) / midpoint if midpoint > 0 else math.inf
    if spread > config.max_spread_ratio:
        return []
    vwap = _session_vwap(regular)
    if vwap is None:
        return []
    latest = regular.row(-1, named=True)
    latest_close = float(latest["close"])
    latest_high = float(latest["high"])
    volume_ratio = _volume_ratio(regular, config.volume_multiple)
    now_et = now_utc.astimezone(EASTERN)

    if state.get("scout_alerted"):
        scout_at_text = state.get("scout_alerted_at_utc")
        scout_at = datetime.fromisoformat(str(scout_at_text)) if scout_at_text else now_utc
        breakout_high = float(state.get("breakout_high", config.trigger))
        post_scout = regular.filter(pl.col("ts_utc") >= scout_at)
        post_high_value = (
            post_scout.get_column("high").max() if not post_scout.is_empty() else None
        )
        post_high = (
            float(post_high_value)
            if isinstance(post_high_value, (int, float))
            else breakout_high
        )
        if post_high > breakout_high:
            state["new_high_after_scout"] = True
        if post_scout.filter(
            (pl.col("low") <= config.trigger * 1.003) & (pl.col("close") >= config.trigger)
        ).height:
            state["retest_seen"] = True
        if bid <= config.hard_stop or latest_close <= config.hard_stop:
            return [
                Signal(
                    "stop",
                    "hard_stop",
                    f"{_message_prefix('止损')}\n"
                    f"TALO 最新买价 {bid:.2f}，跌破硬止损 {config.hard_stop:.2f}。\n"
                    "清仓，不补仓，不等待反弹。",
                    "talo:stop:hard",
                )
            ]
        if latest_close < vwap:
            return [
                Signal(
                    "stop",
                    "vwap_lost",
                    f"{_message_prefix('跌破VWAP')}\n"
                    f"TALO 收盘 {latest_close:.2f} 低于盘中 VWAP {vwap:.2f}。\n"
                    "取消加仓，已有仓位按预案退出。",
                    "talo:stop:vwap",
                )
            ]
        if (
            not state.get("add_alerted")
            and state.get("retest_seen")
            and state.get("new_high_after_scout")
            and latest_close >= config.trigger
            and latest_high > breakout_high
            and volume_ratio is not None
        ):
            return [
                Signal(
                    "add",
                    "retest_and_new_high",
                    f"{_message_prefix('允许加仓')}\n"
                    f"TALO 回踩 {config.trigger:.2f} 附近守住后再次创新高。\n"
                    f"建议追加 {config.full_shares - config.scout_shares} 股，"
                    f"合计 {config.full_shares} 股。\n"
                    f"现价 {ask:.2f}，VWAP {vwap:.2f}，"
                    f"量能约为近20根中位数的 {volume_ratio:.1f} 倍。\n"
                    f"硬止损 {config.hard_stop:.2f}；总投入上限 30,000 美元。",
                    "talo:add:retest-new-high",
                )
            ]
        if (now_utc - scout_at).total_seconds() >= config.no_new_high_seconds and not state.get(
            "new_high_after_scout"
        ):
            return [
                Signal(
                    "stop",
                    "no_new_high_timeout",
                    f"{_message_prefix('时间止损')}\n"
                    f"侦察仓后 {config.no_new_high_seconds // 60} 分钟没有创新高。\n"
                    "放弃继续持有，不加仓。",
                    "talo:stop:no-new-high",
                )
            ]
        return []

    if now_et.time() >= config.entry_deadline_et:
        return [
            Signal(
                "stop",
                "entry_deadline",
                f"{_message_prefix('放弃买入')}\n"
                f"已超过美东 {config.entry_deadline_et.strftime('%H:%M')}，未形成确认突破。\n"
                "今日不再开新仓。",
                "talo:stop:entry-deadline",
            )
        ]
    if ask > config.trigger * (1 + config.max_chase_ratio):
        return []
    if latest_close < config.hard_stop or bid <= config.hard_stop:
        return [
            Signal(
                "stop",
                "false_breakout",
                f"{_message_prefix('假突破')}\n"
                f"TALO 跌回 {config.hard_stop:.2f} 下方，突破失效。\n"
                "不买入，等待下一交易日重新评估。",
                "talo:stop:false-breakout",
            )
        ]
    if regular.height < 2 or volume_ratio is None:
        return []
    previous = regular.row(-2, named=True)
    if (
        float(previous["close"]) >= config.trigger
        and latest_close >= config.trigger
        and latest_close >= vwap
        and latest_high > float(previous["high"])
    ):
        return [
            Signal(
                "scout",
                "two_closes_breakout",
                f"{_message_prefix('侦察仓信号')}\n"
                f"TALO 连续两根1分钟收盘站上 {config.trigger:.2f}，"
                f"VWAP {vwap:.2f}，量能约为近20根中位数的 "
                f"{volume_ratio:.1f} 倍。\n"
                f"建议先买 {config.scout_shares} 股，限价不追高；硬止损 {config.hard_stop:.2f}。\n"
                "只有回踩守住并再次创新高，才允许加仓。",
                "talo:scout:two-closes",
            )
        ]
    return []


def _apply_signal(
    state: dict[str, Any],
    signal: Signal,
    now_utc: datetime,
    bars: pl.DataFrame,
) -> None:
    if signal.event == "scout":
        state["scout_alerted"] = True
        state["scout_alerted_at_utc"] = now_utc.isoformat()
        state["breakout_high"] = float(bars.get_column("high")[-1])
        state["phase"] = "scout"
    elif signal.event == "add":
        state["add_alerted"] = True
        state["phase"] = "full"
    elif signal.event == "stop":
        state["phase"] = "halted"


def _push_client(config: TaloConfig) -> LivermorePushClient:
    secret = os.getenv("VPS_LIVERMORE_APP_SECRET", "").strip()
    if not secret:
        raise RuntimeError("VPS_LIVERMORE_APP_SECRET is not configured")
    app_id = os.getenv("VPS_LIVERMORE_APP_ID", "").strip()
    return LivermorePushClient(
        app_id=app_id,
        app_secret=SecretStr(secret),
        channel_id=config.channel_id,
    )


def run_once(
    config: TaloConfig,
    *,
    state_path: Path,
    log_path: Path,
    push: bool,
    now_utc: datetime,
    actual_scout_shares: int = 0,
    feishu: FeishuBaseEventClient | None = None,
) -> tuple[Signal, ...]:
    state = _state(state_path, now_utc.astimezone(EASTERN).date())
    if actual_scout_shares > 0 and not state.get("scout_alerted"):
        state.update(
            {
                "scout_alerted": True,
                "actual_scout_shares": actual_scout_shares,
                "scout_alerted_at_utc": now_utc.isoformat(),
                "breakout_high": config.trigger,
                "phase": "scout",
            }
        )
    market_error: str | None = None
    bars = pl.DataFrame()
    quotes = pl.DataFrame()
    coverage_usable = False
    try:
        policy = stock_data_policy_from_env()
        rth_open = datetime.combine(
            now_utc.astimezone(EASTERN).date(), clock_time(9, 30), tzinfo=EASTERN
        ).astimezone(UTC)
        bars, coverage = fetch_sparse_bars_for_monitoring(
            (config.symbol,), rth_open, now_utc, feed=policy.feed
        )
        quotes = fetch_quotes(
            (config.symbol,), now_utc - timedelta(minutes=3), now_utc, feed=policy.feed
        )
        coverage_usable = coverage.get("status") == "observed" and not bool(
            coverage.get("fallback_recommended")
        )
        if not coverage_usable:
            market_error = "market_data_coverage_degraded"
    except Exception as exc:
        market_error = f"{type(exc).__name__}: {str(exc)[:120]}"

    signals = _evaluate(
        config,
        state,
        bars,
        quotes,
        now_utc=now_utc,
        coverage_usable=coverage_usable,
    )
    if (
        market_error
        and os.getenv("TALO_PUSH_STATUS", "").strip().lower() == "true"
        and not state.setdefault("notified", {}).get("talo:status:coverage-degraded")
    ):
        signals.append(
            Signal(
                "status",
                "market_data_coverage_degraded",
                f"{_message_prefix('行情状态')}\n"
                "TALO 行情覆盖存在缺口，当前只保留观测，不生成买入或加仓结论。\n"
                "监控会继续重试，覆盖恢复后再按突破、VWAP和量能条件推送。",
                "talo:status:coverage-degraded",
            )
        )
    delivered: list[Signal] = []
    client: LivermorePushClient | None = None
    try:
        if push and signals:
            client = _push_client(config)
        for signal in signals:
            notified = state.setdefault("notified", {})
            if signal.dedupe_key in notified:
                continue
            if push:
                if client is None:
                    raise RuntimeError("Livermore push client is unavailable")
                message_id = client.push(signal.message)
                notified[signal.dedupe_key] = {
                    "event": signal.event,
                    "reason": signal.reason,
                    "message_id": message_id,
                    "pushed_at_utc": now_utc.isoformat(),
                }
                _apply_signal(
                    state,
                    signal,
                    now_utc,
                    _completed_bars(bars, now_utc),
                )
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
                    source="scripts.monitor_talo_breakout",
                )
            except FeishuBaseError as exc:
                print(json.dumps({"feishu_error": type(exc).__name__}), flush=True)
    state["last_poll_utc"] = now_utc.isoformat()
    state["last_market_error"] = market_error
    _write_json(state_path, state)
    _append_log(
        log_path,
        {
            "event": "poll",
            "observed_at_utc": now_utc,
            "symbol": config.symbol,
            "coverage_usable": coverage_usable,
            "market_error": market_error,
            "signals": [signal.event for signal in delivered],
        },
    )
    return tuple(delivered)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only TALO breakout monitor")
    parser.add_argument("--trigger", type=float, default=15.26)
    parser.add_argument("--hard-stop", type=float, default=15.20)
    parser.add_argument("--scout-shares", type=int, default=600)
    parser.add_argument("--full-shares", type=int, default=1900)
    parser.add_argument("--poll-seconds", type=int, default=1)
    parser.add_argument("--actual-scout-shares", type=int, default=0)
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
    config = TaloConfig(
        symbol="TALO",
        trigger=args.trigger,
        hard_stop=args.hard_stop,
        scout_shares=args.scout_shares,
        full_shares=args.full_shares,
        poll_seconds=args.poll_seconds,
        max_spread_ratio=0.003,
        max_chase_ratio=0.005,
        volume_multiple=1.5,
        no_new_high_seconds=600,
        entry_deadline_et=clock_time(11, 30),
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
                print(
                    json.dumps(
                        {"event": "monitor_end", "reason": "market_close"},
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                return 0
            delivered = run_once(
                config,
                state_path=args.state_path,
                log_path=args.log_path,
                push=not args.no_push,
                now_utc=now_utc,
                actual_scout_shares=args.actual_scout_shares,
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
            if any(signal.event == "stop" for signal in delivered):
                return 0
            if args.once:
                return 0
            time.sleep(max(0.0, config.poll_seconds - (time.monotonic() - started)))


if __name__ == "__main__":
    raise SystemExit(main())
