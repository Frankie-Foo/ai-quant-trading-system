"""Read-only 10-second monitor for one Alpaca symbol."""

# ruff: noqa: E501

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
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl
from dotenv import load_dotenv
from pydantic import SecretStr

from data_plane.http import DownloadError
from data_plane.providers.alpaca import (
    fetch_quotes,
    fetch_sparse_bars_for_monitoring,
    fetch_trades,
)
from operations.feishu_base import FeishuBaseError, FeishuBaseEventClient
from operations.feishu_investment_events import record_monitor_trigger
from operations.livermore_push import LivermorePushClient
from schedule.runtime import ProcessLock

ROOT = Path(__file__).resolve().parents[1]
EASTERN = ZoneInfo("America/New_York")
DEFAULT_CHANNEL_ID = "4edcd570-603f-4c5f-a070-db88c48a5c9b"
DEFAULT_APP_ID = "vbot_pATI_VCgdkiJn1Sw"
DEFAULT_POSITIONS_PATH = ROOT / "runs" / "target-monitors" / "positions.json"
BATCH_COUNT = 4
EVENT_COOLDOWN = timedelta(minutes=10)
MONITOR_MODE = os.getenv("VPS_TARGET_MONITOR_MODE", "exit_only").strip().lower()
ENTRY_MONITORING_ENABLED = MONITOR_MODE == "entry_and_exit"


@dataclass(frozen=True)
class Target:
    symbol: str
    role: str
    entry_low: float
    entry_high: float
    tp1: float | None
    tp2_low: float | None
    tp2_high: float | None
    hard_sl: float | None
    max_shares: int
    max_spread_ratio: float
    cutoff_et: clock_time
    benchmark: str | None = None
    budget_usd: float = 0.0
    tranche_shares: tuple[int, ...] | None = None
    risk_note: str = ""
    entry_confirm_polls: int = 1


@dataclass(frozen=True)
class Snapshot:
    observed_at_utc: datetime
    last: float | None
    bid: float | None
    ask: float | None
    quote_at_utc: datetime | None
    minute_volume: int | None
    session_volume: int | None
    vwap: float | None
    volume_ratio: float | None
    session_return: float | None
    benchmark_return: float | None

    @property
    def spread_ratio(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        midpoint = (self.bid + self.ask) / 2
        return None if midpoint <= 0 else (self.ask - self.bid) / midpoint


@dataclass(frozen=True)
class Event:
    kind: str
    title: str
    detail: str


TARGETS = {
    "MRVL": Target(
        symbol="MRVL",
        role="主攻",
        entry_low=213.5,
        entry_high=216.0,
        tp1=222.5,
        tp2_low=225.5,
        tp2_high=226.5,
        hard_sl=209.0,
        max_shares=21_000,
        max_spread_ratio=0.0025,
        cutoff_et=clock_time(15, 30),
    ),
    "ON": Target(
        symbol="ON",
        role="次主攻",
        entry_low=87.2,
        entry_high=88.0,
        tp1=91.5,
        tp2_low=92.8,
        tp2_high=93.5,
        hard_sl=84.8,
        max_shares=34_100,
        max_spread_ratio=0.0020,
        cutoff_et=clock_time(15, 30),
        benchmark="MRVL",
    ),
    "ALAB": Target(
        symbol="ALAB",
        role="高弹性日内",
        entry_low=347.0,
        entry_high=351.0,
        tp1=362.0,
        tp2_low=368.0,
        tp2_high=372.0,
        hard_sl=337.0,
        max_shares=4_300,
        max_spread_ratio=0.0030,
        cutoff_et=clock_time(15, 30),
    ),
    "NVDA": Target(
        symbol="NVDA",
        role="核心强势",
        entry_low=222.30,
        entry_high=222.60,
        tp1=226.70,
        tp2_low=None,
        tp2_high=None,
        hard_sl=217.90,
        max_shares=44,
        max_spread_ratio=0.0015,
        cutoff_et=clock_time(15, 30),
        budget_usd=10_000,
        tranche_shares=(6, 9, 11, 9, 9),
        risk_note="成交后止损 217.90；TP1 226.70。",
        entry_confirm_polls=2,
    ),
    "DIS": Target(
        symbol="DIS",
        role="回撤确认",
        entry_low=103.21,
        entry_high=103.35,
        tp1=None,
        tp2_low=None,
        tp2_high=None,
        hard_sl=101.20,
        max_shares=96,
        max_spread_ratio=0.0025,
        cutoff_et=clock_time(15, 30),
        budget_usd=10_000,
        tranche_shares=(14, 19, 24, 19, 20),
        risk_note="未重新站上 103.21 不追价；成交后止损 101.20；截图未给止盈位。",
        entry_confirm_polls=2,
    ),
    "LLY": Target(
        symbol="LLY",
        role="高波动回撤",
        entry_low=1184.00,
        entry_high=1190.00,
        tp1=None,
        tp2_low=None,
        tp2_high=None,
        hard_sl=None,
        max_shares=8,
        max_spread_ratio=0.0030,
        cutoff_et=clock_time(15, 30),
        budget_usd=10_000,
        tranche_shares=(1, 2, 2, 1, 2),
        risk_note="仅回到 1184–1190 才考虑侦察仓；成交后止损抬到实际成本价。持仓比例 0.97 需人工核对。",
        entry_confirm_polls=2,
    ),
}


def _latest(frame: pl.DataFrame, symbol: str) -> dict[str, Any] | None:
    if frame.is_empty() or "symbol" not in frame.columns:
        return None
    rows = frame.filter(pl.col("symbol") == symbol).sort("ts_utc")
    return rows.row(-1, named=True) if not rows.is_empty() else None


def _number(value: Any) -> float | None:
    if value is None:
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _fresh_row(
    row: dict[str, Any] | None,
    now_utc: datetime,
    *,
    max_age_seconds: float,
) -> dict[str, Any] | None:
    if row is None:
        return None
    observed = row.get("ts_utc")
    if not isinstance(observed, datetime) or observed.tzinfo is None:
        return None
    age = (now_utc - observed.astimezone(UTC)).total_seconds()
    if age < -1 or age > max_age_seconds:
        return None
    return row


def _session_stats(
    bars: pl.DataFrame, symbol: str
) -> tuple[int | None, int | None, float | None, float | None, float | None]:
    if bars.is_empty() or "symbol" not in bars.columns:
        return None, None, None, None, None
    rows = bars.filter(pl.col("symbol") == symbol).sort("ts_utc")
    if rows.is_empty():
        return None, None, None, None, None
    volume = [int(value or 0) for value in rows.get_column("volume").to_list()]
    closes = [_number(value) for value in rows.get_column("close").to_list()]
    vwap_values = (
        [_number(value) for value in rows.get_column("vwap").to_list()]
        if "vwap" in rows.columns
        else closes
    )
    usable = [
        (price, qty)
        for price, qty in zip(vwap_values, volume, strict=True)
        if price is not None and qty > 0
    ]
    session_vwap = (
        sum(price * qty for price, qty in usable) / sum(qty for _, qty in usable)
        if usable
        else None
    )
    recent_volume = volume[-1] if volume else None
    session_volume = sum(volume) if volume else None
    ratio = None
    if len(volume) >= 6 and recent_volume is not None:
        baseline = median(float(value) for value in volume[-6:-1])
        ratio = recent_volume / baseline if baseline > 0 else None
    usable_closes = [value for value in closes if value is not None and value > 0]
    session_return = usable_closes[-1] / usable_closes[0] - 1 if len(usable_closes) >= 2 else None
    return recent_volume, session_volume, session_vwap, ratio, session_return


def fetch_snapshot(target: Target, now_utc: datetime) -> Snapshot:
    symbols = (target.symbol, target.benchmark) if target.benchmark else (target.symbol,)
    start_utc = now_utc - timedelta(seconds=20)
    session_start = datetime.combine(
        now_utc.astimezone(EASTERN).date(), clock_time(4, 0), tzinfo=EASTERN
    ).astimezone(UTC)
    bars, coverage = fetch_sparse_bars_for_monitoring(symbols, session_start, now_utc)
    coverage_usable = coverage.get("status") == "observed" and not bool(
        coverage.get("fallback_recommended")
    )
    quotes = fetch_quotes(symbols, start_utc, now_utc)
    trades = fetch_trades(symbols, start_utc, now_utc)
    quote = _fresh_row(_latest(quotes, target.symbol), now_utc, max_age_seconds=30)
    trade = _fresh_row(_latest(trades, target.symbol), now_utc, max_age_seconds=30)
    last = _number(trade.get("price")) if trade else None
    if last is None:
        bar = _fresh_row(
            _latest(bars, target.symbol) if coverage_usable else None,
            now_utc,
            max_age_seconds=180,
        )
        last = _number(bar.get("close")) if bar else None
    if last is None and quote:
        bid = _number(quote.get("bid_price"))
        ask = _number(quote.get("ask_price"))
        last = (bid + ask) / 2 if bid is not None and ask is not None else None
    bid = _number(quote.get("bid_price")) if quote else None
    ask = _number(quote.get("ask_price")) if quote else None
    quote_at = quote.get("ts_utc") if quote else None
    if not isinstance(quote_at, datetime):
        quote_at = None
    stats_bars = bars if coverage_usable else pl.DataFrame()
    minute_volume, session_volume, vwap, volume_ratio, session_return = _session_stats(
        stats_bars, target.symbol
    )
    benchmark_return = _session_stats(stats_bars, target.benchmark)[4] if target.benchmark else None
    return Snapshot(
        observed_at_utc=now_utc,
        last=last,
        bid=bid,
        ask=ask,
        quote_at_utc=quote_at,
        minute_volume=minute_volume,
        session_volume=session_volume,
        vwap=vwap,
        volume_ratio=volume_ratio,
        session_return=session_return,
        benchmark_return=benchmark_return,
    )


def _fmt(value: float | int | None, digits: int = 2) -> str:
    return "N/A" if value is None else f"{value:,.{digits}f}"


def _state_defaults() -> dict[str, Any]:
    return {
        "conditions": {},
        "last_event_at": {},
        "last_summary_at": None,
        "buy_seen": False,
        "buy_trigger_count": 0,
        "entry_ready_polls": 0,
        "below_vwap_polls": 0,
    }


def _read_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _state_defaults()
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("monitor state must be a JSON object")
    state = _state_defaults()
    state.update(value)
    if not isinstance(state.get("buy_trigger_count"), int):
        state["buy_trigger_count"] = 0
    if state["buy_trigger_count"] == 0 and isinstance(
        state.get("last_event_at", {}).get("buy_ready"), str
    ):
        # Backfill the first trigger for state files created before counting existed.
        state["buy_trigger_count"] = 1
    return state


def _read_positions(path: Path) -> dict[str, int]:
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    positions = value.get("positions", value) if isinstance(value, dict) else None
    if not isinstance(positions, dict):
        raise ValueError("positions must be a JSON object")
    result: dict[str, int] = {}
    for symbol, shares in positions.items():
        quantity = int(shares)
        if quantity < 0:
            raise ValueError("position shares cannot be negative")
        result[str(symbol).upper()] = quantity
    return result


def _batch_sizes(target: Target) -> tuple[int, ...]:
    if target.tranche_shares is not None:
        return target.tranche_shares
    tranche = math.ceil(target.max_shares / BATCH_COUNT)
    return (tranche,) * BATCH_COUNT


def _next_batch_shares(target: Target, position_shares: int, batch_number: int = 1) -> int:
    batches = _batch_sizes(target)
    index = max(0, min(batch_number - 1, len(batches) - 1))
    return max(0, min(batches[index], target.max_shares - position_shares))


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def evaluate(
    target: Target,
    snapshot: Snapshot,
    state: dict[str, Any],
    now_utc: datetime,
    position_shares: int = 0,
    entry_monitoring_enabled: bool | None = None,
) -> tuple[Event, ...]:
    entry_monitoring_enabled = (
        ENTRY_MONITORING_ENABLED if entry_monitoring_enabled is None else entry_monitoring_enabled
    )
    last = snapshot.last
    vwap = snapshot.vwap
    if last is None:
        return ()
    volume_ok = snapshot.volume_ratio is None or snapshot.volume_ratio >= 1.0
    benchmark_ok = (
        target.benchmark is None
        or snapshot.session_return is None
        or snapshot.benchmark_return is None
        or snapshot.session_return >= snapshot.benchmark_return
    )
    in_entry = target.entry_low <= last <= target.entry_high
    entry_candidate = entry_monitoring_enabled and (
        in_entry
        and vwap is not None
        and last >= vwap
        and volume_ok
        and benchmark_ok
        and (snapshot.spread_ratio is None or snapshot.spread_ratio <= target.max_spread_ratio)
    )
    state["entry_ready_polls"] = (
        int(state.get("entry_ready_polls", 0)) + 1 if entry_candidate else 0
    )
    buy_condition = entry_candidate and (state["entry_ready_polls"] >= target.entry_confirm_polls)
    if buy_condition:
        state["buy_seen"] = True
    below_vwap = vwap is not None and last < vwap
    state["below_vwap_polls"] = state.get("below_vwap_polls", 0) + 1 if below_vwap else 0
    now_et = now_utc.astimezone(EASTERN)
    abandon_condition = entry_monitoring_enabled and (
        (state.get("buy_seen") is True and state.get("below_vwap_polls", 0) >= 2)
        or (state.get("buy_seen") is not True and now_et.time() >= target.cutoff_et)
    )
    conditions = {
        "buy_ready": buy_condition,
        "abandon": abandon_condition,
        "stop_loss": target.hard_sl is not None and position_shares > 0 and last <= target.hard_sl,
        "tp1": target.tp1 is not None and position_shares > 0 and last >= target.tp1,
        "tp2": target.tp2_low is not None and position_shares > 0 and last >= target.tp2_low,
    }
    previous = state.setdefault("conditions", {})
    events: list[Event] = []
    for kind, active in conditions.items():
        if active and previous.get(kind) is not True:
            if kind == "buy_ready":
                state["buy_trigger_count"] = int(state.get("buy_trigger_count", 0)) + 1
            events.append(_event(target, kind, snapshot, state, position_shares))
        previous[kind] = active
    return tuple(events)


def _event(
    target: Target,
    kind: str,
    snapshot: Snapshot,
    state: dict[str, Any],
    position_shares: int,
) -> Event:
    last = _fmt(snapshot.last)
    vwap = _fmt(snapshot.vwap)
    if kind == "buy_ready":
        trigger_count = int(state.get("buy_trigger_count", 1))
        batch_number = min(trigger_count, len(_batch_sizes(target)))
        next_shares = _next_batch_shares(target, position_shares, batch_number)
        reference_price = snapshot.last or target.entry_low
        budget = f"单票预算 ${target.budget_usd:,.0f}；" if target.budget_usd > 0 else ""
        return Event(
            "buy_ready",
            f"{target.symbol} 触发买点",
            f"第 {trigger_count} 次触发；价格 {last}，VWAP {vwap}，进入 {target.entry_low:.2f}-{target.entry_high:.2f}，量比 {_fmt(snapshot.volume_ratio)}。"
            f"{budget}当前记录持仓 {position_shares:,} 股；建议第 {batch_number} 批买入 {next_shares:,} 股，"
            f"约 ${next_shares * reference_price:,.0f}。只提示，不下单。",
        )
    if kind == "abandon":
        return Event(
            "abandon",
            f"{target.symbol} 放弃信号",
            f"价格 {last}，VWAP {vwap}；跌破 VWAP 未收回，或已过 {target.cutoff_et.strftime('%H:%M')} ET 买入截止时间。",
        )
    if kind == "stop_loss":
        return Event(
            "stop_loss",
            f"{target.symbol} 触发止损线",
            f"最新价 {last} 已触及 Hard SL {_fmt(target.hard_sl)}。仅推送提醒，不自动卖出。",
        )
    if kind == "tp1":
        return Event(
            "tp1",
            f"{target.symbol} 触达 TP1",
            f"最新价 {last} ≥ TP1 {_fmt(target.tp1)}；如已成交，按预案分批止盈。",
        )
    return Event(
        "tp2",
        f"{target.symbol} 触达 TP2",
        f"最新价 {last} ≥ TP2 区间 {_fmt(target.tp2_low)}-{_fmt(target.tp2_high)}；如已成交，按预案继续止盈。",
    )


def summary(
    target: Target,
    snapshot: Snapshot,
    state: dict[str, Any],
    position_shares: int = 0,
) -> str:
    status = (
        "仅离场监控"
        if not ENTRY_MONITORING_ENABLED
        else ("买点已触发" if state.get("buy_seen") else "等待买点")
    )
    trigger_count = int(state.get("buy_trigger_count", 0))
    next_batch = min(trigger_count + 1, len(_batch_sizes(target)))
    next_shares = _next_batch_shares(target, position_shares, next_batch)
    reference_price = snapshot.last or target.entry_low
    mode_line = (
        f"持仓：记录 {position_shares:,} 股｜买入监控：已关闭｜仅监测止盈/止损"
        if not ENTRY_MONITORING_ENABLED
        else f"预算：${target.budget_usd:,.0f}｜持仓：记录 {position_shares:,} 股｜第 {next_batch} 批建议 {next_shares:,} 股（约 ${next_shares * reference_price:,.0f}）"
    )
    trigger_line = (
        "买入监控：已关闭（历史买点次数不再累计）"
        if not ENTRY_MONITORING_ENABLED
        else f"买点累计触发 {trigger_count} 次"
    )
    return (
        f"【{target.symbol} 15分钟监控总结】\n"
        f"时间：{snapshot.observed_at_utc.isoformat()}\n"
        f"状态：{status}｜{trigger_line}\n"
        f"{mode_line}\n"
        f"价格：最新 {_fmt(snapshot.last)}｜Bid/Ask {_fmt(snapshot.bid)}/{_fmt(snapshot.ask)}\n"
        f"成交量：最近1分钟 {_fmt(snapshot.minute_volume, 0)}｜盘中累计 {_fmt(snapshot.session_volume, 0)}｜量比 {_fmt(snapshot.volume_ratio)}\n"
        f"VWAP：{_fmt(snapshot.vwap)}｜Entry：{target.entry_low:.2f}-{target.entry_high:.2f}\n"
        f"TP：{_fmt(target.tp1)} / {_fmt(target.tp2_low)}-{_fmt(target.tp2_high)}｜Hard SL：{_fmt(target.hard_sl)}\n"
        + (f"风控：{target.risk_note}\n" if target.risk_note else "")
        + "本脚本只读监控，不自动下单。"
    )


def _push_client() -> LivermorePushClient:
    secret = os.getenv("VPS_BUFFETT_APP_SECRET", "").strip()
    if not secret:
        raise RuntimeError("VPS_BUFFETT_APP_SECRET is not configured")
    return LivermorePushClient(
        app_id=os.getenv("VPS_BUFFETT_APP_ID", DEFAULT_APP_ID).strip(),
        app_secret=SecretStr(secret),
        channel_id=os.getenv("VPS_BUFFETT_CHANNEL_ID", DEFAULT_CHANNEL_ID).strip(),
    )


def _deliver(client: LivermorePushClient, body: str) -> str:
    if "?" in body:
        raise ValueError("message contains question marks; refusing to push")
    return client.push(body)


def _event_due(state: dict[str, Any], event: Event, now_utc: datetime) -> bool:
    raw = state.setdefault("last_event_at", {}).get(event.kind)
    if not isinstance(raw, str):
        return True
    try:
        return now_utc - datetime.fromisoformat(raw) >= EVENT_COOLDOWN
    except (TypeError, ValueError):
        return True


def run(
    target: Target,
    *,
    poll_seconds: int = 10,
    summary_seconds: int = 0,
    state_path: Path | None = None,
    lock_path: Path | None = None,
    positions_path: Path | None = None,
    push: bool = True,
    once: bool = False,
) -> None:
    if poll_seconds < 1 or summary_seconds < 0:
        raise ValueError("poll_seconds must be positive and summary_seconds must be non-negative")
    state_path = state_path or ROOT / "runs" / "target-monitors" / f"{target.symbol.lower()}.json"
    lock_path = lock_path or ROOT / "runs" / "target-monitors" / f"{target.symbol.lower()}.lock"
    positions_path = positions_path or DEFAULT_POSITIONS_PATH
    state = _read_state(state_path)
    client = _push_client() if push else None
    feishu = FeishuBaseEventClient.from_environment(os.environ)
    try:
        with ProcessLock(lock_path):
            while True:
                started = time.monotonic()
                now_utc = datetime.now(UTC)
                try:
                    snapshot = fetch_snapshot(target, now_utc)
                    position_shares = _read_positions(positions_path).get(target.symbol, 0)
                    events = evaluate(target, snapshot, state, now_utc, position_shares)
                    for event in events:
                        if _event_due(state, event, now_utc):
                            event_counts = state.setdefault("event_counts", {})
                            occurrence = int(event_counts.get(event.kind, 0)) + 1
                            event_counts[event.kind] = occurrence
                            trade_date = now_utc.astimezone(EASTERN).date()
                            event_id = (
                                f"monitor:{trade_date}:{target.symbol}:{event.kind}:{occurrence}"
                            )
                            message = f"【{event.title}】\n{event.detail}"
                            if feishu is not None:
                                try:
                                    record_monitor_trigger(
                                        feishu,
                                        event_id=event_id,
                                        symbol=target.symbol,
                                        trade_date=trade_date,
                                        triggered_at_utc=now_utc,
                                        trigger_type=event.kind,
                                        operation=event.title,
                                        reason=event.detail,
                                        message=message,
                                        source="scripts.monitor_target",
                                        trigger_price=snapshot.last,
                                        position_shares=position_shares,
                                    )
                                except FeishuBaseError as exc:
                                    print(
                                        json.dumps(
                                            {
                                                "symbol": target.symbol,
                                                "feishu_error": type(exc).__name__,
                                            },
                                            ensure_ascii=False,
                                        ),
                                        flush=True,
                                    )
                            if client is not None:
                                _deliver(client, message)
                            state.setdefault("last_event_at", {})[event.kind] = now_utc.isoformat()
                    if summary_seconds > 0 and client is not None:
                        last_summary = state.get("last_summary_at")
                        summary_due = not isinstance(last_summary, str)
                        if isinstance(last_summary, str):
                            summary_due = now_utc - datetime.fromisoformat(last_summary) >= timedelta(
                                seconds=summary_seconds
                            )
                        if summary_due:
                            _deliver(client, summary(target, snapshot, state, position_shares))
                            state["last_summary_at"] = now_utc.isoformat()
                    state["last_poll_utc"] = now_utc.isoformat()
                    state["last_snapshot"] = {
                        "last": snapshot.last,
                        "bid": snapshot.bid,
                        "ask": snapshot.ask,
                        "minute_volume": snapshot.minute_volume,
                        "session_volume": snapshot.session_volume,
                        "vwap": snapshot.vwap,
                        "volume_ratio": snapshot.volume_ratio,
                        "position_shares": position_shares,
                    }
                    if push or not once:
                        _write_state(state_path, state)
                    print(
                        json.dumps(
                            {
                                "symbol": target.symbol,
                                "last": snapshot.last,
                                "events": [event.kind for event in events],
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                except (DownloadError, RuntimeError, ValueError, OSError) as exc:
                    print(
                        json.dumps(
                            {"symbol": target.symbol, "error": type(exc).__name__},
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                if once:
                    return
                time.sleep(max(0.0, poll_seconds - (time.monotonic() - started)))
    finally:
        if client is not None:
            client.close()


def main(target: Target) -> int:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=f"Read-only {target.symbol} monitor")
    parser.add_argument("--poll-seconds", type=int, default=10)
    parser.add_argument("--summary-seconds", type=int, default=0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--no-push", action="store_true")
    parser.add_argument("--position-path", type=Path, default=DEFAULT_POSITIONS_PATH)
    args = parser.parse_args()
    run(
        target,
        poll_seconds=args.poll_seconds,
        summary_seconds=args.summary_seconds,
        positions_path=args.position_path,
        push=not args.no_push,
        once=args.once,
    )
    return 0
