from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

import scripts.serve_macos_research_runtime as runtime_server
from execution.ibkr_execution import (
    BrokerAccountSnapshot,
    BrokerOrderRequest,
    BrokerSubmission,
    BrokerWhatIf,
)
from operations.local_research_runtime import DisabledExecutionDesk
from scripts.serve_macos_research_runtime import _build_execution_desk


class _RecordingBroker:
    def __init__(self) -> None:
        self.connect_args: tuple[str, int, int] | None = None

    @property
    def connected(self) -> bool:
        return self.connect_args is not None

    def connect(self, *, host: str, port: int, client_id: int) -> None:
        self.connect_args = (host, port, client_id)

    def disconnect(self) -> None:
        self.connect_args = None

    def account_snapshot(self) -> BrokerAccountSnapshot:
        return BrokerAccountSnapshot(
            account_id="U1234567",
            api_read_only=False,
            positions=(),
            open_orders=(),
        )

    def what_if(self, request: BrokerOrderRequest) -> BrokerWhatIf:
        del request
        return BrokerWhatIf(True, Decimal("1"), Decimal("100"), None)

    def submit(
        self, request: BrokerOrderRequest, *, order_ref: str
    ) -> BrokerSubmission:
        del request
        return BrokerSubmission("submitted", 1, 2, order_ref)

    def find_by_order_ref(self, order_ref: str) -> BrokerSubmission | None:
        del order_ref
        return None


def test_incomplete_ibkr_configuration_stays_disabled(tmp_path: Path) -> None:
    desk = _build_execution_desk(
        environ={"IBKR_HOST": "192.0.2.10"},
        runs_root=tmp_path,
    )

    assert isinstance(desk, DisabledExecutionDesk)
    snapshot = desk.snapshot()
    assert snapshot["mode"] == "live"
    assert snapshot["port"] == 4001
    assert snapshot["enabled"] is False
    assert snapshot["writes_armed"] is False


def test_configured_desk_is_live_only_and_starts_disconnected(tmp_path: Path) -> None:
    broker = _RecordingBroker()
    desk = _build_execution_desk(
        environ={
            "IBKR_HOST": "192.0.2.10",
            "IBKR_CLIENT_ID": "87",
            "IBKR_LIVE_ACCOUNT": "U1234567",
            "IBKR_MAX_ORDER_NOTIONAL": "25000",
        },
        runs_root=tmp_path,
        broker_factory=lambda: broker,
    )

    initial = desk.snapshot()
    assert initial["mode"] == "live"
    assert initial["port"] == 4001
    assert initial["connected"] is False
    assert initial["writes_armed"] is False

    connected = desk.handle({"kind": "connect"})

    assert broker.connect_args == ("192.0.2.10", 4001, 87)
    assert connected["connected"] is True
    assert connected["account_masked"] == "U***4567"
    assert connected["writes_armed"] is False


def test_first_connection_detects_but_cannot_trade_an_unbound_account(
    tmp_path: Path,
) -> None:
    broker = _RecordingBroker()
    desk = _build_execution_desk(
        environ={
            "IBKR_HOST": "192.0.2.10",
            "IBKR_CLIENT_ID": "87",
            "IBKR_MAX_ORDER_NOTIONAL": "25000",
        },
        runs_root=tmp_path,
        broker_factory=lambda: broker,
    )

    connected = desk.handle({"kind": "connect"})

    assert connected["connected"] is True
    assert connected["account_bound"] is False
    assert connected["binding_confirmation_phrase"]
    with pytest.raises(RuntimeError, match="bound"):
        desk.handle(
            {
                "kind": "arm",
                "confirmation": connected["arm_confirmation_phrase"],
            }
        )


def test_ibkr_runtime_configuration_rejects_invalid_risk_values(
    tmp_path: Path,
) -> None:
    base = {
        "IBKR_HOST": "192.0.2.10",
        "IBKR_CLIENT_ID": "87",
        "IBKR_LIVE_ACCOUNT": "U1234567",
        "IBKR_MAX_ORDER_NOTIONAL": "25000",
    }
    for field, value in (
        ("IBKR_CLIENT_ID", "-1"),
        ("IBKR_MAX_ORDER_NOTIONAL", "0"),
        ("IBKR_LIVE_ACCOUNT", "bad account"),
    ):
        candidate = {**base, field: value}
        try:
            _build_execution_desk(environ=candidate, runs_root=tmp_path)
        except ValueError:
            continue
        raise AssertionError(f"invalid {field} should fail closed")


def test_bound_account_is_forwarded_to_the_official_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}
    broker = _RecordingBroker()

    def build_adapter(**kwargs: object) -> _RecordingBroker:
        captured.update(kwargs)
        return broker

    monkeypatch.setattr(runtime_server, "OfficialIbapiAdapter", build_adapter)
    runtime_server._build_execution_desk(
        environ={
            "IBKR_HOST": "192.0.2.10",
            "IBKR_CLIENT_ID": "87",
            "IBKR_LIVE_ACCOUNT": "U1234567",
            "IBKR_MAX_ORDER_NOTIONAL": "25000",
        },
        runs_root=tmp_path,
    )

    assert captured == {
        "api_read_only": False,
        "expected_account_id": "U1234567",
    }
