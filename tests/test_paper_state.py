from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from execution.alpaca_paper import BrokerOrder, PaperPosition
from operations.paper_state import (
    OutboxClaim,
    PaperStateStore,
    UnknownBrokerStateError,
)

TRADE_DATE = date(2026, 8, 24)
NOW = datetime(2026, 8, 24, 14, 0, tzinfo=UTC)


def test_order_intent_and_position_state_survive_restart(tmp_path: Path) -> None:
    path = tmp_path / "paper.sqlite3"
    first = PaperStateStore(path)
    first.record_order_intent(
        trade_date=TRADE_DATE,
        client_order_id="mm-20260824-AAPL-entry-1",
        symbol="AAPL",
        attempt=1,
        role="entry",
        quantity=100,
        payload={"limit_price": "100.05"},
        observed_at_utc=NOW,
    )
    first.attach_broker_order(
        client_order_id="mm-20260824-AAPL-entry-1",
        broker_order_id="broker-1",
        status="new",
        observed_at_utc=NOW,
    )
    first.save_symbol_state(
        trade_date=TRADE_DATE,
        symbol="AAPL",
        state={"phase": "entry_pending", "attempt": 1},
        observed_at_utc=NOW,
    )

    restarted = PaperStateStore(path)
    order = restarted.get_order("mm-20260824-AAPL-entry-1")
    assert order is not None
    assert order.broker_order_id == "broker-1"
    assert restarted.load_symbol_states(TRADE_DATE)["AAPL"]["phase"] == "entry_pending"


def test_order_identity_cannot_be_rebound_after_a_crash(tmp_path: Path) -> None:
    store = PaperStateStore(tmp_path / "paper.sqlite3")
    store.record_order_intent(
        trade_date=TRADE_DATE,
        client_order_id="mm-20260824-AAPL-entry-1",
        symbol="AAPL",
        attempt=1,
        role="entry",
        quantity=100,
        payload={},
        observed_at_utc=NOW,
    )
    store.attach_broker_order(
        client_order_id="mm-20260824-AAPL-entry-1",
        broker_order_id="broker-1",
        status="new",
        observed_at_utc=NOW,
    )

    with pytest.raises(RuntimeError, match="different broker order"):
        store.attach_broker_order(
            client_order_id="mm-20260824-AAPL-entry-1",
            broker_order_id="broker-2",
            status="new",
            observed_at_utc=NOW,
        )


def test_outbox_never_resends_sent_or_ambiguous_delivery(tmp_path: Path) -> None:
    store = PaperStateStore(tmp_path / "paper.sqlite3")
    store.enqueue_outbox(
        event_key="fill:AAPL:entry-1",
        event_type="paper_fill",
        payload={"symbol": "AAPL"},
        observed_at_utc=NOW,
    )

    assert store.claim_outbox("fill:AAPL:entry-1", observed_at_utc=NOW) is OutboxClaim.CLAIMED
    assert (
        store.claim_outbox(
            "fill:AAPL:entry-1",
            observed_at_utc=NOW + timedelta(seconds=1),
        )
        is OutboxClaim.IN_FLIGHT
    )
    store.mark_outbox_sent(
        "fill:AAPL:entry-1",
        message_id="message-1",
        observed_at_utc=NOW + timedelta(seconds=2),
    )
    assert (
        store.claim_outbox(
            "fill:AAPL:entry-1",
            observed_at_utc=NOW + timedelta(seconds=3),
        )
        is OutboxClaim.SENT
    )


def test_process_lease_blocks_a_duplicate_monitor(tmp_path: Path) -> None:
    store = PaperStateStore(tmp_path / "paper.sqlite3")
    assert store.claim_run(TRADE_DATE, owner="process-1", observed_at_utc=NOW)
    assert store.active_run_owner(TRADE_DATE, observed_at_utc=NOW) == "process-1"
    assert not store.claim_run(
        TRADE_DATE,
        owner="process-2",
        observed_at_utc=NOW + timedelta(seconds=1),
    )
    assert store.active_run_owner(
        TRADE_DATE, observed_at_utc=NOW + timedelta(seconds=31)
    ) is None
    assert store.claim_run(
        TRADE_DATE,
        owner="process-2",
        observed_at_utc=NOW + timedelta(seconds=31),
    )


def test_unknown_broker_orders_or_positions_freeze_recovery(tmp_path: Path) -> None:
    store = PaperStateStore(tmp_path / "paper.sqlite3")
    unknown_order = BrokerOrder(
        id="foreign-order",
        client_order_id="mm-20260824-AAPL-entry-1",
        symbol="AAPL",
        qty=10,
        filled_qty="0",
        status="new",
    )
    unknown_position = PaperPosition(
        symbol="MSFT",
        qty="10",
        side="long",
        market_value="1000",
    )

    with pytest.raises(UnknownBrokerStateError):
        store.assert_reconcilable(
            TRADE_DATE,
            open_orders=(unknown_order,),
            positions=(unknown_position,),
        )
