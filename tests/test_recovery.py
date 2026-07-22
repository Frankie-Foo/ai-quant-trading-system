from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from execution.alpaca_paper import BrokerOrder, PaperPosition
from execution.ledger import OrderLedger
from execution.order_state import OrderLifecycle, OrderState
from execution.recovery import reconcile_startup

NOW = datetime(2026, 7, 21, 14, 0, tzinfo=UTC)


class FakeRecoveryBroker:
    def __init__(
        self,
        orders: dict[str, BrokerOrder],
        positions: tuple[PaperPosition, ...] = (),
    ):
        self.orders = orders
        self.positions = positions

    def get_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        return self.orders.get(client_order_id)

    def list_positions(self) -> tuple[PaperPosition, ...]:
        return self.positions


def _submitted(ledger: OrderLedger) -> OrderLifecycle:
    value = ledger.create(
        OrderLifecycle(
            client_order_id="entry-1",
            plan_id="plan-1",
            symbol="AAPL",
            requested_shares=10,
        ),
        created_at_utc=NOW,
    )
    for state in (OrderState.PENDING_RISK, OrderState.APPROVED, OrderState.SUBMITTED):
        value = ledger.transition(
            value.client_order_id,
            state,
            at_utc=NOW,
            provenance="test",
        )
    return value


def test_startup_recovery_reconciles_fill_and_is_safe(tmp_path: Path) -> None:
    ledger = OrderLedger(tmp_path / "orders.sqlite3")
    local = _submitted(ledger)
    broker = FakeRecoveryBroker(
        {
            local.client_order_id: BrokerOrder(
                id="broker-1",
                client_order_id=local.client_order_id,
                symbol="AAPL",
                qty=10,
                filled_qty="10",
                status="filled",
            )
        }
    )
    report = reconcile_startup(ledger, broker, at_utc=NOW)
    assert report.safe_to_resume is True
    assert report.match_rate == 1.0
    assert ledger.get(local.client_order_id).state is OrderState.FILLED  # type: ignore[union-attr]


def test_startup_recovery_fails_closed_on_missing_broker_order(tmp_path: Path) -> None:
    ledger = OrderLedger(tmp_path / "orders.sqlite3")
    local = _submitted(ledger)
    report = reconcile_startup(
        ledger, FakeRecoveryBroker({}), at_utc=NOW
    )
    assert report.safe_to_resume is False
    assert report.unresolved_orders == (local.client_order_id,)
    assert report.match_rate == 0.0


def test_startup_recovery_flags_unmanaged_position(tmp_path: Path) -> None:
    ledger = OrderLedger(tmp_path / "orders.sqlite3")
    broker = FakeRecoveryBroker(
        {},
        (
            PaperPosition(
                symbol="MSFT", qty="5", side="long", market_value="2500"
            ),
        ),
    )
    report = reconcile_startup(ledger, broker, at_utc=NOW)
    assert report.safe_to_resume is False
    assert report.unmatched_position_symbols == ("MSFT",)
