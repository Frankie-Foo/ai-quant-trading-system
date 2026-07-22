from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from data_plane.cloud_features import (
    CloudFeatureApiError,
    CloudFeatureCache,
    CloudFeatureClient,
)

NOW = datetime(2026, 7, 22, 15, 30, tzinfo=UTC)


def _payload() -> dict[str, object]:
    return {
        "api_version": "v1",
        "feature_vector": {
            "symbol": "AAPL",
            "asof_utc": NOW.isoformat(),
            "input_event_id": "event-1",
            "features": [
                {
                    "name": "minute_return",
                    "value": 0.01,
                    "asof_utc": NOW.isoformat(),
                    "definition_version": "sip.minute.v1",
                    "provenance": "cloud-feature-api@test",
                }
            ],
        },
    }


def test_slow_loop_client_fetches_versioned_features_and_cache_is_point_in_time(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer secret-token"
        assert request.url.path == "/v1/features/AAPL"
        return httpx.Response(200, json=_payload())

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as http_client:
        client = CloudFeatureClient(
            base_url="http://localhost:8765",
            token=SecretStr("secret-token"),
            client=http_client,
        )
        vector = client.fetch("aapl", asof_utc=NOW)
    assert vector is not None
    cache = CloudFeatureCache(tmp_path / "features.sqlite3")
    cache.put(vector)
    assert cache.latest("AAPL", asof_utc=NOW - timedelta(microseconds=1)) is None
    assert cache.latest("AAPL", asof_utc=NOW) == vector


def test_client_fails_closed_on_wrong_contract_version() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, json={"api_version": "v2", "feature_vector": None}
        )
    )
    with httpx.Client(transport=transport) as http_client:
        client = CloudFeatureClient(
            base_url="http://127.0.0.1:8765",
            token=SecretStr("secret-token"),
            client=http_client,
        )
        with pytest.raises(CloudFeatureApiError):
            client.fetch("AAPL", asof_utc=NOW)


def test_client_rejects_insecure_nonlocal_http() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        CloudFeatureClient(
            base_url="http://example.com",
            token=SecretStr("secret-token"),
        )
