"""Deterministic reconciliation of local OMS state with Alpaca Paper state."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation

from execution.alpaca_paper import BrokerOrder
from execution.ledger import OrderLedger
from execution.order_state import OrderLifecycle, OrderState


class ReconciliationError(RuntimeError):
    """Broker and local identities or quantities cannot be reconciled safely."""


SUBMITTED_STATUSES = {
    "new",
    "accepted",
    "pending_new",
    "accepted_for_bidding",
    "calculated",
    "held",
    "stopped",
    "pending_replace",
}
CANCELLED_STATUSES = {"canceled", "expired", "replaced", "done_for_day"}
REJECTED_STATUSES = {"rejected", "suspended"}


def _filled_shares(order: BrokerOrder) -> int:
    try:
        value = Decimal(order.filled_qty)
    except InvalidOperation as exc:
        raise ReconciliationError("broker filled quantity is invalid") from exc
    if value != value.to_integral_value() or value < 0:
        raise ReconciliationError("broker filled quantity is not a non-negative integer")
    filled = int(value)
    if filled > order.qty:
        raise ReconciliationError("broker filled quantity exceeds requested quantity")
    return filled


def reconcile_broker_order(
    ledger: OrderLedger,
    local: OrderLifecycle,
    broker: BrokerOrder,
    *,
    at_utc: datetime,
) -> OrderLifecycle:
    if (
        local.client_order_id != broker.client_order_id
        or local.symbol != broker.symbol
        or local.requested_shares != broker.qty
    ):
        raise ReconciliationError("broker order identity does not match local intent")
    ledger.record_broker_order_id(local.client_order_id, broker.id)
    current = ledger.get(local.client_order_id)
    if current is None:
        raise ReconciliationError("local order disappeared during reconciliation")
    if current.state in {OrderState.CREATED, OrderState.PENDING_RISK}:
        raise ReconciliationError("broker order exists before local risk approval")
    if current.state is OrderState.APPROVED:
        current = ledger.transition(
            current.client_order_id,
            OrderState.SUBMITTED,
            at_utc=at_utc,
            provenance=f"alpaca.paper.recovery:{broker.id}",
        )

    status = broker.status.strip().lower()
    filled = _filled_shares(broker)
    if status == "filled":
        if filled != current.requested_shares:
            raise ReconciliationError("broker filled status has inconsistent filled quantity")
        target = OrderState.FILLED
    elif status == "partially_filled":
        target = OrderState.PARTIALLY_FILLED
    elif status in CANCELLED_STATUSES:
        target = OrderState.CANCELLED
    elif status in REJECTED_STATUSES:
        target = OrderState.REJECTED
    elif status in SUBMITTED_STATUSES:
        target = OrderState.SUBMITTED
    else:
        raise ReconciliationError(f"unsupported broker order status: {status}")

    if current.state in {OrderState.CANCELLED, OrderState.REJECTED, OrderState.FILLED}:
        if target is current.state and filled == current.filled_shares:
            return current
        raise ReconciliationError("broker status conflicts with local terminal state")
    if target is current.state and filled == current.filled_shares:
        return current
    if target is OrderState.SUBMITTED:
        if current.state is OrderState.SUBMITTED:
            return current
        raise ReconciliationError("broker status regressed to submitted")
    return ledger.transition(
        current.client_order_id,
        target,
        at_utc=at_utc,
        provenance=f"alpaca.paper.status:{status}",
        filled_shares=filled,
    )
