"""Typed projections for the four investment Feishu Base tables."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from hashlib import sha256
from typing import Protocol

from execution.autonomous_paper_session import AutonomousPaperPlan, PaperSessionResult
from execution.engine import PaperExecutionResult
from execution.locked_selection import LockedSelection
from operations.feishu_base import InvestmentTable


class InvestmentEventPort(Protocol):
    def record_event(
        self,
        table: InvestmentTable,
        event_id: str,
        fields: Mapping[str, object],
    ) -> str: ...


def record_locked_selection(
    client: InvestmentEventPort,
    selection: LockedSelection,
    *,
    observed_at_utc: datetime,
) -> tuple[str, ...]:
    """Record accepted candidates only; never record polling snapshots."""

    _require_utc(observed_at_utc)
    record_ids: list[str] = []
    snapshot = selection.snapshot
    for candidate in selection.candidates:
        event_id = (
            f"selection:{selection.trade_date.isoformat()}:"
            f"{snapshot.dataset_id}:{candidate.symbol}"
        )
        fields = {
            "选股时间": observed_at_utc,
            "交易日期": selection.trade_date,
            "股票代码": candidate.symbol,
            "选择排名": candidate.selection_rank,
            "RVOL": candidate.rvol,
            "参考价格": candidate.price,
            "日均成交额": candidate.adv_usd,
            "ATR比例": candidate.atr_pct,
            "市值分层": candidate.tier,
            "选择快照ID": snapshot.dataset_id,
            "状态": "进入观察名单",
            "触发原因": (
                f"rank={candidate.selection_rank};"
                f"rvol={candidate.rvol:.4f};"
                "directional_volume_confirmed=true"
            ),
            "来源": (
                f"{snapshot.source}|{snapshot.schema_version}|"
                f"asof={snapshot.asof_utc.isoformat()}|"
                f"sha256={snapshot.content_sha256}"
            ),
            "消息正文": (
                f"【量化系统·选股】{candidate.symbol}\n"
                f"排名：{candidate.selection_rank}\n"
                f"RVOL：{candidate.rvol:.2f}\n"
                f"参考价：{candidate.price:.2f}\n"
                "状态：进入盘中观察名单"
            ),
        }
        record_ids.append(client.record_event(InvestmentTable.SELECTION, event_id, fields))
    return tuple(record_ids)


def record_monitor_trigger(
    client: InvestmentEventPort,
    *,
    event_id: str,
    symbol: str,
    trade_date: date,
    triggered_at_utc: datetime,
    trigger_type: str,
    operation: str,
    reason: str,
    message: str,
    source: str,
    trigger_price: float | None = None,
    position_shares: int | None = None,
    order_ids: Sequence[str] = (),
) -> str:
    """Record one actual monitor transition, never a poll."""

    _require_utc(triggered_at_utc)
    clean_event_id = event_id.strip()
    if not clean_event_id:
        raise ValueError("monitor trigger event ID is required")
    if not symbol.strip() or not trigger_type.strip() or not operation.strip():
        raise ValueError("monitor trigger identity fields are required")
    fields: dict[str, object] = {
        "触发时间": triggered_at_utc,
        "交易日期": trade_date,
        "股票代码": symbol.strip().upper(),
        "触发类型": trigger_type.strip(),
        "操作": operation.strip(),
        "触发原因": reason.strip(),
        "状态": "已触发",
        "来源": source.strip(),
        "消息正文": message,
    }
    if trigger_price is not None:
        fields["触发价格"] = trigger_price
    if position_shares is not None:
        fields["持仓股数"] = position_shares
    clean_orders = tuple(item.strip() for item in order_ids if item.strip())
    if clean_orders:
        fields["订单ID"] = ",".join(clean_orders)
    return client.record_event(InvestmentTable.MONITOR, clean_event_id, fields)


def record_simulated_trade(
    client: InvestmentEventPort,
    *,
    event_id: str,
    trade_date: date,
    observed_at_utc: datetime,
    symbol: str,
    operation: str,
    status: str,
    plan_id: str,
    order_ids: Sequence[str],
    reason: str,
    source: str,
    message: str,
    requested_shares: int | None = None,
    filled_shares: int | None = None,
) -> str:
    """Record a simulated order/state transition, not every market tick."""

    _require_utc(observed_at_utc)
    if not event_id.strip() or not symbol.strip() or not plan_id.strip():
        raise ValueError("simulated trade identity fields are required")
    fields: dict[str, object] = {
        "成交时间": observed_at_utc,
        "交易日期": trade_date,
        "股票代码": symbol.strip().upper(),
        "操作": operation.strip(),
        "状态": status.strip(),
        "计划ID": plan_id.strip(),
        "触发原因": reason.strip(),
        "来源": source.strip(),
        "消息正文": message,
    }
    clean_orders = tuple(item.strip() for item in order_ids if item.strip())
    if clean_orders:
        fields["订单ID"] = ",".join(clean_orders)
    if requested_shares is not None:
        fields["请求股数"] = requested_shares
    if filled_shares is not None:
        fields["成交股数"] = filled_shares
    return client.record_event(InvestmentTable.TRADE, event_id.strip(), fields)


def record_autonomous_trade(
    client: InvestmentEventPort,
    plan: AutonomousPaperPlan,
    result: PaperSessionResult,
    *,
    observed_at_utc: datetime,
    action_name: str,
    message: str,
) -> str:
    """Project an autonomous session result into the simulated-trade table."""

    order_ids = (
        *result.submitted_order_ids,
        *result.cancelled_order_ids,
        *result.flatten_order_ids,
    )
    return record_simulated_trade(
        client,
        event_id=_stable_trade_event_id(plan.plan_id, result),
        trade_date=plan.trade_date,
        observed_at_utc=observed_at_utc,
        symbol=plan.symbol,
        operation=action_name,
        status=result.action.value,
        plan_id=plan.plan_id,
        order_ids=order_ids,
        reason=";".join(result.reasons) or "N/A",
        source=f"{plan.provenance}|{result.provenance}",
        message=message,
    )


def record_paper_execution(
    client: InvestmentEventPort,
    *,
    trade_date: date,
    observed_at_utc: datetime,
    result: PaperExecutionResult,
) -> str:
    """Project the centralized paper-session decision into the trade table."""

    lifecycle = result.lifecycle
    return record_simulated_trade(
        client,
        event_id=f"trade:{trade_date.isoformat()}:{lifecycle.client_order_id}",
        trade_date=trade_date,
        observed_at_utc=observed_at_utc,
        symbol=lifecycle.symbol,
        operation="执行模拟交易" if result.verdict.approved else "模拟交易被拦截",
        status=lifecycle.state.value,
        plan_id=lifecycle.plan_id,
        order_ids=tuple(
            item for item in (result.broker_order_id, lifecycle.client_order_id) if item
        ),
        reason=(
            result.verdict.failure_code.value
            if result.verdict.failure_code is not None
            else "approved"
        ),
        source="scripts.run_paper_session|execution.engine",
        message=(
            f"【量化系统·模拟交易】{lifecycle.symbol}\n"
            f"计划：{lifecycle.plan_id}\n"
            f"状态：{lifecycle.state.value}\n"
            f"结果：{'通过' if result.verdict.approved else '拦截'}"
        ),
        requested_shares=lifecycle.requested_shares,
        filled_shares=lifecycle.filled_shares,
    )


def record_postmarket_review(
    client: InvestmentEventPort,
    *,
    trade_date: date,
    program_review_id: str,
    selection_review_id: str,
    evidence_ids: Sequence[str],
    observed_at_utc: datetime,
) -> str:
    """Record one completed review with the complete evidence chain."""

    _require_utc(observed_at_utc)
    if not program_review_id.strip() or not selection_review_id.strip():
        raise ValueError("postmarket review IDs are required")
    clean_evidence = tuple(item.strip() for item in evidence_ids if item.strip())
    if not clean_evidence:
        raise ValueError("postmarket review evidence IDs are required")
    event_id = (
        f"review:{trade_date.isoformat()}:{program_review_id}:"
        f"{selection_review_id}"
    )
    fields = {
        "复盘时间": observed_at_utc,
        "交易日期": trade_date,
        "复盘类型": "收盘复盘",
        "状态": "已完成",
        "复盘ID": program_review_id,
        "程序复盘ID": program_review_id,
        "选股复盘ID": selection_review_id,
        "关联证据": "|".join(clean_evidence),
        "触发原因": "交易日已结束，数据与证据链完整",
        "来源": "schedule.postmarket|research.postmarket.program_review",
        "消息正文": (
            f"【量化系统·收盘复盘】{trade_date.isoformat()}\n"
            f"程序复盘：{program_review_id}\n"
            f"选股复盘：{selection_review_id}\n"
            "结论：复盘完成，结果进入本地研究账本"
        ),
    }
    return client.record_event(InvestmentTable.REVIEW, event_id, fields)


def _stable_trade_event_id(plan_id: str, result: PaperSessionResult) -> str:
    material = "|".join(
        (
            plan_id,
            result.action.value,
            *result.reasons,
            *result.submitted_order_ids,
            *result.cancelled_order_ids,
            *result.flatten_order_ids,
        )
    )
    # The caller already has a durable notification key; this compact form is
    # only the table-specific namespace prefix.
    return "trade:" + sha256(material.encode()).hexdigest()


def _require_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("Feishu investment event timestamp must be UTC")
