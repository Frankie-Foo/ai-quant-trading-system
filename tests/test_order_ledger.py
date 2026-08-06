from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from execution.ledger import OrderLedger, OrderLedgerConflictError
from execution.order_state import OrderLifecycle, OrderState

NOW = datetime(2026, 7, 21, 14, 37, tzinfo=UTC)


def _order(*, shares: int = 10) -> OrderLifecycle:
    return OrderLifecycle(
        client_order_id="tsv2-plan-1-entry",
        plan_id="plan-1",
        symbol="AAPL",
        requested_shares=shares,
    )


def test_sqlite_order_ledger_is_idempotent_and_persists_events(tmp_path: Path) -> None:
    ledger = OrderLedger(tmp_path / "orders.sqlite3")
    first = ledger.create(_order(), created_at_utc=NOW)
    second = ledger.create(_order(), created_at_utc=NOW)
    pending = ledger.transition(
        first.client_order_id,
        OrderState.PENDING_RISK,
        at_utc=NOW,
        provenance="kernel.guardrails",
    )

    assert first == second
    assert pending.state is OrderState.PENDING_RISK
    assert ledger.get(first.client_order_id) == pending
    assert pending.events[0].sequence == 1


def test_same_client_id_with_different_intent_is_a_conflict(tmp_path: Path) -> None:
    ledger = OrderLedger(tmp_path / "orders.sqlite3")
    ledger.create(_order(), created_at_utc=NOW)

    with pytest.raises(OrderLedgerConflictError):
        ledger.create(_order(shares=11), created_at_utc=NOW)
