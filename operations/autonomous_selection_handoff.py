"""Freeze a selection, publish its Paper plan, and make it ready for monitoring."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Protocol

from execution.locked_selection import LockedSelection, load_locked_selection
from operations.autonomous_notifications import AutonomousNotificationLedger
from operations.autonomous_paper_config import load_autonomous_paper_config
from operations.autonomous_plan_compiler import compile_autonomous_paper_plans
from operations.feishu_investment_events import record_locked_selection


class PushPort(Protocol):
    def push(self, body: str) -> str: ...


class SelectionAuditPort(Protocol):
    def record_event(self, table: object, event_id: str, fields: dict[str, object]) -> str: ...


@dataclass(frozen=True)
class AutonomousSelectionHandoff:
    config_path: Path
    symbols: tuple[str, ...]
    selection_snapshot_id: str
    message_id: str | None


def prepare_autonomous_selection_handoff(
    *,
    data_root: Path,
    trade_date: date,
    output_path: Path,
    notification_db: Path,
    push: PushPort,
    audit: SelectionAuditPort | None = None,
    observed_at_utc: datetime | None = None,
    max_plans: int = 5,
) -> AutonomousSelectionHandoff:
    """Publish exactly one selection-plan message before autonomous monitoring."""

    observed_at = observed_at_utc or datetime.now(UTC)
    if observed_at.tzinfo is None or observed_at.utcoffset() != UTC.utcoffset(observed_at):
        raise ValueError("observed_at_utc must be UTC")
    prepared = compile_autonomous_paper_plans(
        data_root=data_root,
        trade_date=trade_date,
        output_path=output_path,
        max_plans=max_plans,
    )
    selection = load_locked_selection(data_root, trade_date, min_rvol=3.0)
    config = load_autonomous_paper_config(output_path)
    symbols = tuple(bundle.plan.symbol for bundle in config.plans)
    if audit is not None:
        record_locked_selection(audit, selection, observed_at_utc=observed_at)
    key = f"selection-plan:{trade_date.isoformat()}:{selection.snapshot.dataset_id}"
    ledger = AutonomousNotificationLedger(notification_db)
    claim = ledger.claim(key, claimed_at_utc=observed_at)
    if claim == "sent":
        return AutonomousSelectionHandoff(
            config_path=output_path,
            symbols=symbols,
            selection_snapshot_id=selection.snapshot.dataset_id,
            message_id=None,
        )
    if claim != "claimed":
        raise RuntimeError("selection-plan notification is already in flight")
    message = format_selection_plan_message(
        selection,
        config_path=output_path,
        symbols=symbols,
    )
    try:
        message_id = push.push(message)
    except Exception:
        ledger.release_claim(key)
        raise
    ledger.record(
        key,
        message_id=message_id,
        sent_at_utc=observed_at,
        message_body=message,
        payload={
            "trade_date": trade_date.isoformat(),
            "symbols": list(symbols),
            "selection_snapshot_id": selection.snapshot.dataset_id,
            "config_path": str(output_path),
        },
    )
    if tuple(item.symbol for item in prepared) != symbols:
        raise RuntimeError("selection-plan handoff symbols diverged")
    return AutonomousSelectionHandoff(
        config_path=output_path,
        symbols=symbols,
        selection_snapshot_id=selection.snapshot.dataset_id,
        message_id=message_id,
    )


def format_selection_plan_message(
    selection: LockedSelection,
    *,
    config_path: Path,
    symbols: tuple[str, ...] | None = None,
) -> str:
    """Compact initial notification; later messages are actual Paper actions only."""

    selected = set(symbols or selection.symbols)
    rows = [
        (
            f"{candidate.selection_rank}. {candidate.symbol} | RVOL {candidate.rvol:.2f} | "
            f"盘前 {candidate.premarket_return or 0:+.2%} | "
            f"参考价 ${candidate.premarket_close or candidate.price:.2f}"
        )
        for candidate in selection.candidates
        if candidate.symbol in selected
    ]
    return "\n".join(
        (
            "【AI量化｜今日选股与模拟盘预案】",
            f"交易日：{selection.trade_date.isoformat()}",
            *rows,
            "执行：以上标的进入自动盯盘；仅当实时条件和风控同时通过时提交 Alpaca Paper 订单。",
            "通知：此后仅推送实际模拟买入或卖出；每秒策略评估不推送。",
            f"证据：{selection.snapshot.dataset_id}",
            f"计划：{config_path.name}",
        )
    )
