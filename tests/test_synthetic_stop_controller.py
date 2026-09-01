from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from execution.alpaca_paper import (
    BrokerOrder,
    PaperExtendedLimitRequest,
    PaperPosition,
)
from execution.synthetic_stop import (
    StopAction,
    SyntheticStopPlan,
    SyntheticStopSnapshot,
)
from execution.synthetic_stop_controller import (
    SyntheticStopController,
    SyntheticStopExecutionLedger,
)

NOW = datetime(2026, 7, 29, 11, 0, tzinfo=UTC)


class FakeStopBroker:
    writes_enabled = True

    def __init__(self) -> None:
        self.position_qty = 10
        self.orders: dict[str, BrokerOrder] = {}
        self.submissions: list[PaperExtendedLimitRequest] = []
        self.cancelled: list[str] = []

    def list_positions(self) -> tuple[PaperPosition, ...]:
        if self.position_qty == 0:
            return ()
        return (
            PaperPosition(
                symbol="AAPL",
                qty=str(self.position_qty),
                side="long",
                market_value="1000",
            ),
        )

    def get_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        return self.orders.get(client_order_id)

    def cancel_order(self, order_id: str) -> bool:
        self.cancelled.append(order_id)
        return True

    def submit_extended_limit_idempotent(
        self, request: PaperExtendedLimitRequest
    ) -> BrokerOrder:
        stored = self.orders.get(request.client_order_id)
        if stored is not None:
            return stored
        self.submissions.append(request)
        order = BrokerOrder(
            id=f"broker-{len(self.orders) + 1}",
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            qty=request.qty,
            filled_qty="0",
            status="new",
        )
        self.orders[request.client_order_id] = order
        return order


def _plan() -> SyntheticStopPlan:
    return SyntheticStopPlan(
        plan_id="premarket-AAPL-20260729",
        symbol="AAPL",
        qty=10,
        stop_price=Decimal("100.00"),
    )


def _snapshot(seconds: int, *, bid: str = "99.90") -> SyntheticStopSnapshot:
    at = NOW + timedelta(seconds=seconds)
    return SyntheticStopSnapshot(
        observed_at_utc=at,
        quote_asof_utc=at,
        bid=Decimal(bid),
        ask=Decimal("100.00"),
        last_trade=Decimal("99.95"),
        last_trade_asof_utc=at,
        quote_provenance="alpaca.sip.nbbo",
        trade_provenance="alpaca.sip.trade",
        data_healthy=True,
        broker_healthy=True,
        verified_material_negative=False,
        halt_risk=False,
        filled=False,
    )


def _controller(tmp_path: Path, broker: FakeStopBroker) -> SyntheticStopController:
    return SyntheticStopController(
        broker=broker,
        ledger=SyntheticStopExecutionLedger(tmp_path / "synthetic-stops.sqlite3"),
        paper_authorized=True,
    )


def test_trigger_submits_one_idempotent_extended_hours_sell(tmp_path: Path) -> None:
    broker = FakeStopBroker()
    controller = _controller(tmp_path, broker)

    first = controller.tick(_plan(), _snapshot(0))
    second = controller.tick(_plan(), _snapshot(1))

    assert first.action is StopAction.SUBMIT_EXIT_LIMIT
    assert second.action is StopAction.OBSERVE
    assert len(broker.submissions) == 1
    request = broker.submissions[0]
    assert request.side == "sell"
    assert request.extended_hours is True
    assert request.qty == 10
    assert request.limit_price == "99.65"


def test_reprice_cancels_previous_order_and_uses_current_position_qty(
    tmp_path: Path,
) -> None:
    broker = FakeStopBroker()
    controller = _controller(tmp_path, broker)
    controller.tick(_plan(), _snapshot(0))
    broker.position_qty = 4

    result = controller.tick(_plan(), _snapshot(2, bid="99.80"))

    assert result.action is StopAction.CANCEL_REPLACE_EXIT
    assert broker.cancelled == ["broker-1"]
    assert [request.qty for request in broker.submissions] == [10, 4]
    assert broker.submissions[-1].limit_price == "99.30"


def test_restart_replays_same_client_id_without_duplicate_sell(tmp_path: Path) -> None:
    broker = FakeStopBroker()
    ledger_path = tmp_path / "synthetic-stops.sqlite3"
    first = SyntheticStopController(
        broker=broker,
        ledger=SyntheticStopExecutionLedger(ledger_path),
        paper_authorized=True,
    )
    first.tick(_plan(), _snapshot(0))

    restarted = SyntheticStopController(
        broker=broker,
        ledger=SyntheticStopExecutionLedger(ledger_path),
        paper_authorized=True,
    )
    result = restarted.tick(_plan(), _snapshot(1))

    assert result.action is StopAction.OBSERVE
    assert len(broker.submissions) == 1


def test_missing_position_completes_without_submitting_sell(tmp_path: Path) -> None:
    broker = FakeStopBroker()
    broker.position_qty = 0

    result = _controller(tmp_path, broker).tick(_plan(), _snapshot(0))

    assert result.action is StopAction.COMPLETE
    assert broker.submissions == []


def test_controller_is_read_only_until_paper_writes_are_armed(
    tmp_path: Path,
) -> None:
    broker = FakeStopBroker()
    controller = SyntheticStopController(
        broker=broker,
        ledger=SyntheticStopExecutionLedger(tmp_path / "synthetic-stops.sqlite3"),
        paper_authorized=False,
    )

    result = controller.tick(_plan(), _snapshot(0))

    assert result.action is StopAction.ALERT
    assert result.dry_run is True
    assert broker.submissions == []


def test_synthetic_stop_schema_is_versioned(tmp_path: Path) -> None:
    ledger = SyntheticStopExecutionLedger(tmp_path / "synthetic-stops.sqlite3")

    with ledger._connect() as connection:
        row = connection.execute(
            """
            SELECT owner, version, name
            FROM schema_migrations
            WHERE owner = 'execution.synthetic_stop_ledger'
            """
        ).fetchone()

    assert row is not None
    assert tuple(row) == (
        "execution.synthetic_stop_ledger",
        1,
        "synthetic_stop_actions",
    )
