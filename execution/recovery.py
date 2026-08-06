"""Startup reconciliation between the durable local OMS and Alpaca Paper."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Protocol

from execution.alpaca_paper import BrokerOrder, PaperPosition
from execution.ledger import OrderLedger
from execution.order_state import OrderState
from execution.reconcile import ReconciliationError, reconcile_broker_order


class RecoveryBroker(Protocol):
    def get_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None: ...

    def list_positions(self) -> tuple[PaperPosition, ...]: ...


@dataclass(frozen=True)
class RecoveryReport:
    checked_orders: int
    matched_orders: int
    unresolved_orders: tuple[str, ...]
    position_symbols: tuple[str, ...]
    unmatched_position_symbols: tuple[str, ...]
    position_mismatch_symbols: tuple[str, ...]
    match_rate: float
    safe_to_resume: bool
    provenance: str


def reconcile_startup(
    ledger: OrderLedger,
    broker: RecoveryBroker,
    *,
    at_utc: datetime,
    trade_date: date,
) -> RecoveryReport:
    if at_utc.tzinfo is None or at_utc.utcoffset() != timedelta(0):
        raise ValueError("at_utc must be timezone-aware UTC")
    checked = 0
    matched = 0
    unresolved: list[str] = []
    terminal_without_lookup = {
        OrderState.CANCELLED,
        OrderState.REJECTED,
    }
    broker_required = {
        OrderState.SUBMITTED,
        OrderState.PARTIALLY_FILLED,
        OrderState.CANCEL_PENDING,
        OrderState.FILLED,
    }
    for local in ledger.list_orders():
        if local.state in terminal_without_lookup:
            continue
        checked += 1
        broker_order = broker.get_order_by_client_id(local.client_order_id)
        if broker_order is None:
            if local.state in broker_required:
                unresolved.append(local.client_order_id)
            else:
                matched += 1
            continue
        try:
            reconcile_broker_order(ledger, local, broker_order, at_utc=at_utc)
        except ReconciliationError:
            unresolved.append(local.client_order_id)
        else:
            matched += 1

    positions = broker.list_positions()
    position_symbols = tuple(sorted({position.symbol for position in positions}))
    current_plan_ids = {
        plan.plan_id for plan in ledger.list_plans() if plan.trade_date == trade_date
    }
    managed_quantities: dict[str, Decimal] = {}
    for order in ledger.list_orders():
        if order.plan_id not in current_plan_ids or order.filled_shares <= 0:
            continue
        managed_quantities[order.symbol] = managed_quantities.get(
            order.symbol, Decimal("0")
        ) + Decimal(order.filled_shares)
    managed_symbols = set(managed_quantities)
    unmatched_positions = tuple(
        symbol for symbol in position_symbols if symbol not in managed_symbols
    )
    broker_quantities: dict[str, Decimal] = {}
    position_mismatches: set[str] = set()
    for position in positions:
        symbol = position.symbol
        if position.side.strip().lower() != "long":
            position_mismatches.add(symbol)
            continue
        try:
            quantity = Decimal(position.qty)
        except (InvalidOperation, ValueError):
            position_mismatches.add(symbol)
            continue
        if not quantity.is_finite() or quantity <= 0:
            position_mismatches.add(symbol)
            continue
        broker_quantities[symbol] = broker_quantities.get(symbol, Decimal("0")) + quantity
    for symbol in managed_symbols:
        if symbol in broker_quantities and broker_quantities[symbol] != managed_quantities[symbol]:
            position_mismatches.add(symbol)
    position_mismatch_symbols = tuple(sorted(position_mismatches))
    denominator = checked + len(position_symbols)
    matched_total = (
        matched
        + len(position_symbols)
        - len(unmatched_positions)
        - len(position_mismatch_symbols)
    )
    match_rate = matched_total / denominator if denominator else 1.0
    safe = not unresolved and not unmatched_positions and not position_mismatch_symbols
    return RecoveryReport(
        checked_orders=checked,
        matched_orders=matched,
        unresolved_orders=tuple(sorted(unresolved)),
        position_symbols=position_symbols,
        unmatched_position_symbols=unmatched_positions,
        position_mismatch_symbols=position_mismatch_symbols,
        match_rate=match_rate,
        safe_to_resume=safe,
        provenance="execution.recovery.reconcile_startup.v1",
    )
