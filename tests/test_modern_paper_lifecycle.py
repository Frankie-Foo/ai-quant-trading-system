from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Self

import httpx
import pytest
from pydantic import SecretStr

from execution.alpaca_paper import DirectAlpacaPaperBroker
from operations.paper_state import PaperStateStore
from scripts import monitor_modern_momentum_paper as paper

DAY = date(2026, 9, 3)
NOW = datetime(2026, 9, 3, 19, 50, tzinfo=UTC)


class PaperExchange:
    """External API fixture: no sockets, no real account or market facts."""

    def __init__(self) -> None:
        self.quantity = 4
        self.orders: dict[str, dict[str, Any]] = {}
        self.submits: list[dict[str, Any]] = []
        self.cancels: list[str] = []
        self.defer_cancel = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        import json

        path = request.url.path
        if path == "/v2/account":
            return httpx.Response(
                200,
                json=dict(
                    status="ACTIVE",
                    account_blocked=False,
                    trading_blocked=False,
                    equity="100000",
                    last_equity="100000",
                    buying_power="100000",
                ),
            )
        if path == "/v2/positions":
            return httpx.Response(
                200,
                json=(
                    [
                        {
                            "symbol": "TEST",
                            "qty": str(self.quantity),
                            "side": "long",
                            "market_value": str(self.quantity * 20),
                        }
                    ]
                    if self.quantity
                    else []
                ),
            )
        if path == "/v2/orders:by_client_order_id":
            order = self.orders.get(request.url.params["client_order_id"])
            return httpx.Response(200 if order else 404, json=order or {})
        if path == "/v2/orders" and request.method == "GET":
            return httpx.Response(
                200,
                json=[
                    o
                    for o in self.orders.values()
                    if o["status"] not in {"filled", "canceled", "rejected", "expired"}
                ],
            )
        if path == "/v2/orders" and request.method == "POST":
            payload = json.loads(request.content)
            self.submits.append(payload)
            order = {
                **payload,
                "id": f"broker-{len(self.submits)}",
                "status": "new",
                "filled_qty": "0",
            }
            self.orders[payload["client_order_id"]] = order
            return httpx.Response(200, json=order)
        if request.method == "DELETE":
            broker_id = path.rsplit("/", 1)[-1]
            self.cancels.append(broker_id)
            for order in self.orders.values():
                if order["id"] == broker_id:
                    order["status"] = "pending_cancel" if self.defer_cancel else "canceled"
            return httpx.Response(204)
        raise AssertionError(f"unexpected fixture request {request.method} {path}")

    def broker(self) -> DirectAlpacaPaperBroker:
        return DirectAlpacaPaperBroker(
            key_id=SecretStr("fixture"),
            secret_key=SecretStr("fixture"),
            writes_enabled=True,
            client=httpx.Client(transport=httpx.MockTransport(self.handle)),
        )


def test_slow_flatten_order_survives_following_ticks_and_restart(tmp_path: Path) -> None:
    exchange = PaperExchange()
    store = PaperStateStore(tmp_path / "paper.sqlite3")
    state: dict[str, object] = {"phase": "active", "attempt": 1, "shares": 4}
    broker = exchange.broker()
    first = paper.request_position_exit(
        broker,
        store,
        trade_date=DAY,
        symbol="TEST",
        position=state,
        observed_at_utc=NOW,
        reason="time_exit",
    )
    restored = PaperStateStore(store.path).load_symbol_states(DAY)["TEST"]
    second = paper.request_position_exit(
        broker,
        store,
        trade_date=DAY,
        symbol="TEST",
        position=restored,
        observed_at_utc=NOW,
        reason="time_exit",
    )
    assert first is not None and second is not None and first.id == second.id
    assert len(exchange.submits) == 1
    assert exchange.cancels == []


def test_canceled_partial_exit_retries_only_residual_with_new_id(tmp_path: Path) -> None:
    exchange = PaperExchange()
    store = PaperStateStore(tmp_path / "paper.sqlite3")
    state: dict[str, object] = {"phase": "active", "attempt": 1, "shares": 4}
    broker = exchange.broker()
    first = paper.request_position_exit(
        broker,
        store,
        trade_date=DAY,
        symbol="TEST",
        position=state,
        observed_at_utc=NOW,
        reason="time_exit",
    )
    assert first is not None
    exchange.orders[first.client_order_id].update(
        status="canceled", filled_qty="1", filled_avg_price="20"
    )
    exchange.quantity = 3
    second = paper.request_position_exit(
        broker,
        store,
        trade_date=DAY,
        symbol="TEST",
        position=state,
        observed_at_utc=NOW,
        reason="time_exit",
    )
    assert second is not None and second.client_order_id != first.client_order_id
    assert second.qty == 3
    assert len(exchange.submits) == 2


def test_cancel_ack_is_required_before_new_exit(tmp_path: Path) -> None:
    exchange = PaperExchange()
    exchange.defer_cancel = True
    exchange.orders["stop"] = dict(
        id="stop-id",
        client_order_id="stop",
        symbol="TEST",
        qty="4",
        filled_qty="0",
        side="sell",
        type="stop",
        status="new",
    )
    store = PaperStateStore(tmp_path / "paper.sqlite3")
    state: dict[str, object] = {
        "phase": "active",
        "attempt": 1,
        "shares": 4,
        "stop_client_id": "stop",
    }
    broker = exchange.broker()
    result = paper.request_position_exit(
        broker,
        store,
        trade_date=DAY,
        symbol="TEST",
        position=state,
        observed_at_utc=NOW,
        reason="time_exit",
    )
    assert result is None and exchange.submits == []
    assert state["phase"] == "exit_pending"
    exchange.orders["stop"]["status"] = "canceled"
    result = paper.request_position_exit(
        broker,
        store,
        trade_date=DAY,
        symbol="TEST",
        position=state,
        observed_at_utc=NOW,
        reason="time_exit",
    )
    assert result is not None and result.qty == 4


@pytest.mark.parametrize("value", [None, 0, -1, float("nan"), float("inf"), 200_000.01])
def test_arming_never_bypasses_smoke_release_cap(value: float | None) -> None:
    with pytest.raises(ValueError):
        paper.validate_smoke_notional(value)


def test_one_expensive_symbol_is_a_candidate_rejection_not_system_fault() -> None:
    with pytest.raises(paper.CandidateRejected, match="one_share"):
        paper.position_size(
            entry_price=250,
            all_in_stop_pct=0.01,
            equity=100000,
            buying_power=100000,
            risk_fraction=0.003,
            remaining_slots=3,
            max_notional=100,
        )


def test_missing_old_entry_intent_is_never_replayed(tmp_path: Path) -> None:
    exchange = PaperExchange()
    exchange.quantity = 0
    store = PaperStateStore(tmp_path / "paper.sqlite3")
    state: dict[str, object] = {
        "phase": "entry_pending",
        "attempt": 1,
        "entry_client_id": "missing",
        "entry_request": {"qty": 4},
    }
    paper.reconcile_symbol_position(
        exchange.broker(),
        store,
        trade_date=DAY,
        symbol="TEST",
        position=state,
        observed_at_utc=NOW,
        cancel_entries=True,
    )
    assert exchange.submits == []
    assert state["phase"] == "complete"
    assert state["reason"] == "unconfirmed_entry_not_replayed"


def test_partial_buy_is_audited_and_closed_without_waiting_forever(tmp_path: Path) -> None:
    exchange = PaperExchange()
    exchange.quantity = 1
    exchange.orders["entry"] = dict(
        id="entry-id",
        client_order_id="entry",
        symbol="TEST",
        qty="4",
        filled_qty="1",
        filled_avg_price="20",
        side="buy",
        type="limit",
        status="partially_filled",
    )
    store = PaperStateStore(tmp_path / "paper.sqlite3")
    state: dict[str, object] = {"phase": "entry_pending", "attempt": 1, "entry_client_id": "entry"}
    paper.reconcile_symbol_position(
        exchange.broker(),
        store,
        trade_date=DAY,
        symbol="TEST",
        position=state,
        observed_at_utc=NOW,
        cancel_entries=False,
    )
    assert exchange.cancels == ["entry-id"]
    assert len(exchange.submits) == 1 and exchange.submits[0]["side"] == "sell"
    assert exchange.submits[0]["qty"] == "1"
    assert state["fill_observations"]
    assert state["phase"] == "exit_pending"


def test_exit_does_not_sell_an_unexplained_extra_share(tmp_path: Path) -> None:
    exchange = PaperExchange()
    exchange.quantity = 5
    store = PaperStateStore(tmp_path / "paper.sqlite3")
    state: dict[str, object] = {"phase": "active", "attempt": 1, "shares": 4}
    with pytest.raises(RuntimeError, match="owned"):
        paper.request_position_exit(
            exchange.broker(),
            store,
            trade_date=DAY,
            symbol="TEST",
            position=state,
            observed_at_utc=NOW,
            reason="time_exit",
        )
    assert exchange.submits == []


def test_partial_exit_fill_remains_audited_through_retry_and_completion(tmp_path: Path) -> None:
    exchange = PaperExchange()
    store = PaperStateStore(tmp_path / "paper.sqlite3")
    state: dict[str, object] = {"phase": "exit_pending", "attempt": 1, "shares": 4}
    broker = exchange.broker()
    paper.reconcile_symbol_position(
        broker,
        store,
        trade_date=DAY,
        symbol="TEST",
        position=state,
        observed_at_utc=NOW,
        cancel_entries=True,
    )
    first = str(state["exit_client_id"])
    exchange.orders[first].update(status="canceled", filled_qty="1", filled_avg_price="20")
    exchange.quantity = 3
    paper.reconcile_symbol_position(
        broker,
        store,
        trade_date=DAY,
        symbol="TEST",
        position=state,
        observed_at_utc=NOW,
        cancel_entries=True,
    )
    second = str(state["exit_client_id"])
    assert first != second and state["phase"] == "exit_pending"
    exchange.orders[second].update(status="filled", filled_qty="3", filled_avg_price="19.9")
    exchange.quantity = 0
    paper.reconcile_symbol_position(
        broker,
        store,
        trade_date=DAY,
        symbol="TEST",
        position=state,
        observed_at_utc=NOW,
        cancel_entries=True,
    )
    persisted = store.load_symbol_states(DAY)["TEST"]
    assert persisted["phase"] == "complete"
    observations = persisted["fill_observations"]
    assert isinstance(observations, dict) and len(observations) == 2


def test_real_monitor_skips_expensive_symbol_but_submits_next_valid_bracket(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whole monitor iteration with real signal/risk/store and fake external I/O."""
    import json
    import sys
    import time
    from datetime import timedelta
    from decimal import Decimal

    import polars as pl

    from execution.alpaca_paper import FreshNbboQuote
    from operations.autonomous_selection_handoff import create_open_confirmation
    from operations.feishu_base import FeishuBaseEventClient
    from research.modern_momentum import modern_strategy_manifest

    opened = datetime(2026, 9, 3, 13, 30, tzinfo=UTC)
    closed = opened + timedelta(minutes=390)

    class Clock(datetime):
        current = opened + timedelta(minutes=28)

        @classmethod
        def now(cls, tz: object = None) -> Self:
            return cls.fromtimestamp(cls.current.timestamp(), tz=UTC)

    class Push:
        messages: list[str] = []

        def push(self, body: str) -> str:
            self.messages.append(body)
            return "fixture-message"

        def close(self) -> None:
            pass

    def sleep(_: float) -> None:
        Clock.current = closed

    rows = []
    for symbol, scale in [("EXPENSIVE", 1.0), ("CHEAP", 0.5)]:
        for minute in range(28):
            close = 99.4 + min(minute, 14) * 0.035
            if 15 <= minute < 26:
                close = 99.85
            if minute >= 26:
                close = 100.45 + (minute - 26) * 0.08
            rows.append(
                dict(
                    symbol=symbol,
                    ts_utc=opened + timedelta(minutes=minute),
                    open=(close - 0.02) * scale,
                    high=(close + 0.08) * scale,
                    low=(close - 0.08) * scale,
                    close=close * scale,
                    vwap=close * scale,
                    volume=10000,
                )
            )
    frame = pl.DataFrame(rows)
    pool = pl.DataFrame(
        dict(
            symbol=["EXPENSIVE", "CHEAP"],
            price=[96.0, 48.0],
            forward_market_cap=[2e9, 2e9],
            rvol=[2.0, 2.0],
            hard_catalyst=[True, True],
        )
    )
    confirmation_path = tmp_path / "confirmation.json"
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({"modern_strategy_manifest": modern_strategy_manifest()}))
    create_open_confirmation(
        trade_date=DAY,
        selection_snapshot_id="fixture-selection",
        candidate_pool=("EXPENSIVE", "CHEAP"),
        feishu_record_ids=("fixture-base",),
        livermore_message_id="fixture-message",
        strategy_version=paper.STRATEGY_VERSION,
        config_path=plan_path,
        confirmation_path=confirmation_path,
        generated_at_utc=opened,
    )
    exchange = PaperExchange()
    exchange.quantity = 0
    push = Push()
    broker = exchange.broker()
    monkeypatch.setattr(paper, "ROOT", tmp_path)
    monkeypatch.setattr(paper, "load_project_env", lambda _: None)
    monkeypatch.setattr(paper, "project_data_root", lambda _: tmp_path)
    monkeypatch.setattr(paper, "_latest_pool", lambda *_: pool)
    monkeypatch.setattr(
        paper,
        "build_xnys_schedule",
        lambda *_: pl.DataFrame({"market_open_utc": [opened], "market_close_utc": [closed]}),
    )
    monkeypatch.setattr(paper, "_broker", lambda **_: broker)
    monkeypatch.setattr(paper, "_push_client", lambda: push)
    monkeypatch.setattr(FeishuBaseEventClient, "from_environment", lambda: None)
    monkeypatch.setattr(paper, "fetch_bars", lambda *_: frame)
    monkeypatch.setattr(
        paper,
        "_latest_sip_nbbo_now",
        lambda symbol: FreshNbboQuote(
            symbol=symbol,
            bid=Decimal("100.52" if symbol == "EXPENSIVE" else "50.26"),
            ask=Decimal("100.54" if symbol == "EXPENSIVE" else "50.27"),
            asof_utc=Clock.current,
            feed="sip",
        ),
    )
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
    paper.main()
    assert len(exchange.submits) == 1
    assert exchange.submits[0]["symbol"] == "CHEAP"
    assert exchange.submits[0]["order_class"] == "bracket"
    assert exchange.submits[0]["qty"] == "1"
    assert push.messages == []  # Submission is not a fill and not a system failure.
    state = json.loads(
        (tmp_path / "runs" / "modern-momentum" / str(DAY) / "paper-state.json").read_text()
    )
    assert "one_share" in state["candidate_blocks"]["EXPENSIVE"]["reason"]
    assert state["attempts"] == {"CHEAP": 1}


def test_prior_day_recovery_proves_fills_and_never_reopens_an_entry(tmp_path: Path) -> None:
    from datetime import timedelta

    prior_day = DAY - timedelta(days=1)
    history_root = tmp_path / "history"
    previous = PaperStateStore(history_root / str(prior_day) / "paper-state.sqlite3")
    previous.save_symbol_state(
        trade_date=prior_day,
        symbol="TEST",
        state={
            "phase": "active",
            "attempt": 1,
            "shares": 4,
            "entry_client_id": "old-entry",
        },
        observed_at_utc=NOW - timedelta(days=1),
    )
    exchange = PaperExchange()
    exchange.orders["old-entry"] = dict(
        id="old-entry-id",
        client_order_id="old-entry",
        symbol="TEST",
        side="buy",
        type="limit",
        qty="4",
        filled_qty="4",
        filled_avg_price="20",
        status="filled",
    )
    recovery = PaperStateStore(tmp_path / "recovery.sqlite3")
    original = previous.load_symbol_states(prior_day)
    paper.recover_prior_day_tick(
        exchange.broker(),
        recovery,
        history_root=history_root,
        source_trade_date=prior_day,
        trade_date=DAY,
        observed_at_utc=NOW,
    )
    assert len(exchange.submits) == 1
    assert exchange.submits[0]["side"] == "sell" and exchange.submits[0]["qty"] == "4"
    assert previous.load_symbol_states(prior_day) == original
    assert recovery.load_symbol_states(DAY)["TEST"]["recovery_only"] is True
    paper.recover_prior_day_tick(
        exchange.broker(),
        recovery,
        history_root=history_root,
        source_trade_date=prior_day,
        trade_date=DAY,
        observed_at_utc=NOW,
    )
    assert len(exchange.submits) == 1 and exchange.cancels == []


def test_prior_day_recovery_refuses_same_symbol_manual_exposure(tmp_path: Path) -> None:
    from datetime import timedelta

    prior_day = DAY - timedelta(days=1)
    history_root = tmp_path / "history"
    previous = PaperStateStore(history_root / str(prior_day) / "paper-state.sqlite3")
    previous.save_symbol_state(
        trade_date=prior_day,
        symbol="TEST",
        state={
            "phase": "active",
            "attempt": 1,
            "shares": 4,
            "entry_client_id": "old-entry",
            "stop_client_id": "old-stop",
        },
        observed_at_utc=NOW - timedelta(days=1),
    )
    exchange = PaperExchange()
    exchange.orders["old-entry"] = dict(
        id="old-entry-id",
        client_order_id="old-entry",
        symbol="TEST",
        side="buy",
        type="limit",
        qty="4",
        filled_qty="4",
        filled_avg_price="20",
        status="filled",
    )
    exchange.orders["old-stop"] = dict(
        id="old-stop-id",
        client_order_id="old-stop",
        symbol="TEST",
        side="sell",
        type="stop",
        qty="4",
        filled_qty="4",
        filled_avg_price="19",
        status="filled",
    )
    recovery = PaperStateStore(tmp_path / "recovery.sqlite3")
    with pytest.raises(RuntimeError, match="inventory"):
        paper.recover_prior_day_tick(
            exchange.broker(),
            recovery,
            history_root=history_root,
            source_trade_date=prior_day,
            trade_date=DAY,
            observed_at_utc=NOW,
        )
    assert exchange.submits == [] and exchange.cancels == []


def test_position_lookup_lag_does_not_falsely_complete_a_filled_buy(tmp_path: Path) -> None:
    exchange = PaperExchange()
    exchange.quantity = 0  # Position endpoint lags the confirmed buy fill.
    exchange.orders["entry"] = dict(
        id="entry-id",
        client_order_id="entry",
        symbol="TEST",
        side="buy",
        type="limit",
        qty="4",
        filled_qty="1",
        filled_avg_price="20",
        status="canceled",
    )
    store = PaperStateStore(tmp_path / "state.sqlite3")
    state: dict[str, object] = dict(phase="exit_pending", attempt=1, entry_client_id="entry")
    with pytest.raises(RuntimeError, match="inventory"):
        paper.reconcile_symbol_position(
            exchange.broker(),
            store,
            trade_date=DAY,
            symbol="TEST",
            position=state,
            observed_at_utc=NOW,
            cancel_entries=True,
        )
    assert state["phase"] != "complete"
