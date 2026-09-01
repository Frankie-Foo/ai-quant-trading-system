from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from execution.alpaca_paper import (
    BrokerOrder,
    PaperCloseRequest,
    PaperPosition,
)
from execution.ledger import OrderLedger
from execution.time_exit import TimeExitCoordinator, TimeExitLedger, TimeExitStatus
from kernel.tradeplan import TradePlan

NOW = datetime(2026, 7, 21, 19, 55, tzinfo=UTC)


def _plan() -> TradePlan:
    return TradePlan(
        plan_id="orb5-20260721-AAPL-001",
        trace_id="trace-1",
        strategy_version="orb5.v1",
        symbol="AAPL",
        trade_date=date(2026, 7, 21),
        decision_asof_utc=NOW - timedelta(hours=5),
        created_at_utc=NOW - timedelta(hours=5),
        quantity=10,
        reference_price=Decimal("225"),
        take_profit_price=Decimal("229"),
        stop_loss_price=Decimal("223"),
        time_stop_utc=NOW,
        source_snapshot_ids=("selection-1",),
        provenance="test.plan",
    )


class FakeTimeExitBroker:
    writes_enabled = True

    def __init__(self, *, has_position: bool = True):
        self.has_position = has_position
        self.cancelled: list[str] = []
        self.close_requests: list[PaperCloseRequest] = []
        self.stored: BrokerOrder | None = None

    def list_positions(self) -> tuple[PaperPosition, ...]:
        if not self.has_position:
            return ()
        return (
            PaperPosition(
                symbol="AAPL", qty="10", side="long", market_value="2250"
            ),
        )

    def list_open_orders(self) -> tuple[BrokerOrder, ...]:
        return (
            BrokerOrder(
                id="protective-leg",
                client_order_id="protective-client",
                symbol="AAPL",
                qty=10,
                filled_qty="0",
                status="new",
            ),
        )

    def cancel_order(self, order_id: str) -> bool:
        self.cancelled.append(order_id)
        return True

    def submit_close_order_idempotent(
        self, request: PaperCloseRequest
    ) -> BrokerOrder:
        if self.stored is not None:
            return self.stored
        self.close_requests.append(request)
        self.stored = BrokerOrder(
            id="time-exit-order",
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            qty=request.qty,
            filled_qty="0",
            status="new",
        )
        return self.stored


def _coordinator(tmp_path: Path, broker: FakeTimeExitBroker) -> TimeExitCoordinator:
    order_ledger = OrderLedger(tmp_path / "orders.sqlite3")
    order_ledger.record_plan(_plan())
    return TimeExitCoordinator(
        order_ledger=order_ledger,
        exit_ledger=TimeExitLedger(tmp_path / "exits.sqlite3"),
        broker=broker,
        paper_authorized=True,
    )


def test_time_exit_cancels_bracket_and_submits_one_idempotent_sell(tmp_path: Path) -> None:
    broker = FakeTimeExitBroker()
    coordinator = _coordinator(tmp_path, broker)

    first = coordinator.run_due(now_utc=NOW)
    replay = coordinator.run_due(now_utc=NOW + timedelta(seconds=1))

    assert first[0].status is TimeExitStatus.SUBMITTED
    assert replay[0].status is TimeExitStatus.SUBMITTED
    assert broker.cancelled == ["protective-leg", "protective-leg"]
    assert len(broker.close_requests) == 1
    assert broker.close_requests[0].side == "sell"
    assert broker.close_requests[0].qty == 10


def test_time_exit_cancels_stale_entry_and_completes_without_position(
    tmp_path: Path,
) -> None:
    broker = FakeTimeExitBroker(has_position=False)
    result = _coordinator(tmp_path, broker).run_due(now_utc=NOW)
    assert result[0].status is TimeExitStatus.COMPLETE
    assert result[0].cancelled_order_ids == ("protective-leg",)
    assert broker.close_requests == []


def test_time_exit_is_dry_run_without_readiness_authorization(tmp_path: Path) -> None:
    broker = FakeTimeExitBroker()
    order_ledger = OrderLedger(tmp_path / "orders.sqlite3")
    order_ledger.record_plan(_plan())
    coordinator = TimeExitCoordinator(
        order_ledger=order_ledger,
        exit_ledger=TimeExitLedger(tmp_path / "exits.sqlite3"),
        broker=broker,
        paper_authorized=False,
    )
    result = coordinator.run_due(now_utc=NOW)
    assert result[0].dry_run is True
    assert broker.cancelled == []
    assert broker.close_requests == []


def test_time_exit_schema_is_versioned(tmp_path: Path) -> None:
    ledger = TimeExitLedger(tmp_path / "exits.sqlite3")

    with ledger._connect() as connection:
        row = connection.execute(
            """
            SELECT owner, version, name
            FROM schema_migrations
            WHERE owner = 'execution.time_exit_ledger'
            """
        ).fetchone()

    assert row is not None
    assert tuple(row) == ("execution.time_exit_ledger", 1, "time_exit_actions")
