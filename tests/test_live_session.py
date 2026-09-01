from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from data_plane.contracts import DatasetSnapshot
from execution.alpaca_paper import (
    BrokerOrder,
    PaperAccount,
    PaperOrderRequest,
    PaperPosition,
)
from execution.alpaca_sip_stream import SipBar, SipQuote
from execution.engine import PaperExecutionEngine
from execution.ledger import OrderLedger
from execution.live_session import LiveSessionProcessor
from execution.locked_selection import LockedCandidate, LockedSelection
from execution.order_state import OrderState
from execution.sip_store import SipEventStore
from kernel.config import load_config

OPEN = datetime(2026, 7, 21, 13, 30, tzinfo=UTC)


class FakeSessionBroker:
    writes_enabled = False

    def get_account(self) -> PaperAccount:
        return PaperAccount(
            status="ACTIVE",
            account_blocked=False,
            trading_blocked=False,
            equity="100000",
            last_equity="100000",
            buying_power="400000",
        )

    def list_positions(self) -> tuple[PaperPosition, ...]:
        return ()

    def submit_order_idempotent(self, request: PaperOrderRequest) -> BrokerOrder:
        raise AssertionError("shadow session must not submit")

    def get_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        del client_order_id
        return None


def _selection() -> LockedSelection:
    snapshot = DatasetSnapshot(
        dataset_id="selection-1",
        source="kernel.universe.selection_gates",
        asof_utc=OPEN - timedelta(hours=1),
        content_sha256="a" * 64,
        schema_version="selection_gates.v1",
        row_count=1,
    )
    return LockedSelection(
        trade_date=date(2026, 7, 21),
        snapshot=snapshot,
        candidates=(
            LockedCandidate(
                symbol="AAPL",
                selection_rank=1,
                rvol=4.0,
                price=10.0,
                adv_usd=10_000_000.0,
                atr_pct=0.05,
                tier="mid",
            ),
        ),
    )


def _bar(minute: int, values: tuple[float, float, float, float, float]) -> SipBar:
    return SipBar(
        symbol="AAPL",
        ts_utc=OPEN + timedelta(minutes=minute),
        open=values[0],
        high=values[1],
        low=values[2],
        close=values[3],
        vwap=values[4],
        volume=1000,
        trade_count=10,
        provenance="alpaca.sip.websocket@test",
    )


def test_stream_to_signal_to_plan_to_guardrail_shadow_loop(tmp_path: Path) -> None:
    broker = FakeSessionBroker()
    ledger = OrderLedger(tmp_path / "orders.sqlite3")
    processor = LiveSessionProcessor(
        selection=_selection(),
        session_open_utc=OPEN,
        session_close_utc=OPEN + timedelta(hours=6, minutes=30),
        is_half_day=False,
        store=SipEventStore(tmp_path / "sip.sqlite3"),
        engine=PaperExecutionEngine(
            broker=broker,
            ledger=ledger,
            config=load_config("config.yaml"),
            paper_authorized=False,
        ),
        broker=broker,
        config=load_config("config.yaml"),
        kill_switch_active=False,
    )
    values = [
        (10.0, 10.2, 9.9, 10.1, 10.05),
        (10.1, 10.3, 10.0, 10.2, 10.15),
        (10.2, 10.4, 10.1, 10.3, 10.25),
        (10.3, 10.35, 10.2, 10.25, 10.28),
        (10.25, 10.45, 10.2, 10.4, 10.33),
        (10.4, 10.6, 10.35, 10.55, 10.50),
    ]
    for minute, row in enumerate(values):
        result = processor.process(
            _bar(minute, row),
            received_at_utc=OPEN + timedelta(minutes=minute + 1),
        )
        assert result is None
    result = processor.process(
        SipQuote(
            symbol="AAPL",
            ts_utc=OPEN + timedelta(minutes=6, milliseconds=100),
            bid_price=10.54,
            bid_size=10,
            ask_price=10.55,
            ask_size=10,
            provenance="alpaca.sip.websocket@test",
        ),
        received_at_utc=OPEN + timedelta(minutes=6, seconds=1),
    )

    assert result is not None
    assert result.dry_run is True
    assert result.lifecycle.state is OrderState.CANCELLED
    assert result.verdict.approved is True
    assert ledger.get_plan(result.lifecycle.plan_id) is not None
    assert processor.attempted_symbols == frozenset({"AAPL"})


def test_live_session_keeps_scanning_until_a_later_breakout(tmp_path: Path) -> None:
    broker = FakeSessionBroker()
    ledger = OrderLedger(tmp_path / "orders.sqlite3")
    processor = LiveSessionProcessor(
        selection=_selection(),
        session_open_utc=OPEN,
        session_close_utc=OPEN + timedelta(hours=6, minutes=30),
        is_half_day=False,
        store=SipEventStore(tmp_path / "sip.sqlite3"),
        engine=PaperExecutionEngine(
            broker=broker,
            ledger=ledger,
            config=load_config("config.yaml"),
            paper_authorized=False,
        ),
        broker=broker,
        config=load_config("config.yaml"),
        kill_switch_active=False,
    )
    rows = [
        (10.00, 10.20, 9.95, 10.05, 10.04),
        (10.05, 10.25, 10.00, 10.10, 10.08),
        (10.10, 10.30, 10.05, 10.15, 10.13),
        (10.15, 10.35, 10.10, 10.20, 10.18),
        (10.20, 10.40, 10.15, 10.30, 10.25),
    ]
    rows.extend([(10.30, 10.38, 10.20, 10.32, 10.30)] * 7)
    rows.append((10.32, 10.60, 10.30, 10.55, 10.50))

    for minute, row in enumerate(rows):
        assert (
            processor.process(
                _bar(minute, row),
                received_at_utc=OPEN + timedelta(minutes=minute + 1),
            )
            is None
        )

    result = processor.process(
        SipQuote(
            symbol="AAPL",
            ts_utc=OPEN + timedelta(minutes=13, milliseconds=100),
            bid_price=10.54,
            bid_size=10,
            ask_price=10.55,
            ask_size=10,
            provenance="alpaca.sip.websocket@test",
        ),
        received_at_utc=OPEN + timedelta(minutes=13, seconds=1),
    )

    assert result is not None
    assert result.dry_run is True
    assert processor.attempted_symbols == frozenset({"AAPL"})
