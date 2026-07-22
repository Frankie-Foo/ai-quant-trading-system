"""Startup reconciliation between the durable local OMS and Alpaca Paper."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
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
    match_rate: float
    safe_to_resume: bool
    provenance: str


def reconcile_startup(
    ledger: OrderLedger,
    broker: RecoveryBroker,
    *,
    at_utc: datetime,
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
    managed_symbols = {plan.symbol for plan in ledger.list_plans()}
    unmatched_positions = tuple(
        symbol for symbol in position_symbols if symbol not in managed_symbols
    )
    denominator = checked + len(position_symbols)
    matched_total = matched + len(position_symbols) - len(unmatched_positions)
    match_rate = matched_total / denominator if denominator else 1.0
    safe = not unresolved and not unmatched_positions
    return RecoveryReport(
        checked_orders=checked,
        matched_orders=matched,
        unresolved_orders=tuple(sorted(unresolved)),
        position_symbols=position_symbols,
        unmatched_position_symbols=unmatched_positions,
        match_rate=match_rate,
        safe_to_resume=safe,
        provenance="execution.recovery.reconcile_startup.v1",
    )
