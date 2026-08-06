from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from execution.alpaca_paper import BrokerOrder, PaperPosition
from execution.ledger import OrderLedger
from execution.order_state import OrderLifecycle, OrderState
from execution.recovery import reconcile_startup
from kernel.tradeplan import TradePlan

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


def _plan(*, trade_date: date) -> TradePlan:
    return TradePlan(
        plan_id="plan-1",
        trace_id="trace-1",
        strategy_version="test.v1",
        symbol="AAPL",
        trade_date=trade_date,
        decision_asof_utc=NOW - timedelta(seconds=10),
        created_at_utc=NOW,
        quantity=10,
        reference_price=Decimal("100"),
        take_profit_price=Decimal("105"),
        stop_loss_price=Decimal("98"),
        time_stop_utc=NOW + timedelta(hours=1),
        source_snapshot_ids=("selection-1",),
        provenance="test.plan",
    )


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
    report = reconcile_startup(ledger, broker, at_utc=NOW, trade_date=NOW.date())
    assert report.safe_to_resume is True
    assert report.match_rate == 1.0
    assert ledger.get(local.client_order_id).state is OrderState.FILLED  # type: ignore[union-attr]


def test_startup_recovery_rejects_short_or_quantity_drift(tmp_path: Path) -> None:
    for side, quantity in (("short", "10"), ("long", "5")):
        ledger = OrderLedger(tmp_path / f"orders-{side}-{quantity}.sqlite3")
        ledger.record_plan(_plan(trade_date=NOW.date()))
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
            },
            (
                PaperPosition(
                    symbol="AAPL",
                    qty=quantity,
                    side=side,
                    market_value="1000",
                ),
            ),
        )

        report = reconcile_startup(
            ledger,
            broker,
            at_utc=NOW,
            trade_date=NOW.date(),
        )

        assert report.safe_to_resume is False
        assert report.position_mismatch_symbols == ("AAPL",)


def test_startup_recovery_fails_closed_on_missing_broker_order(tmp_path: Path) -> None:
    ledger = OrderLedger(tmp_path / "orders.sqlite3")
    local = _submitted(ledger)
    report = reconcile_startup(
        ledger, FakeRecoveryBroker({}), at_utc=NOW, trade_date=NOW.date()
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
    report = reconcile_startup(ledger, broker, at_utc=NOW, trade_date=NOW.date())
    assert report.safe_to_resume is False
    assert report.unmatched_position_symbols == ("MSFT",)


def test_startup_recovery_does_not_reuse_a_historical_plan_for_a_position(
    tmp_path: Path,
) -> None:
    ledger = OrderLedger(tmp_path / "orders.sqlite3")
    ledger.record_plan(_plan(trade_date=NOW.date() - timedelta(days=1)))
    local = _submitted(ledger)
    ledger.transition(
        local.client_order_id,
        OrderState.CANCELLED,
        at_utc=NOW,
        provenance="test.cancelled",
    )
    broker = FakeRecoveryBroker(
        {},
        (
            PaperPosition(
                symbol="AAPL", qty="5", side="long", market_value="500"
            ),
        ),
    )

    report = reconcile_startup(ledger, broker, at_utc=NOW, trade_date=NOW.date())

    assert report.safe_to_resume is False
    assert report.unmatched_position_symbols == ("AAPL",)
