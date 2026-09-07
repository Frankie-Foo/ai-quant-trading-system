"""Regressions from the independent Paper lifecycle review; all I/O is synthetic."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Thread
from typing import Any, NoReturn, Self

import httpx
import polars as pl
import pytest
from test_modern_paper_lifecycle import DAY, NOW, PaperExchange

from execution.alpaca_paper import PaperCloseRequest
from operations.autonomous_selection_handoff import create_open_confirmation
from operations.feishu_base import FeishuBaseEventClient
from operations.paper_state import PaperStateStore
from research.modern_momentum import modern_strategy_manifest
from scripts import monitor_modern_momentum_paper as paper


@pytest.fixture(autouse=True)
def isolated_io(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Unexpected network/CLI access fails before contacting any external service."""

    def forbidden(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("review regressions forbid external network and subprocess I/O")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(paper, "ROOT", tmp_path)
    monkeypatch.setattr(paper, "load_project_env", lambda _: None)
    monkeypatch.setattr(paper, "project_data_root", lambda _: tmp_path)
    yield


def _order(
    client_id: str,
    *,
    side: str = "sell",
    kind: str = "stop",
    status: str = "new",
    filled: int = 0,
) -> dict[str, Any]:
    return {
        "id": f"{client_id}-id",
        "client_order_id": client_id,
        "symbol": "TEST",
        "qty": "4",
        "filled_qty": str(filled),
        "filled_avg_price": "20" if filled else None,
        "side": side,
        "type": kind,
        "status": status,
    }


def _reconcile(exchange: PaperExchange, store: PaperStateStore, state: dict[str, object]) -> None:
    paper.reconcile_symbol_position(
        exchange.broker(),
        store,
        trade_date=DAY,
        symbol="TEST",
        position=state,
        observed_at_utc=NOW,
        cancel_entries=False,
    )


def _cumulative_fills(store: PaperStateStore, broker_id: str) -> set[str]:
    """Read the public persisted audit, not private SQLite tables or key formatting."""
    state = PaperStateStore(store.path).load_symbol_states(DAY)["TEST"]
    observations = state.get("fill_observations", {})
    assert isinstance(observations, dict)
    return {
        snapshot["order"]["filled_qty"]
        for snapshot in observations.values()
        if snapshot["order"]["id"] == broker_id
    }


def test_blocked_fill_notification_does_not_delay_residual_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1: actual close/fill must finish while the notification remains blocked."""
    opened = datetime(2026, 9, 3, 13, 30, tzinfo=UTC)
    closed = opened + timedelta(minutes=390)
    notification_started = Event()
    release_notification = Event()
    notification_finished = Event()
    protective_exit_filled = Event()
    stop_monitor = Event()
    errors: list[BaseException] = []

    class Clock(datetime):
        current = NOW

        @classmethod
        def now(cls, tz: object = None) -> Self:
            return cls.fromtimestamp(cls.current.timestamp(), tz=UTC)

    class BlockingPush:
        def push(self, body: str) -> str:
            notification_started.set()
            try:
                # Timeout is only a test watchdog; the test releases delivery in finally.
                if not release_notification.wait(10):
                    raise TimeoutError("fixture notification was not released")
                return "fixture-message"
            finally:
                notification_finished.set()

        def close(self) -> None:
            pass

    class CancelThenFillExchange(PaperExchange):
        def handle(self, request: httpx.Request) -> httpx.Response:
            # Cancellation resolves after delivery starts, requiring another broker tick.
            if notification_started.is_set() and self.orders["entry"]["status"] == "pending_cancel":
                self.orders["entry"]["status"] = "canceled"
            response = super().handle(request)
            if request.method == "POST" and request.url.path == "/v2/orders":
                submitted = self.submits[-1]
                assert submitted["side"] == "sell" and submitted["qty"] == "1"
                assert self.orders["entry"]["status"] == "canceled"
                fill = self.orders[submitted["client_order_id"]]
                fill.update(status="filled", filled_qty="1", filled_avg_price="20")
                self.quantity = 0
                protective_exit_filled.set()
                return httpx.Response(200, json=fill)
            return response

    def sleep(seconds: float) -> None:
        Clock.current = (
            closed if stop_monitor.is_set() else (Clock.current + timedelta(seconds=seconds))
        )
        stop_monitor.wait(0.005)  # Yield to delivery without depending on its implementation.

    exchange = CancelThenFillExchange()
    exchange.quantity = 1
    exchange.defer_cancel = True
    exchange.orders["entry"] = _order(
        "entry", side="buy", kind="limit", status="partially_filled", filled=1
    )
    store = PaperStateStore(
        tmp_path / "runs" / "modern-momentum" / str(DAY) / "paper-state.sqlite3"
    )
    store.save_symbol_state(
        trade_date=DAY,
        symbol="TEST",
        state={"phase": "entry_pending", "attempt": 1, "entry_client_id": "entry"},
        observed_at_utc=NOW,
    )
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(
        json.dumps({"modern_strategy_manifest": modern_strategy_manifest()}), encoding="utf-8"
    )
    confirmation_path = tmp_path / "confirmation.json"
    create_open_confirmation(
        trade_date=DAY,
        selection_snapshot_id="fixture-selection",
        candidate_pool=("TEST",),
        feishu_record_ids=("fixture-base",),
        livermore_message_id="fixture-message",
        strategy_version=paper.STRATEGY_VERSION,
        config_path=plan_path,
        confirmation_path=confirmation_path,
        generated_at_utc=opened,
    )
    pool = pl.DataFrame(
        dict(
            symbol=["TEST"],
            price=[20.0],
            forward_market_cap=[2e9],
            rvol=[2.0],
            hard_catalyst=[True],
        )
    )
    monkeypatch.setattr(paper, "_latest_pool", lambda *_: pool)
    monkeypatch.setattr(
        paper,
        "build_xnys_schedule",
        lambda *_: pl.DataFrame({"market_open_utc": [opened], "market_close_utc": [closed]}),
    )
    monkeypatch.setattr(paper, "_broker", lambda **_: exchange.broker())
    monkeypatch.setattr(paper, "_push_client", BlockingPush)
    monkeypatch.setattr(FeishuBaseEventClient, "from_environment", lambda: None)
    monkeypatch.setattr(paper, "fetch_bars", lambda *_: pl.DataFrame({"symbol": []}))
    monkeypatch.setattr(paper, "datetime", Clock)
    monkeypatch.setattr(time, "sleep", sleep)
    monkeypatch.setenv("BROKER_WRITE_ENABLED", "true")
    monkeypatch.setenv("TRADING_KILL_SWITCH", "false")
    monkeypatch.setenv("AI_QUANT_PAPER_SMOKE_MAX_NOTIONAL", "100")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "monitor",
            "--trade-date",
            str(DAY),
            "--arm-paper",
            "--confirmation-path",
            str(confirmation_path),
        ],
    )

    def run_monitor() -> None:
        try:
            paper.main()
        except BaseException as exc:
            errors.append(exc)

    monitor = Thread(target=run_monitor, name="review-monitor-fixture", daemon=True)
    monitor.start()
    try:
        assert notification_started.wait(3), f"notification was never attempted: {errors!r}"
        assert protective_exit_filled.wait(2), "blocked notification prevented the residual exit"
        assert not notification_finished.is_set(), "notification must still be blocked"
        assert exchange.quantity == 0
    finally:
        stop_monitor.set()
        Clock.current = closed
        release_notification.set()
        monitor.join(timeout=5)
        assert not monitor.is_alive(), "fixture monitor did not stop after notification release"
    assert errors == []


def test_unknown_inventory_preserves_existing_stop_before_refusing_exit(tmp_path: Path) -> None:
    """P1: an ownership failure cannot remove the existing protective order."""
    exchange = PaperExchange()
    exchange.quantity = 5
    exchange.orders["stop"] = _order("stop")
    store = PaperStateStore(tmp_path / "paper.sqlite3")
    state: dict[str, object] = {
        "phase": "exit_pending",
        "attempt": 1,
        "shares": 4,
        "stop_client_id": "stop",
    }
    with pytest.raises(RuntimeError):
        _reconcile(exchange, store, state)
    assert exchange.cancels == [], "ownership validation ran after the stop was canceled"
    assert exchange.submits == []
    assert exchange.orders["stop"]["status"] == "new"


def test_exit_lookup_gap_never_cancels_or_resubmits_visible_live_exit(tmp_path: Path) -> None:
    """P1: a point-lookup 404 cannot overrule the live exit in open orders."""

    class LookupGapExchange(PaperExchange):
        def handle(self, request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v2/orders:by_client_order_id":
                if request.url.params["client_order_id"] == "exit":
                    return httpx.Response(404, json={})
            if request.method == "POST" and request.url.path == "/v2/orders":
                # A real duplicate client ID cannot create another order in the fixture.
                self.submits.append(json.loads(request.content))
                return httpx.Response(422, json={})
            return super().handle(request)

    exchange = LookupGapExchange()
    exchange.orders["exit"] = _order("exit", kind="market")
    request = PaperCloseRequest(client_order_id="exit", symbol="TEST", qty=4)
    state: dict[str, object] = {
        "phase": "exit_pending",
        "attempt": 1,
        "shares": 4,
        "exit_client_id": "exit",
        "exit_request": request.model_dump(mode="json"),
    }
    store = PaperStateStore(tmp_path / "paper.sqlite3")
    try:
        result = paper.request_position_exit(
            exchange.broker(),
            store,
            trade_date=DAY,
            symbol="TEST",
            position=state,
            observed_at_utc=NOW,
            reason="time_exit",
        )
    except RuntimeError:
        # Either preserve/return the known exit or fail closed without broker writes.
        result = None
    assert exchange.cancels == [], "the visible live exit was canceled after a lookup gap"
    assert exchange.submits == [], "an unresolved existing exit must not be replayed"
    assert exchange.orders["exit"]["status"] == "new"
    if result is not None:
        assert result.id == "exit-id"


def test_unknown_protective_status_is_not_accepted_as_healthy(tmp_path: Path) -> None:
    """P1: unknown broker status must surface a fault to the monitor's freeze guard."""
    exchange = PaperExchange()
    exchange.orders["stop"] = _order("stop", status="mystery_status")
    exchange.orders["target"] = _order("target", kind="limit")
    store = PaperStateStore(tmp_path / "paper.sqlite3")
    state: dict[str, object] = {
        "phase": "active",
        "attempt": 1,
        "shares": 4,
        "stop_client_id": "stop",
        "target_client_id": "target",
    }
    with pytest.raises((RuntimeError, ValueError)):
        _reconcile(exchange, store, state)
    assert exchange.cancels == [] and exchange.submits == []


def test_late_partial_entry_fill_is_audited_across_restart_and_completion(tmp_path: Path) -> None:
    """P2: a parent fills from 1 to 2 while cancellation is pending."""
    exchange = PaperExchange()
    exchange.quantity = 1
    exchange.defer_cancel = True
    exchange.orders["entry"] = _order(
        "entry", side="buy", kind="limit", status="partially_filled", filled=1
    )
    store = PaperStateStore(tmp_path / "paper.sqlite3")
    state: dict[str, object] = {"phase": "entry_pending", "attempt": 1, "entry_client_id": "entry"}
    _reconcile(exchange, store, state)
    assert exchange.submits == []  # No sell before cancellation confirmation.
    assert _cumulative_fills(store, "entry-id") == {"1"}

    exchange.orders["entry"].update(status="canceled", filled_qty="2")
    exchange.quantity = 2
    state = PaperStateStore(store.path).load_symbol_states(DAY)["TEST"]
    _reconcile(exchange, store, state)
    assert len(exchange.submits) == 1 and exchange.submits[0]["qty"] == "2"
    exit_id = str(state["exit_client_id"])
    exchange.orders[exit_id].update(status="filled", filled_qty="2", filled_avg_price="20")
    exchange.quantity = 0
    state = PaperStateStore(store.path).load_symbol_states(DAY)["TEST"]
    _reconcile(exchange, store, state)

    assert PaperStateStore(store.path).load_symbol_states(DAY)["TEST"]["phase"] == "complete"
    assert "2" in _cumulative_fills(store, "entry-id"), "final cumulative buy fill was lost"
    assert "2" in _cumulative_fills(store, exchange.orders[exit_id]["id"])


def test_late_stop_fill_is_audited_across_restart_and_completion(tmp_path: Path) -> None:
    """P2: the stop finishes from 1 to 4 during cancellation, leaving no residual."""
    exchange = PaperExchange()
    exchange.quantity = 3
    exchange.defer_cancel = True
    exchange.orders["stop"] = _order("stop", status="partially_filled", filled=1)
    exchange.orders["target"] = _order("target", kind="limit")
    store = PaperStateStore(tmp_path / "paper.sqlite3")
    state: dict[str, object] = {
        "phase": "active",
        "attempt": 1,
        "shares": 4,
        "stop_client_id": "stop",
        "target_client_id": "target",
    }
    _reconcile(exchange, store, state)
    assert exchange.submits == []
    assert _cumulative_fills(store, "stop-id") == {"1"}

    exchange.orders["stop"].update(status="filled", filled_qty="4")
    exchange.orders["target"]["status"] = "canceled"
    exchange.quantity = 0
    state = PaperStateStore(store.path).load_symbol_states(DAY)["TEST"]
    _reconcile(exchange, store, state)

    assert PaperStateStore(store.path).load_symbol_states(DAY)["TEST"]["phase"] == "complete"
    assert exchange.submits == [], "a fully exited holding must not produce another sell"
    assert "4" in _cumulative_fills(store, "stop-id"), "final cumulative stop fill was lost"


def test_active_position_lookup_gap_preserves_partial_stop_and_nonterminal_state(
    tmp_path: Path,
) -> None:
    """An empty positions response cannot prove flat after only 1 of 4 shares sold."""

    class MissingPositionExchange(PaperExchange):
        position_lookups = 0

        def handle(self, request: httpx.Request) -> httpx.Response:
            if request.method == "GET" and request.url.path == "/v2/positions":
                self.position_lookups += 1
                return httpx.Response(200, json=[])
            return super().handle(request)

    exchange = MissingPositionExchange()
    exchange.quantity = 3  # Actual fixture inventory; only the positions endpoint omits it.
    exchange.orders["entry"] = _order("entry", side="buy", kind="limit", status="filled", filled=4)
    exchange.orders["stop"] = _order("stop", status="partially_filled", filled=1)
    exchange.orders["target"] = _order("target", kind="limit")
    store = PaperStateStore(tmp_path / "paper.sqlite3")
    state: dict[str, object] = {
        "phase": "active",
        "attempt": 1,
        "shares": 4,
        "entry_client_id": "entry",
        "stop_client_id": "stop",
        "target_client_id": "target",
    }
    store.save_symbol_state(trade_date=DAY, symbol="TEST", state=state, observed_at_utc=NOW)
    try:
        _reconcile(exchange, store, state)
    except RuntimeError:
        pass  # Reporting the inconsistency is valid, but must preserve protection/state.

    persisted = PaperStateStore(store.path).load_symbol_states(DAY)["TEST"]
    assert exchange.position_lookups > 0, "the active holdingNone branch was not exercised"
    assert (exchange.cancels, exchange.submits) == ([], []), (
        f"contradictory flat lookup changed broker orders; phase={state['phase']}, "
        f"persisted_phase={persisted['phase']}"
    )
    assert state["phase"] not in {"complete", "stopped"}
    assert persisted["phase"] not in {"complete", "stopped"}
    assert exchange.quantity == 3
