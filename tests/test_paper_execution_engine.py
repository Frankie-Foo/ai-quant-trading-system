from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from execution.alpaca_paper import BrokerOrder, PaperOrderRequest
from execution.engine import PaperExecutionEngine
from execution.ledger import OrderLedger
from execution.order_state import OrderState
from kernel.config import load_config
from kernel.guardrails import GuardrailContext, RiskCode
from kernel.tradeplan import TradePlan

NOW = datetime(2026, 7, 21, 14, 37, tzinfo=UTC)


class FakeBroker:
    def __init__(self, *, writes_enabled: bool):
        self.writes_enabled = writes_enabled
        self.requests: list[PaperOrderRequest] = []

    def submit_order_idempotent(self, request: PaperOrderRequest) -> BrokerOrder:
        self.requests.append(request)
        return BrokerOrder(
            id="broker-1",
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            qty=request.qty,
            filled_qty="0",
            status="new",
        )


def _plan() -> TradePlan:
    return TradePlan(
        plan_id="orb5-20260721-AAPL-001",
        trace_id="trace-1",
        strategy_version="orb5.v1",
        symbol="AAPL",
        trade_date=date(2026, 7, 21),
        decision_asof_utc=NOW - timedelta(seconds=10),
        created_at_utc=NOW,
        quantity=10,
        reference_price=Decimal("225"),
        take_profit_price=Decimal("229"),
        stop_loss_price=Decimal("223"),
        time_stop_utc=datetime(2026, 7, 21, 19, 55, tzinfo=UTC),
        source_snapshot_ids=("selection-1", "bar-1"),
        provenance="test.plan",
    )


def _context(*, kill_switch: bool = False) -> GuardrailContext:
    return GuardrailContext(
        evaluated_at_utc=NOW,
        market_data_asof_utc=NOW - timedelta(seconds=20),
        market_data_feed="sip",
        paper_endpoint=True,
        kill_switch_active=kill_switch,
        market_open=True,
        account_active=True,
        account_blocked=False,
        trading_blocked=False,
        equity=Decimal("100000"),
        daily_pnl=Decimal("0"),
        gross_exposure=Decimal("0"),
        open_position_symbols=(),
        buying_power=Decimal("100000"),
        sizing_notional_cap=Decimal("2250"),
        selected_symbols=("AAPL",),
        selection_snapshot_ids=("selection-1",),
    )


def test_default_dry_run_records_approved_but_never_submits(tmp_path: Path) -> None:
    broker = FakeBroker(writes_enabled=False)
    engine = PaperExecutionEngine(
        broker=broker,
        ledger=OrderLedger(tmp_path / "orders.sqlite3"),
        config=load_config("config.yaml"),
    )

    result = engine.execute(_plan(), _context())

    assert result.dry_run is True
    assert result.lifecycle.state is OrderState.CANCELLED
    assert result.verdict.approved is True
    assert broker.requests == []


def test_armed_paper_engine_submits_one_bracket_and_persists_broker_id(tmp_path: Path) -> None:
    broker = FakeBroker(writes_enabled=True)
    ledger = OrderLedger(tmp_path / "orders.sqlite3")
    engine = PaperExecutionEngine(
        broker=broker,
        ledger=ledger,
        config=load_config("config.yaml"),
        paper_authorized=True,
    )

    first = engine.execute(_plan(), _context())
    replay = engine.execute(_plan(), _context())

    assert first.lifecycle.state is OrderState.SUBMITTED
    assert replay.replayed is True
    assert len(broker.requests) == 1
    assert broker.requests[0].take_profit_price == "229"
    assert broker.requests[0].stop_loss_price == "223"
    assert ledger.get_broker_order_id(_plan().client_order_id) == "broker-1"
    assert ledger.get_plan(_plan().plan_id) == _plan()


def test_kill_switch_rejects_before_broker_call(tmp_path: Path) -> None:
    broker = FakeBroker(writes_enabled=True)
    engine = PaperExecutionEngine(
        broker=broker,
        ledger=OrderLedger(tmp_path / "orders.sqlite3"),
        config=load_config("config.yaml"),
    )

    result = engine.execute(_plan(), _context(kill_switch=True))

    assert result.lifecycle.state is OrderState.REJECTED
    assert result.verdict.failure_code is RiskCode.P0_KILL_SWITCH
    assert broker.requests == []


def test_write_flag_alone_cannot_bypass_product_readiness(tmp_path: Path) -> None:
    broker = FakeBroker(writes_enabled=True)
    engine = PaperExecutionEngine(
        broker=broker,
        ledger=OrderLedger(tmp_path / "orders.sqlite3"),
        config=load_config("config.yaml"),
    )

    result = engine.execute(_plan(), _context())

    assert result.dry_run is True
    assert result.lifecycle.state is OrderState.CANCELLED
    assert broker.requests == []
