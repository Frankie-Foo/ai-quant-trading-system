from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from operations.local_research_http import build_local_research_http_server
from operations.local_research_runtime import (
    AlpacaProxyMarketDataAdapter,
    EnvironmentMarketDataAdapter,
    LocalResearchRuntime,
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
