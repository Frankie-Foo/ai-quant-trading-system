from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from data_plane.storage import persist_snapshot
from execution.alpaca_paper import (
    BrokerOrder,
    PaperAccount,
    PaperCloseRequest,
    PaperExtendedLimitRequest,
    PaperOrderRequest,
    PaperPosition,
    PaperStopRequest,
)
from operations.autonomous_policy_adapter import (
    RuntimeSafetyEnvelope,
    write_runtime_safety_envelope,
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


class _SafetyRefresher:
    def __init__(self) -> None:
        self.calls = 0

    def refresh(
        self,
        *,
        bundles: object,
        broker: Any,
        observed_at_utc: datetime,
    ) -> dict[str, object]:
        del bundles, observed_at_utc
        self.calls += 1
        assert broker.get_account().status == "ACTIVE"
        return {
            "plans": 1,
            "healthy_envelopes": 1,
            "input_errors": 0,
            "push_healthy": True,
        }


def _write_current_paper_plan(runs_root: Path, now: datetime) -> None:
    plan_dir = runs_root / "paper-autopilot"
    safety_path = plan_dir / "AAPL-safety.json"
    plan_dir.mkdir(parents=True)
    write_runtime_safety_envelope(
        safety_path,
        RuntimeSafetyEnvelope(
            trade_date=now.date(),
            symbol="AAPL",
            generated_at_utc=now - timedelta(minutes=1),
            expires_at_utc=now + timedelta(minutes=1),
            negative_news_clear=True,
            material_negative=False,
            agents_healthy=True,
            push_healthy=True,
            source_snapshot_ids=("selection-20260803",),
            provenance="test.paper-safety.v1",
        ),
    )
    payload = {
        "schema_version": "autonomous_paper_config.v1",
        "poll_seconds": 1,
        "plans": [
            {
                "plan": {
                    "plan_id": "auto-20260803-AAPL",
                    "symbol": "AAPL",
                    "trade_date": now.date().isoformat(),
                    "reference_price": "100.00",
                    "hard_stop": "98.00",
                    "max_notional_fraction": "0.20",
                    "full_risk_fraction": "0.0035",
                    "max_spread_ratio": "0.0025",
                    "source_snapshot_ids": ["selection-20260803"],
                    "provenance": "test.paper-plan.v1",
                },
                "policy_evidence": {
                    "route": "catalyst",
                    "catalyst": {
                        "value": 90.0,
                        "asof_utc": (now - timedelta(minutes=2)).isoformat(),
                        "provenance": "test.catalyst.v1",
                    },
                    "factor": {
                        "value": 80.0,
                        "asof_utc": (now - timedelta(minutes=2)).isoformat(),
                        "provenance": "test.factor.v1",
                    },
                    "right_tail": {
                        "value": 70.0,
                        "asof_utc": (now - timedelta(minutes=2)).isoformat(),
                        "provenance": "test.tail.v1",
                    },
                    "first_target_reward_r": 2.5,
                    "weighted_expected_reward_r": 3.0,
                    "reward_risk_provenance": "test.risk-reward.v1",
                    "a_plus_plus_approved": False,
                },
                "market_context": {
                    "benchmark_symbol": "SPY",
                    "sector_symbol": "XLK",
                    "provenance": "test.market-context.v1",
                },
                "safety_envelope": "AAPL-safety.json",
            }
        ],
    }
    (plan_dir / "approved-autonomous-paper.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


def _write_selection(data_root: Path, trade_date: datetime) -> None:
    persist_snapshot(
        pl.DataFrame(
            {
                "symbol": ["AAPL"],
                "session_date": [trade_date.date()],
                "selection_rank": [1],
                "pass_gate": [True],
                "rvol": [8.0],
                "price": [100.0],
                "premarket_close": [102.0],
                "premarket_above_vwap": [True],
                "directional_volume_confirmed": [True],
                "earnings_intensity_score": [80.0],
                "gate_asof_utc": [trade_date - timedelta(minutes=2)],
            }
        ),
        root=data_root,
        source="kernel.universe.selection_gates",
        schema_version="selection_gates.v2",
        checks=(),
    )


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
    assert connected["audit_event_count"] == 1
    audit = autopilot.audit_history()
    assert [event["event_type"] for event in audit] == ["autopilot_connected"]
    payload = audit[0]["payload"]
    assert isinstance(payload, dict)
    account = payload["account"]
    assert isinstance(account, dict)
    assert account["equity"] == "100000"
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


def test_paper_autopilot_prepares_auditable_plan_from_current_selection(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 3, 14, 0, tzinfo=UTC)
    _write_selection(tmp_path / "data", now)
    autopilot = PaperAutopilot(
        data_root=tmp_path / "data",
        runs_root=tmp_path / "runs",
        environ={},
        now=lambda: now,
    )

    prepared = autopilot.handle({"kind": "prepare_plan"})

    assert prepared["running"] is False
    assert prepared["plan_status"] == "prepared"
    assert prepared["plan_error"] == "paper_safety_envelope_missing"
    assert prepared["plan_symbol"] == "AAPL"
    events = autopilot.audit_history()
    assert [event["event_type"] for event in events] == [
        "autopilot_plan_prepared"
    ]
    payload = events[0]["payload"]
    assert isinstance(payload, dict)
    assert payload["symbol"] == "AAPL"
    assert (tmp_path / "runs" / "paper-autopilot" / "approved-autonomous-paper.json").is_file()


def test_paper_autopilot_requires_write_gate_and_released_kill_switch(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 3, 14, 0, tzinfo=UTC)
    _write_current_paper_plan(tmp_path / "runs", now)
    autopilot = PaperAutopilot(
        data_root=tmp_path / "data",
        runs_root=tmp_path / "runs",
        environ={
            "IBKR_PAPER_HOST": "192.0.2.44",
            "IBKR_PAPER_CLIENT_ID": "91",
            "IBKR_PAPER_ACCOUNT": "DU7654321",
        },
        broker_factory=lambda writes_enabled, account: _Broker(writes_enabled, account),
        now=lambda: now,
    )
    autopilot.handle({"kind": "connect"})

    with pytest.raises(RuntimeError, match="BROKER_WRITE_ENABLED"):
        autopilot.start(confirmation=str(autopilot.snapshot()["arm_confirmation_phrase"]))


def test_paper_autopilot_refreshes_safety_only_after_read_only_paper_connection(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 3, 14, 0, tzinfo=UTC)
    _write_current_paper_plan(tmp_path / "runs", now)
    safety = _SafetyRefresher()
    autopilot = PaperAutopilot(
        data_root=tmp_path / "data",
        runs_root=tmp_path / "runs",
        environ={
            "IBKR_PAPER_HOST": "192.0.2.44",
            "IBKR_PAPER_CLIENT_ID": "91",
            "IBKR_PAPER_ACCOUNT": "DU7654321",
        },
        broker_factory=lambda writes_enabled, account: _Broker(writes_enabled, account),
        safety_refresher=safety,
        now=lambda: now,
    )

    with pytest.raises(RuntimeError, match="paper_not_connected"):
        autopilot.handle({"kind": "refresh_safety"})
    autopilot.handle({"kind": "connect"})
    refreshed = autopilot.handle({"kind": "refresh_safety"})

    assert safety.calls == 1
    assert refreshed["plan_status"] == "valid"
    assert refreshed["safety_error"] == ""
    events = [event["event_type"] for event in autopilot.audit_history()]
    assert "safety_refresh_requested" in events
    assert "broker_account_read" in events
    assert "safety_refresh_result" in events


def test_paper_autopilot_persists_every_tick_boundary_before_execution(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 3, 14, 0, tzinfo=UTC)
    _write_current_paper_plan(tmp_path / "runs", now)
    autopilot = PaperAutopilot(
        data_root=tmp_path / "data",
        runs_root=tmp_path / "runs",
        environ={
            "IBKR_PAPER_HOST": "192.0.2.44",
            "IBKR_PAPER_CLIENT_ID": "91",
            "IBKR_PAPER_ACCOUNT": "DU7654321",
            "BROKER_WRITE_ENABLED": "true",
            "TRADING_KILL_SWITCH": "false",
        },
        broker_factory=lambda writes_enabled, account: _Broker(writes_enabled, account),
        now=lambda: now,
    )

    autopilot.start(confirmation=str(autopilot.snapshot()["arm_confirmation_phrase"]))
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        event_types = [event["event_type"] for event in autopilot.audit_history()]
        if "tick_open" in event_types and "tick_result" in event_types:
            break
        time.sleep(0.02)
    autopilot.stop()

    events = autopilot.audit_history()
    event_types = [event["event_type"] for event in events]
    assert "autopilot_plan_validated" in event_types
    assert "autopilot_started" in event_types
    assert "tick_open" in event_types
    assert "broker_positions_read" in event_types
    assert "broker_account_read" in event_types
    assert "market_facts_read_requested" in event_types
    assert "market_facts_read_failed" in event_types
    assert "tick_result" in event_types
    assert events[-1]["event_hash"]
