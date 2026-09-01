from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from execution.alpaca_paper import BrokerOrder
from execution.ledger import OrderLedger
from execution.order_state import OrderLifecycle, OrderState
from execution.reconcile import ReconciliationError, reconcile_broker_order

NOW = datetime(2026, 7, 21, 14, 37, tzinfo=UTC)


def _approved(ledger: OrderLedger) -> OrderLifecycle:
    order = ledger.create(
        OrderLifecycle(
            client_order_id="tsv2-plan-1-entry",
            plan_id="plan-1",
            symbol="AAPL",
            requested_shares=10,
        ),
        created_at_utc=NOW,
    )
    for state in (OrderState.PENDING_RISK, OrderState.APPROVED):
        order = ledger.transition(
            order.client_order_id,
            state,
            at_utc=NOW,
            provenance="test",
        )
    return order


def test_recovery_finds_posted_order_and_advances_to_fill(tmp_path: Path) -> None:
    ledger = OrderLedger(tmp_path / "orders.sqlite3")
    local = _approved(ledger)
    broker = BrokerOrder(
        id="broker-1",
        client_order_id=local.client_order_id,
        symbol="AAPL",
        qty=10,
        filled_qty="10",
        status="filled",
    )

    result = reconcile_broker_order(ledger, local, broker, at_utc=NOW)
    replay = reconcile_broker_order(ledger, result, broker, at_utc=NOW)

    assert result.state is OrderState.FILLED
    assert [event.to_state for event in result.events[-2:]] == [
        OrderState.SUBMITTED,
        OrderState.FILLED,
    ]
    assert replay == result
    assert ledger.get_broker_order_id(local.client_order_id) == "broker-1"


def test_reconciliation_rejects_broker_identity_mismatch(tmp_path: Path) -> None:
    ledger = OrderLedger(tmp_path / "orders.sqlite3")
    local = _approved(ledger)
    broker = BrokerOrder(
        id="broker-1",
        client_order_id=local.client_order_id,
        symbol="MSFT",
        qty=10,
        filled_qty="0",
        status="new",
    )

    with pytest.raises(ReconciliationError, match="identity"):
        reconcile_broker_order(ledger, local, broker, at_utc=NOW)


def test_broker_cancel_can_arrive_without_local_cancel_request(tmp_path: Path) -> None:
    ledger = OrderLedger(tmp_path / "orders.sqlite3")
    local = _approved(ledger)
    local = ledger.transition(
        local.client_order_id,
        OrderState.SUBMITTED,
        at_utc=NOW,
        provenance="test",
    )
    broker = BrokerOrder(
        id="broker-1",
        client_order_id=local.client_order_id,
        symbol="AAPL",
        qty=10,
        filled_qty="0",
        status="canceled",
    )

    result = reconcile_broker_order(ledger, local, broker, at_utc=NOW)
    assert result.state is OrderState.CANCELLED


def test_terminal_local_state_rejects_conflicting_broker_fill(tmp_path: Path) -> None:
    ledger = OrderLedger(tmp_path / "orders.sqlite3")
    local = _approved(ledger)
    local = ledger.transition(
        local.client_order_id,
        OrderState.SUBMITTED,
        at_utc=NOW,
        provenance="test",
    )
    local = ledger.transition(
        local.client_order_id,
        OrderState.CANCELLED,
        at_utc=NOW,
        provenance="test",
    )
    broker = BrokerOrder(
        id="broker-1",
        client_order_id=local.client_order_id,
        symbol="AAPL",
        qty=10,
        filled_qty="10",
        status="filled",
    )

    with pytest.raises(ReconciliationError, match="terminal"):
        reconcile_broker_order(ledger, local, broker, at_utc=NOW)
