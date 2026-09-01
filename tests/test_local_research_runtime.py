from __future__ import annotations

import json
import threading
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from operations.client_desk import market_phase
from operations.desktop_workflows import DesktopWorkflowManager
from operations.local_research_http import build_local_research_http_server
from operations.local_research_runtime import (
    AlpacaProxyMarketDataAdapter,
    EnvironmentMarketDataAdapter,
    LocalResearchRuntime,
    ScheduledResearchPipeline,
    StandaloneMarketDataAdapter,
    UnconfiguredMarketDataAdapter,
)


class _RecordingPipeline:
    def __init__(self) -> None:
        self.calls = 0

    def run_due(
        self,
        *,
        now_utc: datetime,
        data_root: Path,
        runs_root: Path,
    ) -> dict[str, object]:
        self.calls += 1
        return {
            "status": "complete",
            "observed_at_utc": now_utc.isoformat(),
            "data_root": str(data_root),
            "runs_root": str(runs_root),
            "orders_submitted": 0,
        }


class _RecordingExecutionDesk:
    def __init__(self) -> None:
        self.commands: list[dict[str, object]] = []

    def snapshot(self) -> dict[str, object]:
        return {
            "schema_version": "execution_desk.v1",
            "mode": "live",
            "enabled": False,
            "port": 4001,
            "connected": False,
            "writes_armed": False,
            "orders_submitted": 0,
        }

    def handle(self, command: dict[str, object]) -> dict[str, object]:
        self.commands.append(dict(command))
        if command.get("kind") == "fail_connection":
            raise ConnectionError(
                "broker connection failed for a value that must stay hidden"
            )
        return {
            "schema_version": "execution_receipt.v1",
            "kind": str(command.get("kind") or ""),
            "accepted": True,
            "orders_submitted": 0,
        }


class _RecordingPaperAutopilot:
    def __init__(self) -> None:
        self.commands: list[dict[str, object]] = []

    def snapshot(self) -> dict[str, object]:
        return {
            "schema_version": "ibkr.paper_autopilot.v1",
            "mode": "paper",
            "port": 4002,
            "configured": True,
            "connected": False,
            "running": False,
            "paper_writes_armed": False,
            "root_research_orders_authorized": False,
        }

    def handle(self, command: dict[str, object]) -> dict[str, object]:
        self.commands.append(dict(command))
        return {**self.snapshot(), "connected": command.get("kind") == "connect"}


class _ReadyWorkflows:
    def __init__(self) -> None:
        self.submissions: list[tuple[str, date]] = []

    def status(self) -> dict[str, object]:
        return {}

    def data_inventory(self) -> dict[str, object]:
        return {"ready_for_selection": True}

    def submit(self, action: str, *, trade_date: date) -> dict[str, object]:
        self.submissions.append((action, trade_date))
        return {"accepted": True, "orders_submitted": 0}


def test_unconfigured_local_runtime_is_honest_and_fail_closed(
    tmp_path: Path,
) -> None:
    pipeline = _RecordingPipeline()
    runtime = LocalResearchRuntime(
        data_root=tmp_path / "data",
        runs_root=tmp_path / "runs",
        market_data=UnconfiguredMarketDataAdapter(),
        pipeline=pipeline,
    )
    observed_at = datetime(2026, 7, 31, 0, 0, tzinfo=UTC)

    status = runtime.status(observed_at)
    desk = runtime.snapshot(observed_at)
    tick = runtime.run_due(observed_at)

    assert status["schema_version"] == "macos_local_research_runtime.v1"
    assert status["execution_mode"] == "local_research"
    assert status["local_execution"] is True
    assert status["orders_authorized"] is False
    market_data = status["market_data"]
    assert isinstance(market_data, dict)
    assert market_data["provider_id"] == "unconfigured"
    assert market_data["configured"] is False
    assert desk["runtime"] == status
    assert desk["pipeline_status"] == "blocked"
    selection = desk["selection"]
    assert isinstance(selection, dict)
    assert selection["status"] == "blocked"
    assert selection["blocker"] == "market_data_provider_unconfigured"
    assert desk["orders_authorized"] is False
    assert tick["status"] == "blocked"
    assert tick["reason"] == "market_data_provider_unconfigured"
    assert tick["orders_submitted"] == 0
    assert pipeline.calls == 0


def test_environment_adapter_is_a_real_second_adapter_and_never_exposes_values() -> None:
    missing = EnvironmentMarketDataAdapter(environ={}).status()
    configured = EnvironmentMarketDataAdapter(
        environ={
            "MASSIVE_API_KEY": "massive-secret",
            "CLOUD_PLATFORM_BASE_URL": "https://market.example.com",
            "CLOUD_MARKET_DATA_API_TOKEN": "market-secret",
            "SEC_USER_AGENT": "Research User research@example.com",
        }
    ).status()

    assert missing["provider_id"] == "environment"
    assert missing["configured"] is False
    requirements = missing["missing_requirements"]
    assert isinstance(requirements, list)
    assert set(requirements) == {
        "CLOUD_MARKET_DATA_API_TOKEN",
        "CLOUD_PLATFORM_BASE_URL",
        "MASSIVE_API_KEY",
        "SEC_USER_AGENT",
    }
    assert configured["configured"] is True
    assert configured["healthy"] is True
    serialized = repr(configured)
    assert "massive-secret" not in serialized
    assert "market-secret" not in serialized


def test_alpaca_proxy_adapter_reports_realtime_ready_without_unlocking_research() -> None:
    adapter = AlpacaProxyMarketDataAdapter(
        environ={
            "ALPACA_PROXY_KEY": "market-key",
            "ALPACA_PROXY_SECRET": "market-secret",
        },
        probe=lambda **_kwargs: {
            "healthy": True,
            "reason": None,
            "endpoint_host": "alpaca-trade-api.vertu.cn",
            "capabilities": ["bars", "quotes", "trades"],
        },
    )

    status = adapter.status()

    assert status == {
        "schema_version": "macos_market_data_adapter.v1",
        "provider_id": "alpaca_proxy_sip",
        "configured": True,
        "healthy": True,
        "reason": "historical_research_inputs_missing",
        "missing_requirements": [],
        "can_run_pipeline": False,
        "realtime_ready": True,
        "research_inputs_ready": False,
        "endpoint_host": "alpaca-trade-api.vertu.cn",
        "capabilities": ["bars", "quotes", "trades"],
    }
    assert "market-key" not in repr(status)
    assert "market-secret" not in repr(status)


def test_alpaca_proxy_adapter_fails_closed_without_credentials() -> None:
    status = AlpacaProxyMarketDataAdapter(environ={}).status()

    assert status["configured"] is False
    assert status["healthy"] is False
    assert status["reason"] == "credentials_missing"
    assert status["missing_requirements"] == [
        "ALPACA_PROXY_KEY",
        "ALPACA_PROXY_SECRET",
    ]
    assert status["can_run_pipeline"] is False


def test_standalone_adapter_unlocks_research_only_when_all_sources_are_ready() -> None:
    status = StandaloneMarketDataAdapter(
        environ={
            "ALPACA_PROXY_KEY": "market-key",
            "ALPACA_PROXY_SECRET": "market-secret",
            "MASSIVE_API_KEY": "massive-key",
            "SEC_USER_AGENT": "Research User research@example.com",
        },
        probe=lambda **_kwargs: {
            "healthy": True,
            "reason": None,
            "endpoint_host": "alpaca-trade-api.vertu.cn",
            "capabilities": ["bars", "quotes", "trades"],
        },
    ).status()

    assert status["provider_id"] == "standalone_massive_alpaca"
    assert status["configured"] is True
    assert status["healthy"] is True
    assert status["can_run_pipeline"] is True
    assert status["research_inputs_ready"] is True
    assert status["realtime_ready"] is True
    assert "massive-key" not in repr(status)
    assert "market-secret" not in repr(status)


def test_configured_local_runtime_delegates_once_to_pipeline(
    tmp_path: Path,
) -> None:
    pipeline = _RecordingPipeline()
    runtime = LocalResearchRuntime(
        data_root=tmp_path / "data",
        runs_root=tmp_path / "runs",
        market_data=EnvironmentMarketDataAdapter(
            environ={
                "MASSIVE_API_KEY": "configured",
                "CLOUD_PLATFORM_BASE_URL": "https://market.example.com",
                "CLOUD_MARKET_DATA_API_TOKEN": "configured",
                "SEC_USER_AGENT": "Research User research@example.com",
            }
        ),
        pipeline=pipeline,
    )
    observed_at = datetime(2026, 7, 31, 0, 0, tzinfo=UTC)

    result = runtime.run_due(observed_at)

    assert result["status"] == "complete"
    assert result["orders_submitted"] == 0
    assert pipeline.calls == 1


def test_market_phase_separates_selection_review_and_waiting() -> None:
    selection = market_phase(datetime(2026, 8, 3, 12, 5, tzinfo=UTC))
    review = market_phase(datetime(2026, 7, 31, 20, 21, tzinfo=UTC))
    waiting = market_phase(datetime(2026, 8, 3, 11, 30, tzinfo=UTC))

    assert selection["kind"] == "selection"
    assert selection["trade_date"] == "2026-08-03"
    assert review["kind"] == "post_close_review"
    assert review["trade_date"] == "2026-07-31"
    assert waiting["kind"] == "waiting"


def test_scheduler_runs_only_one_exchange_clock_stage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import schedule.postmarket as postmarket
    import schedule.premarket as premarket

    calls: list[tuple[str, list[str]]] = []

    def run_selection(argv: list[str], *, now_utc: datetime) -> int:
        del now_utc
        calls.append(("selection", argv))
        return 0

    def run_review(argv: list[str]) -> int:
        calls.append(("review", argv))
        return 0

    monkeypatch.setattr(
        premarket,
        "run",
        run_selection,
    )
    monkeypatch.setattr(
        postmarket,
        "run",
        run_review,
    )
    pipeline = ScheduledResearchPipeline()

    selection = pipeline.run_due(
        now_utc=datetime(2026, 8, 3, 12, 5, tzinfo=UTC),
        data_root=tmp_path / "data",
        runs_root=tmp_path / "runs",
    )
    review = pipeline.run_due(
        now_utc=datetime(2026, 7, 31, 20, 21, tzinfo=UTC),
        data_root=tmp_path / "data",
        runs_root=tmp_path / "runs",
    )

    selection_phase = selection["market_phase"]
    assert isinstance(selection_phase, dict)
    assert selection_phase["kind"] == "selection"
    assert selection["premarket_exit_code"] == 0
    assert selection["postmarket_exit_code"] is None
    review_phase = review["market_phase"]
    assert isinstance(review_phase, dict)
    assert review_phase["kind"] == "post_close_review"
    assert review["premarket_exit_code"] is None
    assert review["postmarket_exit_code"] == 0
    assert [name for name, _ in calls] == ["selection", "review"]
    assert "2026-07-31" in calls[-1][1]


def test_runtime_rejects_selection_outside_its_exchange_window(tmp_path: Path) -> None:
    workflows = _ReadyWorkflows()
    runtime = LocalResearchRuntime(
        data_root=tmp_path / "data",
        runs_root=tmp_path / "runs",
        market_data=EnvironmentMarketDataAdapter(
            environ={
                "MASSIVE_API_KEY": "configured",
                "CLOUD_PLATFORM_BASE_URL": "https://market.example.com",
                "CLOUD_MARKET_DATA_API_TOKEN": "configured",
                "SEC_USER_AGENT": "Research User research@example.com",
            }
        ),
        workflows=cast(DesktopWorkflowManager, workflows),
    )

    blocked = runtime.submit_workflow(
        "run_today",
        date(2026, 8, 3),
        datetime(2026, 8, 3, 11, 30, tzinfo=UTC),
    )
    accepted = runtime.submit_workflow(
        "run_today",
        date(2026, 8, 3),
        datetime(2026, 8, 3, 12, 5, tzinfo=UTC),
    )

    assert blocked["accepted"] is False
    assert blocked["reason"] == "selection_not_open"
    assert accepted["accepted"] is True
    assert workflows.submissions == [("run_today", date(2026, 8, 3))]


def _http_json(
    url: str,
    *,
    token: str | None = None,
    method: str = "GET",
    payload: dict[str, object] | None = None,
) -> tuple[int, dict[str, object]]:
    headers = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    body = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode("utf-8")
    request = Request(url, headers=headers, data=body, method=method)
    try:
        with urlopen(request, timeout=5) as response:  # noqa: S310
            return response.status, json.loads(response.read())
    except HTTPError as error:
        return error.code, json.loads(error.read())


def test_local_runtime_http_requires_ephemeral_auth_and_exposes_no_order_route(
    tmp_path: Path,
) -> None:
    runtime = LocalResearchRuntime(
        data_root=tmp_path / "data",
        runs_root=tmp_path / "runs",
        market_data=UnconfiguredMarketDataAdapter(),
        pipeline=_RecordingPipeline(),
    )
    token = "ephemeral-local-runtime-token-1234"
    server = build_local_research_http_server(
        runtime,
        host="127.0.0.1",
        port=0,
        bearer_token=token,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        missing_status, missing = _http_json(f"{base}/v1/health")
        health_status, health = _http_json(
            f"{base}/v1/health",
            token=token,
        )
        desk_status, desk = _http_json(f"{base}/v1/desk", token=token)
        tick_status, tick = _http_json(
            f"{base}/v1/run-due",
            token=token,
            method="POST",
        )
        order_status, order = _http_json(
            f"{base}/v1/orders",
            token=token,
            method="POST",
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert missing_status == 401
    assert missing["orders_authorized"] is False
    assert health_status == 200
    assert health["local_execution"] is True
    assert desk_status == 200
    desk_runtime = desk["runtime"]
    assert isinstance(desk_runtime, dict)
    assert desk_runtime["schema_version"] == health["schema_version"]
    assert desk_runtime["local_execution"] is True
    assert tick_status == 200
    assert tick["status"] == "blocked"
    assert order_status == 404
    assert order["orders_authorized"] is False
    serialized = json.dumps([missing, health, desk, tick, order])
    assert token not in serialized


def test_execution_desk_http_is_authenticated_and_keeps_research_fail_closed(
    tmp_path: Path,
) -> None:
    execution = _RecordingExecutionDesk()
    runtime = LocalResearchRuntime(
        data_root=tmp_path / "data",
        runs_root=tmp_path / "runs",
        market_data=UnconfiguredMarketDataAdapter(),
        pipeline=_RecordingPipeline(),
        execution_desk=execution,
    )
    token = "ephemeral-local-runtime-token-1234"
    server = build_local_research_http_server(
        runtime,
        host="127.0.0.1",
        port=0,
        bearer_token=token,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        status_code, status = _http_json(
            f"{base}/v1/execution",
            token=token,
        )
        command_code, receipt = _http_json(
            f"{base}/v1/execution/commands",
            token=token,
            method="POST",
            payload={"kind": "connect"},
        )
        health_code, health = _http_json(
            f"{base}/v1/health",
            token=token,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert status_code == 200
    assert status["mode"] == "live"
    assert status["port"] == 4001
    assert status["enabled"] is False
    assert command_code == 200
    assert receipt["kind"] == "connect"
    assert execution.commands == [{"kind": "connect"}]
    assert health_code == 200
    assert health["orders_authorized"] is False


def test_paper_autopilot_http_is_an_isolated_4002_control_plane(
    tmp_path: Path,
) -> None:
    paper = _RecordingPaperAutopilot()
    runtime = LocalResearchRuntime(
        data_root=tmp_path / "data",
        runs_root=tmp_path / "runs",
        market_data=UnconfiguredMarketDataAdapter(),
        pipeline=_RecordingPipeline(),
        paper_autopilot=paper,
    )
    token = "ephemeral-local-runtime-token-1234"
    server = build_local_research_http_server(
        runtime,
        host="127.0.0.1",
        port=0,
        bearer_token=token,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        status_code, status = _http_json(
            f"{base}/v1/paper-autopilot", token=token
        )
        command_code, receipt = _http_json(
            f"{base}/v1/paper-autopilot/commands",
            token=token,
            method="POST",
            payload={"kind": "connect"},
        )
        health_code, health = _http_json(f"{base}/v1/health", token=token)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert status_code == 200
    assert status["mode"] == "paper"
    assert status["port"] == 4002
    assert command_code == 200
    assert receipt["connected"] is True
    assert paper.commands == [{"kind": "connect"}]
    assert health_code == 200
    assert health["orders_authorized"] is False
    paper_status = health["paper_autopilot"]
    assert isinstance(paper_status, dict)
    assert paper_status["port"] == 4002


def test_execution_command_rejects_non_object_json(tmp_path: Path) -> None:
    runtime = LocalResearchRuntime(
        data_root=tmp_path / "data",
        runs_root=tmp_path / "runs",
        market_data=UnconfiguredMarketDataAdapter(),
        pipeline=_RecordingPipeline(),
        execution_desk=_RecordingExecutionDesk(),
    )
    token = "ephemeral-local-runtime-token-1234"
    server = build_local_research_http_server(
        runtime,
        host="127.0.0.1",
        port=0,
        bearer_token=token,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        request = Request(
            f"{base}/v1/execution/commands",
            headers=headers,
            data=b"[]",
            method="POST",
        )
        try:
            urlopen(request, timeout=5)  # noqa: S310
            raise AssertionError("non-object command should not be accepted")
        except HTTPError as error:
            status = error.code
            payload = json.loads(error.read())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert status == 400
    assert payload["orders_authorized"] is False


def test_execution_http_maps_broker_failures_to_safe_codes(tmp_path: Path) -> None:
    runtime = LocalResearchRuntime(
        data_root=tmp_path / "data",
        runs_root=tmp_path / "runs",
        market_data=UnconfiguredMarketDataAdapter(),
        pipeline=_RecordingPipeline(),
        execution_desk=_RecordingExecutionDesk(),
    )
    token = "ephemeral-local-runtime-token-1234"
    server = build_local_research_http_server(
        runtime,
        host="127.0.0.1",
        port=0,
        bearer_token=token,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        status, payload = _http_json(
            f"{base}/v1/execution/commands",
            token=token,
            method="POST",
            payload={"kind": "fail_connection"},
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert status == 409
    assert payload == {
        "error": "connection_failed",
        "error_code": "connection_failed",
        "orders_authorized": False,
    }
    assert "hidden" not in json.dumps(payload)
