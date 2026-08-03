from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from execution.alpaca_paper import (
    BrokerOrder,
    PaperAccount,
    PaperCloseRequest,
    PaperExtendedLimitRequest,
    PaperOrderRequest,
    PaperPosition,
    PaperStopRequest,
)
from operations.ibkr_paper_autopilot import PaperAutopilot


class _Broker:
    def __init__(self, writes_enabled: bool, account: str) -> None:
        self.writes_enabled = writes_enabled
        self.account = account
        self.connect_calls: list[tuple[str, int]] = []
        self.closed = False

    def connect(self, *, host: str, client_id: int) -> None:
        self.connect_calls.append((host, client_id))

    def close(self) -> None:
        self.closed = True

    def get_account(self) -> PaperAccount:
        return PaperAccount(
            status="ACTIVE",
            account_blocked=False,
            trading_blocked=False,
            equity="100000",
            last_equity="100000",
            buying_power="100000",
        )

    def list_positions(self) -> tuple[PaperPosition, ...]:
        return ()

    def list_open_orders(self) -> tuple[BrokerOrder, ...]:
        return ()

    def get_order_by_client_id(self, _client_order_id: str) -> BrokerOrder | None:
        return None

    def cancel_order(self, _order_id: str) -> bool:
        return False

    def submit_close_order_idempotent(
        self, _request: PaperCloseRequest
    ) -> BrokerOrder:
        raise AssertionError("must not submit")

    def submit_order_idempotent(self, _request: PaperOrderRequest) -> BrokerOrder:
        raise AssertionError("must not submit")

    def submit_stop_order_idempotent(self, _request: PaperStopRequest) -> BrokerOrder:
        raise AssertionError("must not submit")

    def submit_extended_limit_idempotent(
        self, _request: PaperExtendedLimitRequest
    ) -> BrokerOrder:
        raise AssertionError("must not submit")


def test_paper_autopilot_connects_only_with_a_paper_profile_and_never_arms_without_a_plan(
    tmp_path: Path,
) -> None:
    created: list[_Broker] = []

    def broker_factory(writes_enabled: bool, account: str) -> _Broker:
        broker = _Broker(writes_enabled, account)
        created.append(broker)
        return broker

    autopilot = PaperAutopilot(
        data_root=tmp_path / "data",
        runs_root=tmp_path / "runs",
        environ={
            "IBKR_PAPER_HOST": "192.0.2.44",
            "IBKR_PAPER_CLIENT_ID": "91",
            "IBKR_PAPER_ACCOUNT": "DU7654321",
        },
        broker_factory=broker_factory,
        now=lambda: datetime(2026, 8, 3, 14, 0, tzinfo=UTC),
    )

    connected = autopilot.handle({"kind": "connect"})

    assert connected["port"] == 4002
    assert connected["connected"] is True
    assert connected["paper_writes_armed"] is False
    assert connected["account_masked"] == "DU***4321"
    assert created[0].writes_enabled is False
    assert created[0].connect_calls == [("192.0.2.44", 91)]
    with pytest.raises(RuntimeError, match="paper_plan_invalid"):
        autopilot.handle(
            {
                "kind": "start",
                "confirmation": "启用模拟盘自动执行 DU***4321",
            }
        )
    assert len(created) == 1
    assert autopilot.snapshot()["running"] is False


def test_paper_autopilot_rejects_non_du_accounts_before_any_connection(
    tmp_path: Path,
) -> None:
    called = False

    def broker_factory(_writes_enabled: bool, _account: str) -> _Broker:
        nonlocal called
        called = True
        raise AssertionError("broker must not be built")

    autopilot = PaperAutopilot(
        data_root=tmp_path / "data",
        runs_root=tmp_path / "runs",
        environ={
            "IBKR_PAPER_HOST": "192.0.2.44",
            "IBKR_PAPER_CLIENT_ID": "91",
            "IBKR_PAPER_ACCOUNT": "U7654321",
        },
        broker_factory=broker_factory,
    )

    with pytest.raises(ValueError, match="paper_profile_invalid"):
        autopilot.handle({"kind": "connect"})
    assert called is False
