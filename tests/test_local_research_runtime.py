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
) -> tuple[int, dict[str, object]]:
    headers = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, headers=headers, method=method)
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
