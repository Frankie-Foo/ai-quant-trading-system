"""Exit-only recovery regressions using temporary stores and synthetic HTTP only."""

import socket
import subprocess
from datetime import date, timedelta
from pathlib import Path
from typing import Any, NoReturn

import httpx
import pytest
from test_modern_paper_lifecycle import DAY, NOW, PaperExchange

from operations.paper_state import PaperStateStore, UnknownBrokerStateError
from scripts import monitor_modern_momentum_paper as paper


@pytest.fixture(autouse=True)
def isolated_io(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("prior-day regressions forbid network, credentials and subprocesses")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(paper, "load_project_env", forbidden)


class RecoveryExchange(PaperExchange):
    def __init__(self) -> None:
        super().__init__()
        self.holdings: dict[str, int | float] = {"TEST": 4}

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/positions":
            return httpx.Response(200, json=[
                {"symbol": symbol, "qty": str(qty), "side": "long", "market_value": str(qty * 20)}
                for symbol, qty in self.holdings.items() if qty
            ])
        return super().handle(request)


def _order(
    client_id: str, *, symbol: str = "TEST", side: str = "buy", qty: int = 4,
    filled: int = 4, status: str = "filled", kind: str = "limit",
) -> dict[str, Any]:
    return dict(
        id=f"{client_id}-id", client_order_id=client_id, symbol=symbol, side=side,
        qty=str(qty), filled_qty=str(filled), filled_avg_price="20" if filled else None,
        status=status, type=kind,
    )


def _source(
    root: Path, day: date, *, symbol: str = "TEST", phase: str = "active",
) -> PaperStateStore:
    store = PaperStateStore(root / day.isoformat() / "paper-state.sqlite3")
    store.save_symbol_state(
        trade_date=day, symbol=symbol,
        state={"phase": phase, "attempt": 1, "shares": 4, "entry_client_id": f"{symbol}-entry"},
        observed_at_utc=NOW - timedelta(days=1),
    )
    return store


def _tick(exchange: RecoveryExchange, recovery: PaperStateStore, root: Path, day: date) -> bool:
    broker = exchange.broker()
    try:
        return paper.recover_prior_day_tick(
            broker, recovery, history_root=root, source_trade_date=day,
            trade_date=DAY, observed_at_utc=NOW,
        )
    finally:
        broker.close()


def test_switching_source_cannot_bypass_an_unproven_previously_imported_position(
    tmp_path: Path,
) -> None:
    root = tmp_path / "history"
    first_day, second_day = DAY - timedelta(days=2), DAY - timedelta(days=1)
    _source(root, first_day)
    _source(root, second_day, symbol="OTHER")
    exchange = RecoveryExchange()
    exchange.holdings["OTHER"] = 4
    exchange.orders["OTHER-entry"] = _order("OTHER-entry", symbol="OTHER")
    recovery = PaperStateStore(tmp_path / "recovery.sqlite3")

    with pytest.raises(RuntimeError):
        _tick(exchange, recovery, root, first_day)
    assert "TEST" in recovery.load_symbol_states(DAY)
    with pytest.raises(RuntimeError, match="proof|inventory"):
        _tick(exchange, recovery, root, second_day)
    assert exchange.submits == [] and exchange.cancels == []


def test_partial_exit_history_survives_retry_restart_and_confirmed_flat(tmp_path: Path) -> None:
    root = tmp_path / "history"
    prior_day = DAY - timedelta(days=1)
    source = _source(root, prior_day)
    original = source.load_symbol_states(prior_day)
    exchange = RecoveryExchange()
    exchange.orders["TEST-entry"] = _order("TEST-entry")
    recovery = PaperStateStore(tmp_path / "recovery.sqlite3")

    assert not _tick(exchange, recovery, root, prior_day)
    first_id = str(exchange.submits[0]["client_order_id"])
    exchange.orders[first_id].update(status="canceled", filled_qty="1", filled_avg_price="20")
    exchange.holdings["TEST"] = 3
    assert not _tick(exchange, recovery, root, prior_day)
    assert [order["qty"] for order in exchange.submits] == ["4", "3"]
    second_id = str(exchange.submits[1]["client_order_id"])

    restarted = PaperStateStore(recovery.path)
    assert not _tick(exchange, restarted, root, prior_day)
    assert len(exchange.submits) == 2 and exchange.cancels == []
    exchange.orders[second_id].update(status="filled", filled_qty="3", filled_avg_price="20")
    exchange.holdings.clear()
    assert _tick(exchange, restarted, root, prior_day)
    completed = restarted.load_symbol_states(DAY)["TEST"]
    assert completed["phase"] == "complete"
    assert "fill:broker-2:3" in str(completed.get("fill_observations"))
    assert source.load_symbol_states(prior_day) == original
    assert len(exchange.submits) == 2 and all(o["side"] == "sell" for o in exchange.submits)


@pytest.mark.parametrize("unowned_quantity", [0, 1])
def test_stopped_sibling_requires_net_fill_proof_before_any_active_exit(
    tmp_path: Path, unowned_quantity: int,
) -> None:
    root = tmp_path / "history"
    prior_day = DAY - timedelta(days=1)
    source = _source(root, prior_day)
    _source(root, prior_day, symbol="STOPPED", phase="stopped")
    stopped = source.load_symbol_states(prior_day)["STOPPED"]
    stopped.update(stop_client_id="STOPPED-stop", stop_audit={"reason": "original_stop"})
    source.save_symbol_state(
        trade_date=prior_day, symbol="STOPPED", state=stopped, observed_at_utc=NOW,
    )
    original = source.load_symbol_states(prior_day)
    exchange = RecoveryExchange()
    exchange.orders["TEST-entry"] = _order("TEST-entry")
    exchange.orders["STOPPED-entry"] = _order("STOPPED-entry", symbol="STOPPED")
    exchange.orders["STOPPED-stop"] = _order("STOPPED-stop", symbol="STOPPED", side="sell")
    exchange.holdings["STOPPED"] = unowned_quantity
    recovery = PaperStateStore(tmp_path / "recovery.sqlite3")

    if unowned_quantity:
        with pytest.raises(RuntimeError, match="inventory"):
            _tick(exchange, recovery, root, prior_day)
        assert exchange.submits == [] and exchange.cancels == []
    else:
        assert not _tick(exchange, recovery, root, prior_day)
        assert [(o["symbol"], o["side"], o["qty"]) for o in exchange.submits] == [
            ("TEST", "sell", "4"),
        ]
        restored = recovery.load_symbol_states(DAY)["STOPPED"]
        assert restored["phase"] == "complete"
        assert restored["stop_audit"] == {"reason": "original_stop"}
    assert source.load_symbol_states(prior_day) == original


def test_parent_proves_unsaved_bracket_children_before_exit_only_recovery(tmp_path: Path) -> None:
    root = tmp_path / "history"
    prior_day = DAY - timedelta(days=1)
    source = _source(root, prior_day, phase="entry_pending")
    original = source.load_symbol_states(prior_day)
    exchange = RecoveryExchange()
    stop = _order("TEST-stop", side="sell", filled=0, status="new", kind="stop")
    target = _order("TEST-target", side="sell", filled=0, status="new")
    exchange.orders.update({
        "TEST-entry": {**_order("TEST-entry"), "legs": [stop, target]},
        "TEST-stop": stop, "TEST-target": target,
    })
    recovery = PaperStateStore(tmp_path / "recovery.sqlite3")

    # Both normal startup callers use this same gate before resolving the parent.
    broker = exchange.broker()
    try:
        with pytest.raises(UnknownBrokerStateError, match="TEST-stop,TEST-target"):
            source.assert_reconcilable(
                prior_day, open_orders=broker.list_open_orders(), positions=broker.list_positions(),
            )
    finally:
        broker.close()

    assert not _tick(exchange, recovery, root, prior_day)
    assert not _tick(exchange, recovery, root, prior_day)
    assert set(exchange.cancels) == {"TEST-stop-id", "TEST-target-id"}
    assert [(o["side"], o["qty"]) for o in exchange.submits] == [("sell", "4")]
    assert source.load_symbol_states(prior_day) == original


def test_stopped_with_missing_broker_proof_is_not_assumed_flat(tmp_path: Path) -> None:
    root = tmp_path / "history"
    prior_day = DAY - timedelta(days=1)
    _source(root, prior_day)
    _source(root, prior_day, symbol="STOPPED", phase="stopped")
    exchange = RecoveryExchange()
    exchange.orders["TEST-entry"] = _order("TEST-entry")
    recovery = PaperStateStore(tmp_path / "recovery.sqlite3")
    with pytest.raises(RuntimeError, match="proof"):
        _tick(exchange, recovery, root, prior_day)
    assert exchange.submits == [] and exchange.cancels == []


@pytest.mark.parametrize(
    "conflict", ["lookup-client", "bound-broker", "entry-side", "duplicate-broker"],
)
def test_all_historical_broker_identities_are_checked_before_any_exit(
    tmp_path: Path, conflict: str,
) -> None:
    root = tmp_path / "history"
    prior_day = DAY - timedelta(days=1)
    source = _source(root, prior_day)
    _source(root, prior_day, symbol="ZZZ")
    source.record_order_intent(
        trade_date=prior_day, client_order_id="ZZZ-entry", symbol="ZZZ", attempt=1,
        role="entry", quantity=4, payload={"side": "buy"}, observed_at_utc=NOW,
    )
    if conflict != "duplicate-broker":
        source.attach_broker_order(
            client_order_id="ZZZ-entry", broker_order_id="ZZZ-entry-id", status="filled",
            observed_at_utc=NOW,
        )
    exchange = RecoveryExchange()
    exchange.holdings["ZZZ"] = 4
    exchange.orders["TEST-entry"] = _order("TEST-entry")
    malformed = _order("ZZZ-entry", symbol="ZZZ")
    if conflict == "lookup-client":
        malformed["client_order_id"] = "foreign-entry"
    elif conflict == "bound-broker":
        malformed["id"] = "foreign-broker-id"
    elif conflict == "duplicate-broker":
        malformed["id"] = "TEST-entry-id"
    else:
        malformed.update(side="sell", filled_qty="0")
        exchange.holdings.pop("ZZZ")
    exchange.orders["ZZZ-entry"] = malformed
    recovery = PaperStateStore(tmp_path / "recovery.sqlite3")
    with pytest.raises(RuntimeError, match="identity"):
        _tick(exchange, recovery, root, prior_day)
    assert exchange.submits == [] and exchange.cancels == []


def test_previously_imported_source_with_a_new_active_lease_blocks_all_exits(
    tmp_path: Path,
) -> None:
    root = tmp_path / "history"
    first_day, second_day = DAY - timedelta(days=2), DAY - timedelta(days=1)
    first = _source(root, first_day)
    _source(root, second_day, symbol="OTHER")
    exchange = RecoveryExchange()
    exchange.holdings["OTHER"] = 4
    exchange.orders["TEST-entry"] = _order("TEST-entry")
    exchange.orders["OTHER-entry"] = _order("OTHER-entry", symbol="OTHER")
    recovery = PaperStateStore(tmp_path / "recovery.sqlite3")
    with pytest.raises(RuntimeError):
        _tick(exchange, recovery, root, first_day)
    assert first.claim_run(first_day, owner="old-monitor", observed_at_utc=NOW)
    with pytest.raises(RuntimeError, match="lease is active"):
        _tick(exchange, recovery, root, second_day)
    assert exchange.submits == [] and exchange.cancels == []


def test_fractional_inventory_in_later_symbol_blocks_every_exit(tmp_path: Path) -> None:
    root = tmp_path / "history"
    prior_day = DAY - timedelta(days=1)
    _source(root, prior_day)
    _source(root, prior_day, symbol="ZZZ")
    exchange = RecoveryExchange()
    exchange.orders["TEST-entry"] = _order("TEST-entry")
    exchange.orders["ZZZ-entry"] = {**_order("ZZZ-entry", symbol="ZZZ"), "filled_qty": "1.5"}
    exchange.holdings["ZZZ"] = 1.5
    recovery = PaperStateStore(tmp_path / "recovery.sqlite3")
    with pytest.raises(RuntimeError):
        _tick(exchange, recovery, root, prior_day)
    assert exchange.submits == [] and exchange.cancels == []


def test_completed_recovery_rechecks_late_entry_fill_without_rebuying(tmp_path: Path) -> None:
    root = tmp_path / "history"
    prior_day = DAY - timedelta(days=1)
    _source(root, prior_day, phase="entry_pending")
    exchange = RecoveryExchange()
    exchange.holdings.clear()
    recovery = PaperStateStore(tmp_path / "recovery.sqlite3")
    assert _tick(exchange, recovery, root, prior_day)
    assert recovery.load_symbol_states(DAY)["TEST"]["phase"] == "complete"

    exchange.orders["TEST-entry"] = _order("TEST-entry")
    exchange.holdings["TEST"] = 4
    assert not _tick(exchange, recovery, root, prior_day)
    assert [(o["side"], o["qty"]) for o in exchange.submits] == [("sell", "4")]
