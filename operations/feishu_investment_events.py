"""Typed projections for the four investment Feishu Base tables."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Protocol

from execution.autonomous_paper_session import AutonomousPaperPlan, PaperSessionResult
from execution.engine import PaperExecutionResult
from execution.locked_selection import LockedCandidate, LockedSelection
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
            f"selection:{selection.trade_date.isoformat()}:{snapshot.dataset_id}:{candidate.symbol}"
        )
        fields = {
            "运行ID": event_id,
            "选股时间": observed_at_utc,
            "股票名称": candidate.symbol,
            "股票代码": candidate.symbol,
            "市场": "美股",
            "信号类型": "综合",
            "模拟动作": "候选",
            "状态": "新信号",
            "触发理由": _selection_reason(candidate),
            "下一动作": ("启动盯盘；实时条件与风控门通过后自动提交模拟盘买入；条件失效则放弃"),
            "执行摘要": (
                f"排名={candidate.selection_rank}；"
                f"参考价={candidate.price:.2f}；"
                f"RVOL={candidate.rvol:.2f}；"
                f"日均成交额=${candidate.adv_usd:,.0f}；"
                f"ATR={candidate.atr_pct:.2%}；"
                f"市值={_market_cap_text(candidate.market_cap)}；"
                f"盘前={_premarket_summary(candidate)}"
            ),
            "策略版本": "selection_gates.v2|investment-flywheel.v1",
            "数据源状态": (
                f"通过；{snapshot.dataset_id};"
                f"asof={snapshot.asof_utc.isoformat()};"
                f"sha256={snapshot.content_sha256}"
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
        "运行ID": clean_event_id,
        "触发时间": triggered_at_utc,
        "监控计划ID": f"watch:{trade_date.isoformat()}:{symbol.strip().upper()}",
        "股票代码": symbol.strip().upper(),
        "股票名称": symbol.strip().upper(),
        "触发类型": _monitor_trigger_type(trigger_type),
        "模拟动作": _monitor_action(trigger_type),
        "触发条件": reason.strip(),
        "执行结果": "已执行" if order_ids else "已触发",
        "执行摘要": f"{operation.strip()}；{message}",
        "下一动作": "继续盯盘并等待下一状态转换",
        "数据源状态": source.strip(),
    }
    if trigger_price is not None:
        fields["触发价格"] = trigger_price
    if position_shares is not None:
        fields["模拟数量"] = position_shares
    clean_orders = tuple(item.strip() for item in order_ids if item.strip())
    if clean_orders:
        fields["执行摘要"] = f"{fields['执行摘要']}；订单={','.join(clean_orders)}"
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
    filled_avg_price: str | None = None,
    position_confirmed: bool = False,
    broker_identity: str = "unknown",
) -> str:
    """Record a simulated order/state transition, not every market tick."""

    _require_utc(observed_at_utc)
    if not event_id.strip() or not symbol.strip() or not plan_id.strip():
        raise ValueError("simulated trade identity fields are required")
    if filled_shares is not None and filled_shares < 0:
        raise ValueError("filled shares cannot be negative")
    if filled_shares and not position_confirmed:
        raise ValueError("filled shares require position confirmation")
    if filled_shares and (filled_avg_price is None or not filled_avg_price.strip()):
        raise ValueError("filled shares require average fill price confirmation")
    if filled_avg_price is not None:
        try:
            price = Decimal(filled_avg_price)
        except InvalidOperation as exc:
            raise ValueError("average fill price must be numeric") from exc
        if not price.is_finite() or price <= 0:
            raise ValueError("average fill price must be positive and finite")
    clean_broker = broker_identity.strip()
    if not clean_broker:
        raise ValueError("broker identity is required")
    fields: dict[str, object] = {
        "运行ID": event_id.strip(),
        "成交时间": observed_at_utc,
        "股票代码": symbol.strip().upper(),
        "股票名称": symbol.strip().upper(),
        "方向": "买入",
        "订单状态": _trade_order_status(status),
        "数量": filled_shares or requested_shares or 0,
        "模拟账户": "paper",
        "持仓状态": "持仓中" if filled_shares else "取消",
        "触发来源": f"{operation.strip()}|{plan_id.strip()}",
        "下一动作": "继续盯盘；订单完成后进入复盘" if filled_shares else "等待修复或重试",
        "数据源状态": f"{source.strip()}|broker={clean_broker}",
        "执行摘要": message,
    }
    if filled_avg_price is not None and filled_shares:
        price = Decimal(filled_avg_price)
        fields["成交价格"] = filled_avg_price
        fields["成交金额"] = format(price * filled_shares, "f")
    clean_orders = tuple(item.strip() for item in order_ids if item.strip())
    if clean_orders:
        fields["执行摘要"] = f"{fields['执行摘要']}；订单={','.join(clean_orders)}"
    return client.record_event(InvestmentTable.TRADE, event_id.strip(), fields)


def record_autonomous_trade(
    client: InvestmentEventPort,
    plan: AutonomousPaperPlan,
    result: PaperSessionResult,
    *,
    observed_at_utc: datetime,
    action_name: str,
    message: str,
    broker_identity: str = "unknown",
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
        broker_identity=broker_identity,
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
        filled_avg_price=result.filled_avg_price,
        position_confirmed=result.position_confirmed,
        broker_identity=result.broker_identity,
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
    event_id = f"review:{trade_date.isoformat()}:{program_review_id}:{selection_review_id}"
    fields = {
        "运行ID": event_id,
        "复盘时间": observed_at_utc,
        "关联信号ID": selection_review_id,
        "关联交易ID": "|".join(clean_evidence),
        "复盘结论": "收盘复盘完成，证据链已记录；结果进入本地研究账本",
        "策略改进": "待下一交易日根据成交与触发结果更新",
        "下一动作": "进入下一交易日选股",
        "数据源状态": "通过；schedule.postmarket|research.postmarket.program_review",
        "执行摘要": (
            f"交易日={trade_date.isoformat()}；"
            f"程序复盘={program_review_id}；"
            f"选股复盘={selection_review_id}；"
            f"证据数={len(clean_evidence)}"
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


def _selection_reason(candidate: LockedCandidate) -> str:
    categories = ",".join(candidate.catalyst_categories) or "N/A"
    above_vwap = (
        candidate.premarket_above_vwap if candidate.premarket_above_vwap is not None else "N/A"
    )
    return (
        f"排名={candidate.selection_rank};催化剂={categories};"
        f"事件数={candidate.event_count};RVOL={candidate.rvol:.2f};"
        f"盘前涨幅={_percent_text(candidate.premarket_return)};"
        f"跳空={_percent_text(candidate.premarket_gap_return)};"
        f"盘前高于VWAP={above_vwap};"
        "方向性成交量已确认;"
        f"当前停牌={candidate.current_halt};"
        f"近期LULD={candidate.recent_luld_count};"
        f"LULD风险={candidate.luld_risk}"
    )


def _premarket_summary(candidate: LockedCandidate) -> str:
    if candidate.premarket_close is None or candidate.premarket_vwap is None:
        return "N/A"
    return f"close={candidate.premarket_close:.4f},vwap={candidate.premarket_vwap:.4f}"


def _market_cap_text(value: float | None) -> str:
    return "N/A" if value is None else f"${value / 1_000_000_000:.2f}B"


def _percent_text(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.2%}"


def _monitor_trigger_type(value: str) -> str:
    normalized = value.strip().lower()
    if normalized == "stop":
        return "止损"
    if normalized in {"tp1", "tp2"}:
        return "止盈"
    if normalized == "entry":
        return "突破"
    if normalized == "add":
        return "量价异动"
    return "风控"


def _monitor_action(value: str) -> str:
    normalized = value.strip().lower()
    if normalized == "add":
        return "加仓"
    if normalized in {"stop", "tp1", "tp2"}:
        return "卖出"
    if normalized == "entry":
        return "买入"
    return "不操作"


def _trade_order_status(value: str) -> str:
    normalized = value.strip().lower()
    if normalized == "filled":
        return "模拟成交"
    if normalized in {
        "entry_submitted",
        "reduce_submitted",
        "exit_submitted",
        "stop_exit_submitted",
        "protection_submitted",
        "hard_loss_flatten",
    }:
        return "待执行"
    if normalized in {"rejected", "cancelled"}:
        return "拒绝"
    if normalized in {
        "created",
        "pending_risk",
        "approved",
        "submitted",
        "partially_filled",
    }:
        return "待执行"
    return "失败"
