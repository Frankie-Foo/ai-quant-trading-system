import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from execution.alpaca_paper import BrokerOrder, FreshNbboQuote, PaperPosition
from operations import paper_state
from operations.paper_state import (
    OutboxClaim,
    PaperStateStore,
    UnknownBrokerStateError,
)

TRADE_DATE = date(2026, 8, 24)
NOW = datetime(2026, 8, 24, 14, 0, tzinfo=UTC)


def _source_facts(path: Path) -> tuple[list[tuple[object, ...]], ...]:
    tables = (
        "paper_orders", "paper_symbol_state", "paper_outbox", "paper_run_lease",
        "paper_monitor_cursor", "paper_store_metadata", "paper_entry_quotes",
    )
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        return tuple(db.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall() for table in tables)


def test_entry_quote_audit_survives_restart_and_cannot_change_result(tmp_path: Path) -> None:
    path = tmp_path / "paper.sqlite3"
    quote = FreshNbboQuote(
        symbol="AAPL", bid=Decimal("99.50"), ask=Decimal("100.50"),
        asof_utc=NOW, feed="sip",
    )
    first = PaperStateStore(path)
    first.record_entry_quote(
        trade_date=TRADE_DATE, client_order_id="entry-1", symbol="AAPL", attempt=1,
        signal_ts_utc=NOW, quote=quote, observed_at_utc=NOW,
    )
    first.record_entry_quote(
        trade_date=TRADE_DATE, client_order_id="entry-1", symbol="AAPL", attempt=1,
        signal_ts_utc=NOW, quote=quote, observed_at_utc=NOW,
    )
    restarted = PaperStateStore(path)
    restarted.mark_entry_quote_result(
        client_order_id="entry-1", observed_at_utc=NOW,
        outcome="guard_refused", reason="price drift",
    )
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT symbol,attempt,feed,bid,ask,relative_spread,outcome,reason "
            "FROM paper_entry_quotes WHERE client_order_id=?", ("entry-1",),
        ).fetchall()
    assert rows == [
        ("AAPL", 1, "sip", "99.50", "100.50", "0.01", "guard_refused", "price drift")
    ]
    with pytest.raises(RuntimeError, match="outcome changed"):
        restarted.mark_entry_quote_result(
            client_order_id="entry-1", observed_at_utc=NOW,
            outcome="pre_submit_rejected",
        )


def test_broker_attachment_atomically_marks_entry_quote_submitted(tmp_path: Path) -> None:
    path = tmp_path / "paper.sqlite3"
    store = PaperStateStore(path)
    earlier = NOW - timedelta(seconds=1)
    store.record_entry_quote(
        trade_date=TRADE_DATE, client_order_id="entry-1", symbol="AAPL", attempt=1,
        signal_ts_utc=earlier,
        quote=FreshNbboQuote(
            symbol="AAPL", bid=Decimal("98.50"), ask=Decimal("99.50"),
            asof_utc=earlier, feed="sip",
        ),
        observed_at_utc=earlier,
    )
    store.mark_entry_quote_result(
        client_order_id="entry-1", observed_at_utc=earlier,
        outcome="guard_refused", reason="price drift",
    )
    store.record_entry_quote(
        trade_date=TRADE_DATE, client_order_id="entry-1", symbol="AAPL", attempt=1,
        signal_ts_utc=NOW,
        quote=FreshNbboQuote(
            symbol="AAPL", bid=Decimal("99.50"), ask=Decimal("100.50"),
            asof_utc=NOW, feed="sip",
        ),
        observed_at_utc=NOW,
    )
    store.record_order_intent(
        trade_date=TRADE_DATE, client_order_id="entry-1", symbol="AAPL", attempt=1,
        role="entry", quantity=1, payload={}, observed_at_utc=NOW,
        entry_quote_observed_at_utc=NOW,
    )
    later = NOW + timedelta(seconds=1)
    store.record_entry_quote(
        trade_date=TRADE_DATE, client_order_id="entry-1", symbol="AAPL", attempt=1,
        signal_ts_utc=NOW,
        quote=FreshNbboQuote(
            symbol="AAPL", bid=Decimal("101.50"), ask=Decimal("102.50"),
            asof_utc=later, feed="sip",
        ),
        observed_at_utc=later,
    )
    store.mark_entry_quote_result(
        client_order_id="entry-1", observed_at_utc=later,
        outcome="guard_refused", reason="later process",
    )
    restarted = PaperStateStore(path)
    restarted.attach_broker_order(
        client_order_id="entry-1", broker_order_id="broker-1", status="new",
        observed_at_utc=NOW,
    )
    restarted.record_order_intent(
        trade_date=TRADE_DATE, client_order_id="entry-1", symbol="AAPL", attempt=1,
        role="entry", quantity=1, payload={}, observed_at_utc=NOW,
        entry_quote_observed_at_utc=NOW,
    )
    with sqlite3.connect(path) as connection:
        outcomes = connection.execute(
            "SELECT outcome,order_intent FROM paper_entry_quotes WHERE client_order_id=? "
            "ORDER BY observed_at_utc", ("entry-1",),
        ).fetchall()
    assert outcomes == [
        ("guard_refused", 0), ("submitted", 1), ("guard_refused", 0)
    ]
    order = restarted.get_order("entry-1")
    assert order is not None and order.broker_order_id == "broker-1"


@pytest.mark.parametrize(
    ("trade_date", "symbol", "attempt"),
    [
        (TRADE_DATE + timedelta(days=1), "AAPL", 1),
        (TRADE_DATE, "MSFT", 1),
        (TRADE_DATE, "AAPL", 2),
    ],
)
def test_entry_intent_refuses_quote_with_different_identity(
    tmp_path: Path, trade_date: date, symbol: str, attempt: int,
) -> None:
    store = PaperStateStore(tmp_path / "paper.sqlite3")
    store.record_entry_quote(
        trade_date=TRADE_DATE, client_order_id="entry-1", symbol="AAPL", attempt=1,
        signal_ts_utc=NOW,
        quote=FreshNbboQuote(
            symbol="AAPL", bid=Decimal("99.50"), ask=Decimal("100.50"),
            asof_utc=NOW, feed="sip",
        ),
        observed_at_utc=NOW,
    )
    with pytest.raises(RuntimeError, match="quote identity"):
        store.record_order_intent(
            trade_date=trade_date, client_order_id="entry-1", symbol=symbol,
            attempt=attempt, role="entry", quantity=1, payload={}, observed_at_utc=NOW,
            entry_quote_observed_at_utc=NOW,
        )
    assert store.get_order("entry-1") is None


def test_rejected_entry_quote_and_intent_abort_commit_together(tmp_path: Path) -> None:
    path = tmp_path / "paper.sqlite3"
    store = PaperStateStore(path)
    store.record_entry_quote(
        trade_date=TRADE_DATE, client_order_id="entry-1", symbol="AAPL", attempt=1,
        signal_ts_utc=NOW,
        quote=FreshNbboQuote(
            symbol="AAPL", bid=Decimal("99.50"), ask=Decimal("100.50"),
            asof_utc=NOW, feed="sip",
        ),
        observed_at_utc=NOW,
    )
    store.record_order_intent(
        trade_date=TRADE_DATE, client_order_id="entry-1", symbol="AAPL", attempt=1,
        role="entry", quantity=1, payload={}, observed_at_utc=NOW,
        entry_quote_observed_at_utc=NOW,
    )
    with pytest.raises(RuntimeError, match="atomic order resolution"):
        store.mark_entry_quote_result(
            client_order_id="entry-1", observed_at_utc=NOW,
            outcome="pre_submit_rejected", reason="quote changed",
        )
    pending: dict[str, object] = {"phase": "entry_pending", "entry_client_id": "entry-1"}
    store.save_symbol_state(
        trade_date=TRADE_DATE, symbol="AAPL", state=pending, observed_at_utc=NOW,
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TRIGGER fail_quote_abort BEFORE UPDATE OF outcome ON paper_entry_quotes "
            "BEGIN SELECT RAISE(ABORT, 'injected quote write failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected quote write failure"):
        store.abort_unsubmitted_entry(
            client_order_id="entry-1", prior_state=None, observed_at_utc=NOW,
            entry_quote_observed_at_utc=NOW, rejection_reason="quote changed",
        )
    order = store.get_order("entry-1")
    assert order is not None and order.status == "intent"
    assert store.load_symbol_states(TRADE_DATE)["AAPL"] == pending
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT outcome FROM paper_entry_quotes WHERE client_order_id='entry-1'"
        ).fetchone() == ("observed",)
        connection.execute("DROP TRIGGER fail_quote_abort")
    store.abort_unsubmitted_entry(
        client_order_id="entry-1", prior_state=None, observed_at_utc=NOW,
        entry_quote_observed_at_utc=NOW, rejection_reason="quote changed",
    )
    order = store.get_order("entry-1")
    assert order is not None and order.status == "aborted"
    assert store.load_symbol_states(TRADE_DATE) == {}
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT outcome,reason FROM paper_entry_quotes WHERE client_order_id='entry-1'"
        ).fetchone() == ("pre_submit_rejected", "quote changed")


@pytest.mark.parametrize("bound", [False, True])
def test_abort_unsubmitted_entry_restores_prior_attempt_without_erasing_audit(
    tmp_path: Path, bound: bool,
) -> None:
    store = PaperStateStore(tmp_path / "paper.sqlite3")
    prior: dict[str, object] = {
        "phase": "stopped", "attempt": 1, "reentry_after_utc": NOW,
        "fill_observations": {"old-exit": {"filled_qty": "10"}},
    }
    pending: dict[str, object] = {
        "phase": "entry_pending", "attempt": 2, "entry_client_id": "second",
    }
    store.record_order_intent(
        trade_date=TRADE_DATE, client_order_id="second", symbol="AAPL", attempt=2,
        role="entry", quantity=10, payload={}, observed_at_utc=NOW,
    )
    store.save_symbol_state(
        trade_date=TRADE_DATE, symbol="AAPL", state=pending, observed_at_utc=NOW,
    )
    if bound:
        store.attach_broker_order(
            client_order_id="second", broker_order_id="broker-second", status="new",
            observed_at_utc=NOW,
        )
        with pytest.raises(RuntimeError, match="unbound"):
            store.abort_unsubmitted_entry(
                client_order_id="second", prior_state=prior, observed_at_utc=NOW,
            )
        assert store.load_symbol_states(TRADE_DATE)["AAPL"] == pending
    else:
        store.abort_unsubmitted_entry(
            client_order_id="second", prior_state=prior, observed_at_utc=NOW,
        )
        assert store.load_symbol_states(TRADE_DATE)["AAPL"] == prior
    order = store.get_order("second")
    assert order is not None
    assert order.status == ("new" if bound else "aborted")


def test_local_aborted_entry_alone_does_not_require_prior_day_recovery(tmp_path: Path) -> None:
    store = PaperStateStore(tmp_path / str(TRADE_DATE) / "paper-state.sqlite3")
    store.record_order_intent(
        trade_date=TRADE_DATE, client_order_id="never-posted", symbol="AAPL", attempt=1,
        role="entry", quantity=1, payload={}, observed_at_utc=NOW,
    )
    store.save_symbol_state(
        trade_date=TRADE_DATE, symbol="AAPL",
        state={"phase": "entry_pending", "entry_client_id": "never-posted"},
        observed_at_utc=NOW,
    )
    store.abort_unsubmitted_entry(
        client_order_id="never-posted", prior_state=None, observed_at_utc=NOW,
    )
    tomorrow = TRADE_DATE + timedelta(days=1)
    assert paper_state.discover_prior_day_stores(tmp_path, trade_date=tomorrow) == ()
    assert paper_state.read_prior_day_states(tmp_path, trade_date=tomorrow) == {}
    assert store.list_orders()[0].status == "aborted"


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


def test_prior_day_discovery_is_readonly_and_never_creates_a_missing_database(
    tmp_path: Path,
) -> None:
    prior = PaperStateStore(tmp_path / "2026-08-24" / "paper-state.sqlite3")
    prior.save_symbol_state(
        trade_date=TRADE_DATE, symbol="AAPL",
        state={"phase": "entry_pending", "entry_client_id": "old-entry", "attempt": 1},
        observed_at_utc=NOW,
    )
    original = _source_facts(prior.path)
    future = PaperStateStore(tmp_path / "2026-08-26" / "paper-state.sqlite3")
    future.save_symbol_state(
        trade_date=date(2026, 8, 26), symbol="MSFT", state={"phase": "active"},
        observed_at_utc=NOW + timedelta(days=2),
    )
    assert paper_state.discover_prior_day_stores(
        tmp_path, trade_date=date(2026, 8, 25),
    ) == (prior.path.resolve(),)
    assert _source_facts(prior.path) == original
    missing = tmp_path / "missing"
    assert paper_state.discover_prior_day_stores(missing, trade_date=TRADE_DATE) == ()
    assert not missing.exists()


def _prior_store(tmp_path: Path) -> PaperStateStore:
    prior = PaperStateStore(tmp_path / "2026-08-24" / "paper-state.sqlite3")
    prior.record_order_intent(
        trade_date=TRADE_DATE, client_order_id="old-entry", symbol="AAPL", attempt=1,
        role="entry", quantity=2, payload={"original": "request"}, observed_at_utc=NOW,
    )
    prior.save_symbol_state(
        trade_date=TRADE_DATE, symbol="AAPL", state={
            "phase": "entry_pending", "attempt": 1, "entry_client_id": "old-entry",
            "entry_request": {"client_order_id": "old-entry"},
            "exit_retry": 2, "extra_lineage": {"fees": None}, "entered_at_utc": NOW,
        }, observed_at_utc=NOW,
    )
    for key in ("sent-fill", "ambiguous-fill"):
        prior.enqueue_outbox(
            event_key=key, event_type="paper_fill", payload={"symbol": "AAPL"},
            observed_at_utc=NOW,
        )
        prior.claim_outbox(key, observed_at_utc=NOW)
    prior.mark_outbox_sent("sent-fill", message_id="msg-1", observed_at_utc=NOW)
    return prior


@pytest.mark.parametrize("bound", [False, True])
def test_mixed_recovery_imports_only_unbound_local_aborted_entry_without_state(
    tmp_path: Path, bound: bool,
) -> None:
    prior = _prior_store(tmp_path)
    prior.save_symbol_state(
        trade_date=TRADE_DATE, symbol="AAPL",
        state={"phase": "active", "attempt": 1, "entry_client_id": "old-entry"},
        observed_at_utc=NOW,
    )
    prior.record_order_intent(
        trade_date=TRADE_DATE, client_order_id="never-posted", symbol="MSFT", attempt=1,
        role="entry", quantity=1, payload={}, observed_at_utc=NOW,
    )
    prior.save_symbol_state(
        trade_date=TRADE_DATE, symbol="MSFT",
        state={"phase": "entry_pending", "entry_client_id": "never-posted"},
        observed_at_utc=NOW,
    )
    prior.abort_unsubmitted_entry(
        client_order_id="never-posted", prior_state=None, observed_at_utc=NOW,
    )
    if bound:
        # Corrupt historical evidence must not receive the local-terminal exemption.
        prior.attach_broker_order(
            client_order_id="never-posted", broker_order_id="unexpected", status="aborted",
            observed_at_utc=NOW,
        )
    tomorrow = TRADE_DATE + timedelta(days=1)
    history = paper_state.read_prior_day_states(tmp_path, trade_date=tomorrow)
    assert set(history[TRADE_DATE].states) == {"AAPL"}
    before_states = prior.load_symbol_states(TRADE_DATE)
    before_orders = prior.list_orders()
    target = PaperStateStore(tmp_path / "recovery.sqlite3")
    if bound:
        with pytest.raises(RuntimeError, match="no recoverable symbol state"):
            target.import_exit_recovery(
                prior.path, source_trade_date=TRADE_DATE, trade_date=tomorrow,
                observed_at_utc=NOW + timedelta(days=1),
            )
        assert target.load_symbol_states(tomorrow) == {}
        assert target.list_orders() == ()
    else:
        imported = target.import_exit_recovery(
            prior.path, source_trade_date=TRADE_DATE, trade_date=tomorrow,
            observed_at_utc=NOW + timedelta(days=1),
        )
        assert set(imported) == {"AAPL"}
        assert imported["AAPL"]["phase"] == "active"
        assert imported["AAPL"]["recovery_only"] is True
        assert target.get_order("never-posted") == prior.get_order("never-posted")
        assert "aborted" not in paper_state.TERMINAL_ORDER_STATUSES
    assert prior.load_symbol_states(TRADE_DATE) == before_states
    assert prior.list_orders() == before_orders


def test_explicit_exit_recovery_preserves_lineage_orders_outbox_and_progress(
    tmp_path: Path,
) -> None:
    prior = _prior_store(tmp_path)
    original_state = prior.load_symbol_states(TRADE_DATE)["AAPL"]
    original_facts = _source_facts(prior.path)
    target_date = date(2026, 8, 25)
    now = NOW + timedelta(days=1)
    target = PaperStateStore(tmp_path / target_date.isoformat() / "paper-state.sqlite3")
    imported = target.import_exit_recovery(
        prior.path, source_trade_date=TRADE_DATE, trade_date=target_date, observed_at_utc=now,
    )
    assert imported["AAPL"]["recovery_only"] is True
    assert imported["AAPL"]["recovery_source_trade_date"] == "2026-08-24"
    assert all(imported["AAPL"][key] == value for key, value in original_state.items())
    order = target.get_order("old-entry")
    assert order is not None and order.trade_date == TRADE_DATE and order.status == "intent"
    assert target.claim_outbox("sent-fill", observed_at_utc=now) is OutboxClaim.SENT
    assert target.claim_outbox("ambiguous-fill", observed_at_utc=now) is OutboxClaim.IN_FLIGHT
    progressed = {**imported["AAPL"], "phase": "complete", "exit_retry": 3}
    target.save_symbol_state(
        trade_date=target_date, symbol="AAPL", state=progressed, observed_at_utc=now,
    )
    replay = target.import_exit_recovery(
        prior.path, source_trade_date=TRADE_DATE, trade_date=target_date, observed_at_utc=now,
    )
    assert replay["AAPL"] == progressed
    assert _source_facts(prior.path) == original_facts
    assert prior.load_symbol_states(TRADE_DATE)["AAPL"] == original_state


def test_read_prior_day_states_exposes_original_order_proof_without_writing(tmp_path: Path) -> None:
    prior = _prior_store(tmp_path)
    original = _source_facts(prior.path)
    history = paper_state.read_prior_day_states(tmp_path, trade_date=date(2026, 8, 25))
    snapshot = history[TRADE_DATE]
    assert snapshot.trade_date == TRADE_DATE
    assert snapshot.path == prior.path.resolve()
    assert snapshot.states["AAPL"]["entry_client_id"] == "old-entry"
    assert snapshot.orders[0] == prior.get_order("old-entry")
    assert _source_facts(prior.path) == original


@pytest.mark.parametrize("blocker", ["active-lease", "state-conflict", "order-conflict"])
def test_exit_recovery_refuses_unsafe_import_without_partial_state(
    tmp_path: Path, blocker: str,
) -> None:
    prior = _prior_store(tmp_path)
    now = NOW + timedelta(days=1)
    target_date = date(2026, 8, 25)
    target = PaperStateStore(tmp_path / "recovery" / "paper-state.sqlite3")
    if blocker == "active-lease":
        prior.claim_run(TRADE_DATE, owner="still-running", observed_at_utc=now)
    elif blocker == "state-conflict":
        target.save_symbol_state(
            trade_date=target_date, symbol="AAPL", state={"phase": "active", "quantity": 99},
            observed_at_utc=now,
        )
    else:
        target.record_order_intent(
            trade_date=target_date, client_order_id="old-entry", symbol="AAPL", attempt=1,
            role="entry", quantity=99, payload={}, observed_at_utc=now,
        )
    before = target.load_symbol_states(target_date)
    with pytest.raises(RuntimeError, match="active|conflicts"):
        target.import_exit_recovery(
            prior.path, source_trade_date=TRADE_DATE, trade_date=target_date, observed_at_utc=now,
        )
    assert target.load_symbol_states(target_date) == before
    with pytest.raises(KeyError):
        target.claim_outbox("sent-fill", observed_at_utc=now)


def test_stopped_state_is_imported_as_evidence_not_assumed_flat(tmp_path: Path) -> None:
    prior = _prior_store(tmp_path)
    stopped: dict[str, object] = {
        "phase": "stopped", "entry_client_id": "stopped-entry", "stop_client_id": "stopped-stop",
        "attempt": 1, "reentry_after_utc": NOW, "audit": {"stop_reason": "original"},
    }
    prior.save_symbol_state(
        trade_date=TRADE_DATE, symbol="MSFT", state=stopped, observed_at_utc=NOW,
    )
    original = _source_facts(prior.path)
    target = PaperStateStore(tmp_path / "recovery.sqlite3")
    imported = target.import_exit_recovery(
        prior.path, source_trade_date=TRADE_DATE, trade_date=date(2026, 8, 25),
        observed_at_utc=NOW + timedelta(days=1),
    )
    assert set(imported) == {"AAPL", "MSFT"}
    assert all(imported["MSFT"][key] == value for key, value in stopped.items())
    assert imported["MSFT"]["recovery_only"] is True
    assert _source_facts(prior.path) == original


def _parent_proof_store(tmp_path: Path) -> tuple[PaperStateStore, BrokerOrder, BrokerOrder]:
    store = PaperStateStore(tmp_path / "parent-proof.sqlite3")
    store.record_order_intent(
        trade_date=TRADE_DATE, client_order_id="entry", symbol="AAPL", attempt=1,
        role="entry", quantity=4, payload={"side": "buy"}, observed_at_utc=NOW,
    )
    store.save_symbol_state(
        trade_date=TRADE_DATE, symbol="AAPL",
        state={"phase": "entry_pending", "entry_client_id": "entry", "attempt": 1},
        observed_at_utc=NOW,
    )
    child = BrokerOrder(
        id="stop-id", client_order_id="stop", symbol="AAPL", side="sell", qty=4,
        filled_qty="0", status="new", type="stop",
    )
    parent = BrokerOrder(
        id="entry-id", client_order_id="entry", symbol="AAPL", side="buy", qty=4,
        filled_qty="4", status="filled", type="limit", legs=(child,),
    )
    return store, parent, child


@pytest.mark.parametrize("bound", [False, True])
def test_verified_parent_proves_a_separately_returned_unsaved_child(
    tmp_path: Path, bound: bool,
) -> None:
    store, parent, child = _parent_proof_store(tmp_path)
    if bound:
        store.attach_broker_order(
            client_order_id="entry", broker_order_id=parent.id, status=parent.status,
            observed_at_utc=NOW,
        )
    states_before = store.load_symbol_states(TRADE_DATE)
    orders_before = store.list_orders()
    with pytest.raises(UnknownBrokerStateError):
        store.assert_reconcilable(TRADE_DATE, open_orders=(child,), positions=())
    store.assert_reconcilable(
        TRADE_DATE, open_orders=(child,), positions=(), parent_orders=(parent,),
    )
    assert store.load_symbol_states(TRADE_DATE) == states_before
    assert store.list_orders() == orders_before


@pytest.mark.parametrize("field,value", [
    ("client_order_id", "foreign"), ("symbol", "MSFT"), ("side", "sell"),
    ("qty", 8), ("id", "foreign-id"),
])
def test_parent_proof_rejects_forged_entry_identity(
    tmp_path: Path, field: str, value: object,
) -> None:
    store, parent, child = _parent_proof_store(tmp_path)
    store.attach_broker_order(
        client_order_id="entry", broker_order_id=parent.id, status=parent.status,
        observed_at_utc=NOW,
    )
    with pytest.raises(UnknownBrokerStateError):
        store.assert_reconcilable(
            TRADE_DATE, open_orders=(child,), positions=(),
            parent_orders=(parent.model_copy(update={field: value}),),
        )


@pytest.mark.parametrize("field,value", [
    ("client_order_id", "foreign"), ("symbol", "MSFT"), ("side", "buy"),
    ("qty", 8), ("id", "foreign-id"), ("order_type", "market"),
])
def test_parent_proof_rejects_forged_standalone_child_identity(
    tmp_path: Path, field: str, value: object,
) -> None:
    store, parent, child = _parent_proof_store(tmp_path)
    with pytest.raises(UnknownBrokerStateError):
        store.assert_reconcilable(
            TRADE_DATE, open_orders=(child.model_copy(update={field: value}),), positions=(),
            parent_orders=(parent,),
        )


@pytest.mark.parametrize("field,value", [
    ("symbol", "MSFT"), ("side", "buy"), ("qty", 8), ("id", "entry-id"),
])
def test_parent_proof_rejects_malformed_leg_even_when_open_snapshot_agrees(
    tmp_path: Path, field: str, value: object,
) -> None:
    store, parent, child = _parent_proof_store(tmp_path)
    malformed = child.model_copy(update={field: value})
    with pytest.raises(UnknownBrokerStateError):
        store.assert_reconcilable(
            TRADE_DATE, open_orders=(malformed,), positions=(),
            parent_orders=(parent.model_copy(update={"legs": (malformed,)}),),
        )


def test_parent_child_proof_cannot_override_a_persisted_broker_binding(tmp_path: Path) -> None:
    store, parent, child = _parent_proof_store(tmp_path)
    store.record_order_intent(
        trade_date=TRADE_DATE, client_order_id=child.client_order_id, symbol="AAPL", attempt=1,
        role="exit", quantity=4, payload={"side": "sell"}, observed_at_utc=NOW,
    )
    store.attach_broker_order(
        client_order_id=child.client_order_id, broker_order_id="already-bound-child",
        status="new", observed_at_utc=NOW,
    )
    with pytest.raises(UnknownBrokerStateError):
        store.assert_reconcilable(
            TRADE_DATE, open_orders=(child,), positions=(), parent_orders=(parent,),
        )
