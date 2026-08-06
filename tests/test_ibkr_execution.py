from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from execution.ibkr_execution import (
    BrokerAccountSnapshot,
    BrokerOrderRejected,
    BrokerOrderRequest,
    BrokerSubmission,
    BrokerWhatIf,
    ExecutionDesk,
)

NOW = datetime(2026, 8, 3, 2, 0, tzinfo=UTC)


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class MemoryBroker:
    def __init__(self) -> None:
        self.mode_accounts = {4001: "U7654321"}
        self.connected_port: int | None = None
        self.connection_lost = False
        self.api_read_only = False
        self.positions: tuple[dict[str, object], ...] = ()
        self.open_orders: tuple[dict[str, object], ...] = ()
        self.account_snapshot_calls = 0
        self.fail_account_snapshot_on_call: int | None = None
        self.what_if_result = BrokerWhatIf(
            accepted=True,
            estimated_commission=Decimal("1.25"),
            initial_margin_change=Decimal("500"),
            warning=None,
        )
        self.what_if_requests: list[BrokerOrderRequest] = []
        self.submissions: list[tuple[BrokerOrderRequest, str]] = []
        self.orders_by_ref: dict[str, BrokerSubmission] = {}
        self.submit_error: Exception | None = None
        self.lookup_refs: list[str] = []
        self.submission_status = "submitted"

    def connect(self, *, host: str, port: int, client_id: int) -> None:
        self.connected_port = port
        self.connection_lost = False

    def disconnect(self) -> None:
        self.connected_port = None

    @property
    def connected(self) -> bool:
        return self.connected_port is not None and not self.connection_lost

    def account_snapshot(self) -> BrokerAccountSnapshot:
        assert self.connected_port is not None
        self.account_snapshot_calls += 1
        if self.fail_account_snapshot_on_call == self.account_snapshot_calls:
            raise TimeoutError("account refresh timeout")
        return BrokerAccountSnapshot(
            account_id=self.mode_accounts[self.connected_port],
            api_read_only=self.api_read_only,
            positions=self.positions,
            open_orders=self.open_orders,
        )

    def what_if(self, request: BrokerOrderRequest) -> BrokerWhatIf:
        self.what_if_requests.append(request)
        return self.what_if_result

    def submit(self, request: BrokerOrderRequest, *, order_ref: str) -> BrokerSubmission:
        self.submissions.append((request, order_ref))
        if self.submit_error is not None:
            raise self.submit_error
        result = BrokerSubmission(
            status=self.submission_status,
            order_id=9001,
            perm_id=70001,
            order_ref=order_ref,
        )
        self.orders_by_ref[order_ref] = result
        return result

    def find_by_order_ref(self, order_ref: str) -> BrokerSubmission | None:
        self.lookup_refs.append(order_ref)
        return self.orders_by_ref.get(order_ref)


class ConcurrentProbeBroker(MemoryBroker):
    def __init__(self) -> None:
        super().__init__()
        self.active_what_if = 0
        self.max_active_what_if = 0
        self.probe_lock = threading.Lock()

    def what_if(self, request: BrokerOrderRequest) -> BrokerWhatIf:
        with self.probe_lock:
            self.active_what_if += 1
            self.max_active_what_if = max(self.max_active_what_if, self.active_what_if)
        time.sleep(0.04)
        try:
            return super().what_if(request)
        finally:
            with self.probe_lock:
                self.active_what_if -= 1


def test_bounded_execution_limits_are_required(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="max_notional"):
        ExecutionDesk(
            tmp_path / "notional.sqlite3",
            MemoryBroker(),
            live_account="U7654321",
            max_notional=Decimal("0"),
        )
    with pytest.raises(ValueError, match="preview_ttl"):
        ExecutionDesk(
            tmp_path / "ttl.sqlite3",
            MemoryBroker(),
            live_account="U7654321",
            preview_ttl=timedelta(seconds=61),
        )


def test_first_connection_requires_exact_account_binding_before_any_order_flow(
    tmp_path: Path,
) -> None:
    desk = ExecutionDesk(
        tmp_path / "ibkr.sqlite3",
        MemoryBroker(),
        live_account=None,
        clock=lambda: NOW,
    )

    connected = desk.handle({"kind": "connect"})

    assert connected["connected"] is True
    assert connected["account_bound"] is False
    assert connected["account_masked"] == "U***4321"
    assert connected["arm_confirmation_phrase"] is None
    assert connected["binding_confirmation_phrase"] == "绑定实盘账户 U***4321"
    with pytest.raises(RuntimeError, match="not bound"):
        desk.handle({"kind": "arm", "confirmation": "启用实盘下单 U***4321"})
    with pytest.raises(RuntimeError, match="not bound"):
        desk.handle({"kind": "preview", "order": {}})
    with pytest.raises(RuntimeError, match="not bound"):
        desk.handle({"kind": "submit"})
    with pytest.raises(RuntimeError, match="binding confirmation"):
        desk.handle({"kind": "bind_account", "confirmation": "绑定"})

    bound = desk.handle(
        {
            "kind": "bind_account",
            "confirmation": connected["binding_confirmation_phrase"],
        }
    )

    assert bound["schema_version"] == "ibkr.execution.v1"
    assert bound["kind"] == "account_binding_receipt"
    assert bound["actual_account_id"] == "U7654321"
    assert bound["account_masked"] == "U***4321"
    assert desk.snapshot()["account_bound"] is True
    assert desk.snapshot()["binding_confirmation_phrase"] is None


def test_default_is_disconnected_disarmed_live_and_connect_uses_only_port_4001(
    tmp_path: Path,
) -> None:
    broker = MemoryBroker()
    desk = ExecutionDesk(
        tmp_path / "ibkr.sqlite3",
        broker,
        live_account="U7654321",
        clock=lambda: NOW,
    )

    initial = desk.snapshot()
    live = desk.handle({"kind": "connect"})

    assert initial["schema_version"] == "ibkr.execution.v1"
    assert initial["kind"] == "execution_snapshot"
    assert initial["mode"] == "live"
    assert initial["port"] == 4001
    assert initial["max_order_notional"] == "10000"
    assert initial["enabled"] is False
    assert initial["connected"] is False
    assert initial["writes_armed"] is False
    assert live["mode"] == "live"
    assert live["port"] == 4001
    assert live["enabled"] is True
    assert live["connected"] is True
    assert live["account_masked"] == "U***4321"
    assert live["writes_armed"] is False
    assert live["positions"] == []
    assert live["open_orders"] == []
    assert live["recovery_required"] is False
    assert live["last_error"] is None


def test_connect_rejects_an_account_that_does_not_match_the_live_account(
    tmp_path: Path,
) -> None:
    broker = MemoryBroker()
    desk = ExecutionDesk(
        tmp_path / "ibkr.sqlite3",
        broker,
        live_account="U0000000",
        clock=lambda: NOW,
    )

    with pytest.raises(RuntimeError, match="does not match"):
        desk.handle({"kind": "connect"})

    status = desk.snapshot()
    assert status["connected"] is False
    assert status["writes_armed"] is False
    assert status["account_masked"] is None
    assert status["last_error"] == "account_mismatch"


def test_live_arm_requires_account_bound_phrase_and_api_write_access(tmp_path: Path) -> None:
    broker = MemoryBroker()
    desk = ExecutionDesk(
        tmp_path / "ibkr.sqlite3",
        broker,
        live_account="U7654321",
        clock=lambda: NOW,
    )
    connected = desk.handle({"kind": "connect"})

    assert connected["arm_confirmation_phrase"] == "启用实盘下单 U***4321"
    with pytest.raises(RuntimeError, match="confirmation"):
        desk.handle({"kind": "arm", "confirmation": "yes"})
    assert desk.handle({"kind": "arm", "confirmation": connected["arm_confirmation_phrase"]})[
        "writes_armed"
    ]

    broker.api_read_only = True
    connected = desk.handle({"kind": "connect"})
    with pytest.raises(RuntimeError, match="read-only"):
        desk.handle({"kind": "arm", "confirmation": connected["arm_confirmation_phrase"]})
    assert desk.snapshot()["writes_armed"] is False


def test_live_write_authority_expires_after_five_minutes(tmp_path: Path) -> None:
    clock = MutableClock(NOW)
    desk = ExecutionDesk(
        tmp_path / "ibkr.sqlite3",
        MemoryBroker(),
        live_account="U7654321",
        clock=clock,
    )
    connected = desk.handle({"kind": "connect"})
    armed = desk.handle(
        {"kind": "arm", "confirmation": connected["arm_confirmation_phrase"]}
    )

    assert armed["armed_until_utc"] == (NOW + timedelta(minutes=5)).isoformat()
    clock.value = NOW + timedelta(minutes=5)
    expired = desk.snapshot()
    assert expired["writes_armed"] is False
    assert expired["armed_until_utc"] is None


def test_disconnect_clears_preview_and_write_authority(tmp_path: Path) -> None:
    broker = MemoryBroker()
    broker.open_orders = (
        {
            "symbol": "AAPL",
            "side": "BUY",
            "quantity": "1",
            "status": "Submitted",
        },
    )
    desk = ExecutionDesk(
        tmp_path / "ibkr.sqlite3",
        broker,
        live_account="U7654321",
        clock=lambda: NOW,
    )
    connected = desk.handle({"kind": "connect"})
    desk.handle({"kind": "arm", "confirmation": connected["arm_confirmation_phrase"]})
    preview = desk.handle(
        {
            "kind": "preview",
            "order": {
                "client_order_id": "disconnect-1",
                "symbol": "MSFT",
                "security_type": "STK",
                "exchange": "SMART",
                "currency": "USD",
                "order_type": "LMT",
                "tif": "DAY",
                "action": "OpenLong",
                "quantity": 1,
                "limit_price": "200",
            },
        }
    )

    disconnected = desk.handle({"kind": "disconnect"})

    assert disconnected["enabled"] is False
    assert disconnected["connected"] is False
    assert disconnected["writes_armed"] is False
    assert disconnected["account_snapshot_stale"] is True
    assert disconnected["orders_left_working"] == 1
    assert len(disconnected["open_orders"]) == 1
    with pytest.raises(RuntimeError, match="not connected"):
        desk.handle(
            {
                "kind": "submit",
                "preview_id": preview["preview_id"],
                "confirmation": preview["confirmation_phrase"],
                "order": preview["intent"],
            }
        )


def test_broker_connection_loss_atomically_disarms_and_invalidates_preview(
    tmp_path: Path,
) -> None:
    broker = MemoryBroker()
    desk = ExecutionDesk(
        tmp_path / "ibkr.sqlite3",
        broker,
        live_account="U7654321",
        clock=lambda: NOW,
    )
    connected = desk.handle({"kind": "connect"})
    desk.handle({"kind": "arm", "confirmation": connected["arm_confirmation_phrase"]})
    broker.connection_lost = True

    lost = desk.snapshot()

    assert lost["enabled"] is False
    assert lost["connected"] is False
    assert lost["writes_armed"] is False
    assert lost["armed_until_utc"] is None
    assert lost["last_error"] == "connection_lost"
    with pytest.raises(RuntimeError, match="not connected"):
        desk.handle({"kind": "preview", "order": {}})


def test_threaded_http_commands_are_serialized_inside_the_execution_desk(
    tmp_path: Path,
) -> None:
    broker = ConcurrentProbeBroker()
    desk = ExecutionDesk(
        tmp_path / "ibkr.sqlite3",
        broker,
        live_account="U7654321",
        clock=lambda: NOW,
    )
    desk.handle({"kind": "connect"})
    base_order = {
        "symbol": "AAPL",
        "security_type": "STK",
        "exchange": "SMART",
        "currency": "USD",
        "order_type": "LMT",
        "tif": "DAY",
        "action": "OpenLong",
        "quantity": 1,
        "limit_price": "200",
    }
    start = threading.Barrier(3)

    def preview(client_order_id: str) -> dict[str, object]:
        start.wait()
        return desk.handle(
            {
                "kind": "preview",
                "order": {**base_order, "client_order_id": client_order_id},
            }
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(preview, "concurrent-1")
        second = pool.submit(preview, "concurrent-2")
        start.wait()
        assert first.result()["status"] == "previewed"
        assert second.result()["status"] == "previewed"

    assert broker.max_active_what_if == 1


def test_preview_accepts_only_bounded_us_stock_day_limits_and_runs_what_if(
    tmp_path: Path,
) -> None:
    broker = MemoryBroker()
    desk = ExecutionDesk(
        tmp_path / "ibkr.sqlite3",
        broker,
        live_account="U7654321",
        max_notional=Decimal("5000"),
        preview_ttl=timedelta(seconds=20),
        clock=lambda: NOW,
        token_factory=lambda: "ABC123",
    )
    desk.handle({"kind": "connect"})
    order = {
        "client_order_id": "entry-1",
        "symbol": "AAPL",
        "security_type": "STK",
        "exchange": "SMART",
        "currency": "USD",
        "order_type": "LMT",
        "tif": "DAY",
        "action": "OpenLong",
        "quantity": 10,
        "limit_price": "199.50",
    }

    preview = desk.handle({"type": "preview", "order": order})

    assert preview["status"] == "previewed"
    assert preview["schema_version"] == "ibkr.execution.v1"
    assert preview["kind"] == "execution_preview"
    assert preview["intent"]["notional"] == "1995.00"
    assert preview["what_if"]["estimated_commission"] == "1.25"
    assert preview["expires_at_utc"] == (NOW + timedelta(seconds=20)).isoformat()
    assert len(broker.what_if_requests) == 1

    for field, invalid in (
        ("security_type", "OPT"),
        ("exchange", "NYSE"),
        ("currency", "EUR"),
        ("order_type", "MKT"),
        ("tif", "GTC"),
        ("action", "SellShort"),
    ):
        bad = {**order, field: invalid, "client_order_id": f"bad-{field}"}
        with pytest.raises(ValueError):
            desk.handle({"type": "preview", "order": bad})

    with pytest.raises(ValueError, match="max_notional"):
        desk.handle(
            {
                "type": "preview",
                "order": {**order, "client_order_id": "too-large", "quantity": 100},
            }
        )


def test_preview_refreshes_account_and_blocks_a_second_active_buy_for_the_symbol(
    tmp_path: Path,
) -> None:
    broker = MemoryBroker()
    desk = ExecutionDesk(
        tmp_path / "ibkr.sqlite3",
        broker,
        live_account="U7654321",
        clock=lambda: NOW,
    )
    desk.handle({"kind": "connect"})
    broker.open_orders = (
        {
            "symbol": "AAPL",
            "side": "BUY",
            "quantity": "2",
            "status": "Submitted",
        },
    )

    with pytest.raises(ValueError, match="active BUY"):
        desk.handle(
            {
                "kind": "preview",
                "order": {
                    "client_order_id": "duplicate-buy",
                    "symbol": "AAPL",
                    "security_type": "STK",
                    "exchange": "SMART",
                    "currency": "USD",
                    "order_type": "LMT",
                    "tif": "DAY",
                    "action": "OpenLong",
                    "quantity": 1,
                    "limit_price": "200",
                },
            }
        )

    assert broker.account_snapshot_calls == 2


def test_reduce_long_cannot_sell_more_than_the_current_long_position_and_what_if_is_binding(
    tmp_path: Path,
) -> None:
    broker = MemoryBroker()
    broker.positions = ({"symbol": "AAPL", "quantity": 5},)
    desk = ExecutionDesk(
        tmp_path / "ibkr.sqlite3",
        broker,
        live_account="U7654321",
        clock=lambda: NOW,
    )
    desk.handle({"kind": "connect"})
    order = {
        "client_order_id": "reduce-1",
        "symbol": "AAPL",
        "security_type": "STK",
        "exchange": "SMART",
        "currency": "USD",
        "order_type": "LMT",
        "tif": "DAY",
        "action": "ReduceLong",
        "quantity": 6,
        "limit_price": "200",
    }

    with pytest.raises(ValueError, match="long position"):
        desk.handle({"type": "preview", "order": order})

    broker.open_orders = (
        {
            "symbol": "AAPL",
            "side": "SELL",
            "quantity": "3",
            "status": "Submitted",
        },
    )
    with pytest.raises(ValueError, match="long position"):
        desk.handle(
            {
                "kind": "preview",
                "order": {**order, "client_order_id": "reduce-open", "quantity": 3},
            }
        )
    broker.what_if_result = BrokerWhatIf(
        accepted=False,
        estimated_commission=None,
        initial_margin_change=None,
        warning="insufficient buying power",
    )
    with pytest.raises(RuntimeError, match="insufficient buying power"):
        desk.handle(
            {
                "type": "preview",
                "order": {
                    **order,
                    "client_order_id": "open-1",
                    "action": "OpenLong",
                    "quantity": 1,
                },
            }
        )


def test_max_notional_is_an_opening_cap_and_does_not_block_a_risk_reducing_sale(
    tmp_path: Path,
) -> None:
    broker = MemoryBroker()
    broker.positions = ({"symbol": "AAPL", "quantity": 100},)
    desk = ExecutionDesk(
        tmp_path / "ibkr.sqlite3",
        broker,
        live_account="U7654321",
        max_notional=Decimal("5000"),
        clock=lambda: NOW,
    )
    desk.handle({"kind": "connect"})

    preview = desk.handle(
        {
            "kind": "preview",
            "order": {
                "client_order_id": "large-reduction",
                "symbol": "AAPL",
                "security_type": "STK",
                "exchange": "SMART",
                "currency": "USD",
                "order_type": "LMT",
                "tif": "DAY",
                "action": "ReduceLong",
                "quantity": 100,
                "limit_price": "200",
            },
        }
    )

    assert preview["intent"]["notional"] == "20000.00"
    assert desk.snapshot()["notional_cap_scope"] == "OpenLong_only"


def test_submit_refreshes_and_revalidates_position_before_reducing_long(
    tmp_path: Path,
) -> None:
    broker = MemoryBroker()
    broker.positions = ({"symbol": "AAPL", "quantity": 5},)
    desk = ExecutionDesk(
        tmp_path / "ibkr.sqlite3",
        broker,
        live_account="U7654321",
        clock=lambda: NOW,
        token_factory=lambda: "REFRESH1",
    )
    connected = desk.handle({"kind": "connect"})
    desk.handle({"kind": "arm", "confirmation": connected["arm_confirmation_phrase"]})
    order = {
        "client_order_id": "reduce-stale",
        "symbol": "AAPL",
        "security_type": "STK",
        "exchange": "SMART",
        "currency": "USD",
        "order_type": "LMT",
        "tif": "DAY",
        "action": "ReduceLong",
        "quantity": 4,
        "limit_price": "200",
    }
    preview = desk.handle({"kind": "preview", "order": order})
    broker.positions = ({"symbol": "AAPL", "quantity": 3},)

    with pytest.raises(ValueError, match="long position"):
        desk.handle(
            {
                "kind": "submit",
                "preview_id": preview["preview_id"],
                "confirmation": preview["confirmation_phrase"],
                "order": order,
            }
        )

    assert broker.submissions == []
    assert broker.account_snapshot_calls == 3


def test_what_if_warning_is_bound_into_confirmation_and_change_requires_new_preview(
    tmp_path: Path,
) -> None:
    broker = MemoryBroker()
    broker.what_if_result = BrokerWhatIf(
        accepted=True,
        estimated_commission=Decimal("1"),
        initial_margin_change=Decimal("200"),
        warning="limit price is far from the market",
    )
    desk = ExecutionDesk(
        tmp_path / "ibkr.sqlite3",
        broker,
        live_account="U7654321",
        clock=lambda: NOW,
        token_factory=lambda: "WARN001",
    )
    connected = desk.handle({"kind": "connect"})
    desk.handle({"kind": "arm", "confirmation": connected["arm_confirmation_phrase"]})
    order = {
        "client_order_id": "warning-1",
        "symbol": "AAPL",
        "security_type": "STK",
        "exchange": "SMART",
        "currency": "USD",
        "order_type": "LMT",
        "tif": "DAY",
        "action": "OpenLong",
        "quantity": 1,
        "limit_price": "200",
    }
    preview = desk.handle({"kind": "preview", "order": order})

    assert len(preview["warning_confirmation_hash"]) == 8
    assert f"警告{preview['warning_confirmation_hash']}" in preview["confirmation_phrase"]
    broker.what_if_result = BrokerWhatIf(
        accepted=True,
        estimated_commission=Decimal("1"),
        initial_margin_change=Decimal("200"),
        warning="a different warning",
    )
    with pytest.raises(RuntimeError, match="warning changed"):
        desk.handle(
            {
                "kind": "submit",
                "preview_id": preview["preview_id"],
                "confirmation": preview["confirmation_phrase"],
                "order": order,
            }
        )

    assert broker.submissions == []


def test_live_submit_requires_an_armed_fresh_exact_preview_and_dynamic_confirmation(
    tmp_path: Path,
) -> None:
    clock = MutableClock(NOW)
    broker = MemoryBroker()
    desk = ExecutionDesk(
        tmp_path / "ibkr.sqlite3",
        broker,
        live_account="U7654321",
        preview_ttl=timedelta(seconds=10),
        clock=clock,
        token_factory=lambda: "9F3A11",
    )
    live = desk.handle({"kind": "connect"})
    desk.handle({"kind": "arm", "confirmation": live["arm_confirmation_phrase"]})
    order = {
        "client_order_id": "live-entry-1",
        "symbol": "NVDA",
        "security_type": "STK",
        "exchange": "SMART",
        "currency": "USD",
        "order_type": "LMT",
        "tif": "DAY",
        "action": "OpenLong",
        "quantity": 4,
        "limit_price": "180.25",
    }
    preview = desk.handle({"type": "preview", "order": order})

    with pytest.raises(RuntimeError, match="confirmation"):
        desk.handle(
            {
                "type": "submit",
                "preview_id": preview["preview_id"],
                "confirmation": "确认",
                "order": order,
            }
        )
    with pytest.raises(RuntimeError, match="exactly match"):
        desk.handle(
            {
                "type": "submit",
                "preview_id": preview["preview_id"],
                "confirmation": preview["confirmation_phrase"],
                "order": {**order, "quantity": 3},
            }
        )

    submitted = desk.handle(
        {
            "type": "submit",
            "preview_id": preview["preview_id"],
            "confirmation": preview["confirmation_phrase"],
            "order": order,
        }
    )
    assert submitted["status"] == "submitted"
    assert submitted["schema_version"] == "ibkr.execution.v1"
    assert submitted["kind"] == "execution_receipt"
    assert submitted["broker_order_id"] == 9001
    assert submitted["perm_id"] == 70001
    assert submitted["order_ref"].startswith("vq:live:U7654321:71:")
    assert len(broker.submissions) == 1
    status_after_submit = desk.snapshot()
    assert status_after_submit["account_refreshed_at_utc"] == NOW.isoformat()
    assert status_after_submit["recent_orders"][0] == {
        "client_order_id": "live-entry-1",
        "broker_order_id": 9001,
        "perm_id": 70001,
        "account_masked": "U***4321",
        "symbol": "NVDA",
        "action": "OpenLong",
        "quantity": 4,
        "limit_price": "180.25",
        "status": "submitted",
        "updated_at_utc": NOW.isoformat(),
    }
    assert status_after_submit["writes_armed"] is False

    expired = desk.handle({"type": "preview", "order": {**order, "client_order_id": "expired"}})
    desk.handle(
        {
            "kind": "arm",
            "confirmation": status_after_submit["arm_confirmation_phrase"],
        }
    )
    clock.value = NOW + timedelta(seconds=11)
    with pytest.raises(RuntimeError, match="expired"):
        desk.handle(
            {
                "type": "submit",
                "preview_id": expired["preview_id"],
                "confirmation": expired["confirmation_phrase"],
                "order": {**order, "client_order_id": "expired"},
            }
        )


def test_sqlite_idempotency_survives_restart_and_conflicting_intent_is_rejected(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ibkr.sqlite3"
    first_broker = MemoryBroker()
    first_broker.submission_status = "filled"
    first = ExecutionDesk(
        path,
        first_broker,
        live_account="U7654321",
        clock=lambda: NOW,
    )
    live = first.handle({"kind": "connect"})
    first.handle({"kind": "arm", "confirmation": live["arm_confirmation_phrase"]})
    order = {
        "client_order_id": "durable-1",
        "symbol": "AAPL",
        "security_type": "STK",
        "exchange": "SMART",
        "currency": "USD",
        "order_type": "LMT",
        "tif": "DAY",
        "action": "OpenLong",
        "quantity": 2,
        "limit_price": "200",
    }
    preview = first.handle({"type": "preview", "order": order})
    original = first.handle(
        {
            "kind": "submit",
            "preview_id": preview["preview_id"],
            "confirmation": preview["confirmation_phrase"],
            "order": order,
        }
    )

    restarted_broker = MemoryBroker()
    restarted = ExecutionDesk(
        path,
        restarted_broker,
        live_account="U7654321",
        clock=lambda: NOW,
    )
    live = restarted.handle({"kind": "connect"})
    restarted.handle({"kind": "arm", "confirmation": live["arm_confirmation_phrase"]})
    duplicate_preview = restarted.handle({"type": "preview", "order": order})
    duplicate = restarted.handle(
        {
            "kind": "submit",
            "preview_id": duplicate_preview["preview_id"],
            "confirmation": duplicate_preview["confirmation_phrase"],
            "order": order,
        }
    )

    assert duplicate == original
    assert restarted_broker.submissions == []

    conflict_preview = restarted.handle(
        {
            "type": "preview",
            "order": {**order, "quantity": 3},
        }
    )
    restarted.handle(
        {
            "kind": "arm",
            "confirmation": restarted.snapshot()["arm_confirmation_phrase"],
        }
    )
    with pytest.raises(RuntimeError, match="different intent"):
        restarted.handle(
            {
                "kind": "submit",
                "preview_id": conflict_preview["preview_id"],
                "confirmation": conflict_preview["confirmation_phrase"],
                "order": {**order, "quantity": 3},
            }
        )


def test_post_submit_snapshot_timeout_preserves_the_durable_broker_receipt(
    tmp_path: Path,
) -> None:
    broker = MemoryBroker()
    broker.fail_account_snapshot_on_call = 4
    desk = ExecutionDesk(
        tmp_path / "ibkr.sqlite3",
        broker,
        live_account="U7654321",
        clock=lambda: NOW,
    )
    connected = desk.handle({"kind": "connect"})
    desk.handle({"kind": "arm", "confirmation": connected["arm_confirmation_phrase"]})
    order = {
        "client_order_id": "refresh-timeout-1",
        "symbol": "AAPL",
        "security_type": "STK",
        "exchange": "SMART",
        "currency": "USD",
        "order_type": "LMT",
        "tif": "DAY",
        "action": "OpenLong",
        "quantity": 1,
        "limit_price": "200",
    }
    preview = desk.handle({"kind": "preview", "order": order})

    receipt = desk.handle(
        {
            "kind": "submit",
            "preview_id": preview["preview_id"],
            "confirmation": preview["confirmation_phrase"],
            "order": order,
        }
    )

    assert receipt["status"] == "submitted"
    assert receipt["post_submit_snapshot_refreshed"] is False
    assert receipt["snapshot_refresh_error"] == "account_refresh_timeout"
    assert desk.snapshot()["recent_orders"][0]["client_order_id"] == "refresh-timeout-1"


def test_deterministic_broker_rejection_is_not_left_as_uncertain(tmp_path: Path) -> None:
    broker = MemoryBroker()
    broker.submit_error = BrokerOrderRejected("ibkr_error_201")
    desk = ExecutionDesk(
        tmp_path / "ibkr.sqlite3",
        broker,
        live_account="U7654321",
        clock=lambda: NOW,
    )
    connected = desk.handle({"kind": "connect"})
    desk.handle({"kind": "arm", "confirmation": connected["arm_confirmation_phrase"]})
    order = {
        "client_order_id": "rejected-1",
        "symbol": "AAPL",
        "security_type": "STK",
        "exchange": "SMART",
        "currency": "USD",
        "order_type": "LMT",
        "tif": "DAY",
        "action": "OpenLong",
        "quantity": 1,
        "limit_price": "200",
    }
    preview = desk.handle({"kind": "preview", "order": order})

    receipt = desk.handle(
        {
            "kind": "submit",
            "preview_id": preview["preview_id"],
            "confirmation": preview["confirmation_phrase"],
            "order": order,
        }
    )

    assert receipt["status"] == "rejected"
    assert receipt["last_error_code"] == "ibkr_error_201"
    assert desk.snapshot()["recovery_required"] is False


def test_broker_timeout_is_durable_unknown_and_is_never_blindly_resubmitted(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ibkr.sqlite3"
    broker = MemoryBroker()
    broker.submit_error = TimeoutError("gateway did not acknowledge")
    desk = ExecutionDesk(
        path,
        broker,
        live_account="U7654321",
        clock=lambda: NOW,
    )
    live = desk.handle({"kind": "connect"})
    desk.handle({"kind": "arm", "confirmation": live["arm_confirmation_phrase"]})
    order = {
        "client_order_id": "uncertain-1",
        "symbol": "MSFT",
        "security_type": "STK",
        "exchange": "SMART",
        "currency": "USD",
        "order_type": "LMT",
        "tif": "DAY",
        "action": "OpenLong",
        "quantity": 1,
        "limit_price": "420",
    }
    preview = desk.handle({"type": "preview", "order": order})
    unknown = desk.handle(
        {
            "kind": "submit",
            "preview_id": preview["preview_id"],
            "confirmation": preview["confirmation_phrase"],
            "order": order,
        }
    )

    assert unknown["status"] == "unknown"
    assert desk.snapshot()["recovery_required"] is True
    assert len(broker.submissions) == 1

    with pytest.raises(RuntimeError, match="recovery_required"):
        desk.handle({"kind": "preview", "order": order})

    broker.orders_by_ref[unknown["order_ref"]] = BrokerSubmission(
        status="filled",
        order_id=9001,
        perm_id=70001,
        order_ref=unknown["order_ref"],
    )
    recovered = desk.handle({"kind": "recover"})

    assert recovered["recovery_required"] is False
    assert recovered["recent_orders"][0]["status"] == "filled"
    assert len(broker.submissions) == 1
    assert len(broker.lookup_refs) == 1
