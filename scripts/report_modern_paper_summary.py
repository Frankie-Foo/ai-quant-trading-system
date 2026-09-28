"""Publish one factual, idempotent hourly status summary for Alpaca Paper."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
from datetime import UTC, date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import polars as pl
from pydantic import SecretStr

from data_plane.calendar import build_xnys_schedule
from data_plane.providers.alpaca import fetch_bars
from operations.autonomous_notifications import AutonomousNotificationLedger
from operations.livermore_push import LivermorePushClient, configured_identity
from operations.local_env import load_project_env
from operations.paper_state import PaperStateStore

ROOT = Path(__file__).resolve().parents[1]
EASTERN = ZoneInfo("America/New_York")
_SYMBOL_RE = re.compile(r"[A-Z][A-Z0-9.-]{0,15}\Z")
_PHASES = {
    "entry_pending": "等待买入成交",
    "active": "持仓中",
    "exit_pending": "等待卖出成交",
    "stopped": "已止损，观察二次入场",
    "complete": "已完成",
}


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise argparse.ArgumentTypeError("observed time must be UTC")
    return parsed


def summary_slot(observed_at_utc: datetime) -> str | None:
    """Return the current report slot, tolerating a four-minute scheduler delay."""
    if observed_at_utc.tzinfo is None or observed_at_utc.utcoffset() is None:
        raise ValueError("observed time must be timezone-aware")
    eastern = observed_at_utc.astimezone(EASTERN)
    if not time(10) <= eastern.time() < time(15, 5):
        return None
    if eastern.minute >= 5:
        return None
    return f"{eastern.hour:02d}00"


def render_summary(
    *,
    trade_date: date,
    observed_at_utc: datetime,
    symbols: tuple[str, ...],
    states: dict[str, dict[str, object]],
    order_count: int,
    filled_order_count: int,
    pool_status: str = "",
    market_status: str = "",
    observation_symbols: tuple[str, ...] = (),
) -> str:
    eastern = observed_at_utc.astimezone(EASTERN)
    pool = "、".join(symbols) if symbols else "未生成"
    pool_line = pool if not pool_status else f"{pool}（{pool_status}）"
    if observation_symbols:
        pool_label = (
            "正式票池" if pool_status == "使用最终票池"
            else "漏斗阶段票池（非最终、未授权交易）"
        )
        pool_line = (
            f"{pool_label}：{pool_line}\n"
            f"盘中观察池（非正式、未授权交易）：{'、'.join(observation_symbols)}"
        )
    title = (
        "【AI量化运行报警｜正式票池未生成】"
        if not symbols and "使用" not in pool_status
        else "【AI量化｜每小时盘中摘要】"
    )
    active = [
        f"{symbol}：{_PHASES.get(str(state.get('phase')), '状态待核对')}"
        for symbol, state in sorted(states.items())
        if str(state.get("phase")) != "complete"
    ]
    position_text = "；".join(active) if active else "无持仓"
    return "\n".join(
        (
            title,
            f"交易日：{trade_date.isoformat()}｜{eastern:%H:%M} ET",
            f"票池：{pool_line}",
            f"大盘：{market_status or '未获取'}",
            f"持仓状态：{position_text}",
            f"自动漏斗Paper订单：{order_count}｜已成交订单：{filled_order_count}",
            "风控：15:00后禁止新仓，15:50前清仓。",
        )
    )


def _pool_snapshot(state_root: Path, trade_date: date) -> tuple[tuple[str, ...], str]:
    day_root = state_root / trade_date.isoformat()
    for filename, label in (
        ("final_wave_pool.json", "最终票池"),
        ("second_wave_pool.json", "第二波票池"),
        ("first_wave_pool.json", "第一波票池"),
    ):
        path = day_root / filename
        if not path.is_file():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload.get("candidates") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return (), f"{label}文件格式无效"
        symbols = tuple(
            str(row.get("symbol", "")).strip().upper()
            for row in rows
            if isinstance(row, dict) and str(row.get("symbol", "")).strip()
        )
        if len(symbols) != len(set(symbols)):
            return (), f"{label}包含重复代码"
        return symbols, f"使用{label}"
    return (), "第一波票池未生成，后续阶段阻断"


def _observation_pool_snapshot(
    research_root: Path, trade_date: date
) -> tuple[str, ...]:
    """Read a recovery observation pool for reporting only, never as execution input."""
    path = research_root / trade_date.isoformat() / "intraday_recovery_pool.json"
    if not path.is_file():
        return ()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ()
    if (
        not isinstance(payload, dict)
        or payload.get("trade_date_et") != trade_date.isoformat()
        or payload.get("pool_type") != "intraday_recovery_observation_only"
        or payload.get("production_funnel_completed") is not False
        or payload.get("paper_entry_authorized") is not False
        or payload.get("orders_submitted") is not False
    ):
        return ()
    rows = payload.get("symbols")
    if not isinstance(rows, list):
        return ()
    symbols = tuple(
        str(row.get("symbol", "")).strip().upper()
        for row in rows
        if isinstance(row, dict) and str(row.get("symbol", "")).strip()
    )
    if (
        len(symbols) != len(rows)
        or len(symbols) != len(set(symbols))
        or any(_SYMBOL_RE.fullmatch(symbol) is None for symbol in symbols)
    ):
        return ()
    return symbols


def _operator_research_root() -> Path:
    """Resolve operator research alongside the configured shared strategy state."""
    active_policy = os.getenv("AI_QUANT_ACTIVE_POLICY_FILE", "").strip()
    if active_policy:
        return Path(active_policy).resolve().parent.parent / "operator-research"
    return ROOT / "runs" / "operator-research"


def _market_status(trade_date: date, observed_at_utc: datetime) -> str:
    schedule = build_xnys_schedule(trade_date, trade_date)
    if schedule.is_empty():
        return "非交易日"
    market_open = schedule["market_open_utc"][0]
    market_close = schedule["market_close_utc"][0]
    end = min(observed_at_utc, market_close)
    if end <= market_open:
        return "尚未开盘"
    try:
        bars = fetch_bars(("SPY", "QQQ"), market_open, end, feed="sip")
    except Exception as exc:
        return f"Alpaca SIP读取失败（{type(exc).__name__}）"
    if bars.is_empty():
        return "Alpaca SIP无大盘K线"
    parts: list[str] = []
    for symbol in ("SPY", "QQQ"):
        rows = bars.filter(pl.col("symbol") == symbol).sort("ts_utc")
        if rows.is_empty():
            parts.append(f"{symbol}无数据")
            continue
        first = float(rows["close"][0])
        last = float(rows["close"][-1])
        if first <= 0 or last <= 0:
            parts.append(f"{symbol}价格无效")
            continue
        parts.append(f"{symbol}{(last / first - 1):+.2%}")
    return "；".join(parts)


def _funnel_status(funnel_db: Path, trade_date: date, pool_status: str) -> str:
    if "使用" in pool_status:
        return pool_status
    if not funnel_db.is_file():
        return pool_status
    try:
        with sqlite3.connect(funnel_db) as connection:
            row = connection.execute(
                "SELECT stage, status, attempts, updated_at_utc, error "
                "FROM funnel_runs WHERE trade_date=? "
                "ORDER BY updated_at_utc DESC LIMIT 1",
                (trade_date.isoformat(),),
            ).fetchone()
    except sqlite3.Error:
        return pool_status
    if row is None:
        return pool_status
    stage, status, attempts, updated_at_utc, error = row
    stage_name = {
        "first_wave": "第一波",
        "second_wave": "第二波",
        "final_rank": "第三波",
        "open_confirmation": "开盘确认",
    }.get(str(stage), str(stage))
    if status == "failed":
        try:
            failed_at = datetime.fromisoformat(str(updated_at_utc)).astimezone(EASTERN)
            time_text = f"，{failed_at:%H:%M} ET"
        except (TypeError, ValueError):
            time_text = ""
        reason = _safe_failure_reason(str(error or ""))
        return (
            f"{stage_name}阶段失败（尝试{attempts}次{time_text}；"
            f"原因：{reason}；后续阶段阻断）"
        )
    return f"{stage_name}阶段{status}"


def _safe_failure_reason(error: str) -> str:
    child = re.search(
        r"((?:schedule|scripts|data_plane)\.[A-Za-z0-9_.]+ failed with exit code [1-9][0-9]{0,2})",
        error,
    )
    if child:
        return child.group(1)
    return "详细原因未保留"


def _paper_state(
    paper_root: Path,
    trade_date: date,
) -> tuple[dict[str, dict[str, object]], int, int]:
    path = paper_root / trade_date.isoformat() / "paper-state.sqlite3"
    if not path.is_file():
        return {}, 0, 0
    store = PaperStateStore(path)
    orders = tuple(order for order in store.list_orders() if order.trade_date == trade_date)
    return (
        store.load_symbol_states(trade_date),
        len(orders),
        sum(order.status.lower() == "filled" for order in orders),
    )


def _push_once(
    *,
    ledger: AutonomousNotificationLedger,
    trade_date: date,
    slot: str,
    body: str,
) -> tuple[str, str | None]:
    key = f"paper-summary:{trade_date.isoformat()}:{slot}"
    claim = ledger.claim(key, claimed_at_utc=datetime.now(UTC))
    if claim == "sent":
        return "already_sent", None
    if claim == "in_flight":
        raise RuntimeError("paper summary delivery is already in flight")
    app_id, channel_id = configured_identity(os.environ)
    push = LivermorePushClient(
        app_id=app_id,
        app_secret=SecretStr(os.getenv("VPS_LIVERMORE_APP_SECRET", "")),
        channel_id=channel_id,
    )
    try:
        message_id = push.push(body)
    except Exception:
        ledger.release_claim(key)
        raise
    finally:
        push.close()
    ledger.record(
        key,
        message_id=message_id,
        sent_at_utc=datetime.now(UTC),
        message_body=body,
        payload={"trade_date": trade_date.isoformat(), "slot": slot},
    )
    return "delivered", message_id


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", type=date.fromisoformat)
    parser.add_argument("--observed-at-utc", type=_parse_utc)
    parser.add_argument("--state-root", type=Path, default=ROOT / "runs" / "autonomous")
    parser.add_argument("--paper-root", type=Path, default=ROOT / "runs" / "modern-momentum")
    parser.add_argument("--funnel-db", type=Path, default=ROOT / "runs" / "modern-funnel.sqlite3")
    parser.add_argument("--check", action="store_true")
    return parser


def main() -> int:
    load_project_env(ROOT)
    args = _parser().parse_args()
    observed_at = args.observed_at_utc or datetime.now(UTC)
    eastern = observed_at.astimezone(EASTERN)
    trade_date = args.trade_date or eastern.date()
    if trade_date != eastern.date() or build_xnys_schedule(trade_date, trade_date).is_empty():
        print(json.dumps({"status": "not_due"}))
        return 0
    slot = summary_slot(observed_at)
    if slot is None:
        print(json.dumps({"status": "not_due"}))
        return 0
    symbols, pool_status = _pool_snapshot(args.state_root, trade_date)
    pool_status = _funnel_status(args.funnel_db, trade_date, pool_status)
    observation_symbols = _observation_pool_snapshot(
        _operator_research_root(), trade_date
    )
    states, order_count, filled_order_count = _paper_state(args.paper_root, trade_date)
    market_status = _market_status(trade_date, observed_at)
    body = render_summary(
        trade_date=trade_date,
        observed_at_utc=observed_at,
        symbols=symbols,
        states=states,
        order_count=order_count,
        filled_order_count=filled_order_count,
        pool_status=pool_status,
        market_status=market_status,
        observation_symbols=observation_symbols,
    )
    if args.check:
        print(json.dumps({"status": "ready", "slot": slot, "body": body}, ensure_ascii=False))
        return 0
    ledger = AutonomousNotificationLedger(
        args.state_root / trade_date.isoformat() / "funnel-notifications.sqlite3"
    )
    status, message_id = _push_once(
        ledger=ledger,
        trade_date=trade_date,
        slot=slot,
        body=body,
    )
    print(json.dumps({"status": status, "slot": slot, "message_id": message_id}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
