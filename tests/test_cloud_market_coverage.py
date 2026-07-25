from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from data_plane.http import DownloadError
from data_plane.providers.alpaca import fetch_bars, fetch_sparse_bars_for_monitoring

START = datetime(2026, 7, 22, 15, 44, tzinfo=UTC)
END = datetime(2026, 7, 22, 15, 46, tzinfo=UTC)


def test_historical_bars_fail_closed_when_cloud_recommends_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLOUD_PLATFORM_BASE_URL", "http://localhost:8765")
    monkeypatch.setenv("CLOUD_MARKET_DATA_API_TOKEN", "market-token")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer market-token"
        return httpx.Response(
            200,
            json={
                "api_version": "v1",
                "bars": [],
                "coverage": {
                    "status": "empty",
                    "fallback_recommended": True,
                    "symbols": [
                        {
                            "symbol": "SMCI",
                            "status": "empty",
                            "reason_codes": [
                                "upstream_empty",
                                "regular_session_missing",
                            ],
                        }
                    ],
                },
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(DownloadError, match="coverage is not usable"):
            fetch_bars(("SMCI",), START, END, client=client)


def test_historical_bars_accept_explicit_observed_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLOUD_PLATFORM_BASE_URL", "http://localhost:8765")
    monkeypatch.setenv("CLOUD_MARKET_DATA_API_TOKEN", "market-token")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "api_version": "v1",
                "bars": [
                    {
                        "symbol": "AAPL",
                        "ts_utc": "2026-07-22T15:44:00Z",
                        "open": 100,
                        "high": 101,
                        "low": 99,
                        "close": 100.5,
                        "volume": 10,
                        "trade_count": 2,
                        "vwap": 100.25,
                        "source": "cloud.alpaca.market_data",
                        "feed": "sip",
                        "adjustment": "split_adjusted",
                    }
                ],
                "coverage": {
                    "status": "observed",
                    "fallback_recommended": False,
                    "symbols": [
                        {
                            "symbol": "AAPL",
                            "status": "observed",
                            "reason_codes": [],
                        }
                    ],
                },
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        frame = fetch_bars(("AAPL",), START, END, client=client)
    assert frame.height == 1
    assert frame.get_column("symbol").to_list() == ["AAPL"]


def test_advisory_monitor_can_preserve_sparse_bars_and_gap_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLOUD_PLATFORM_BASE_URL", "http://localhost:8765")
    monkeypatch.setenv("CLOUD_MARKET_DATA_API_TOKEN", "market-token")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "api_version": "v1",
                "bars": [
                    {
                        "symbol": "RNG",
                        "ts_utc": "2026-07-22T15:44:00Z",
                        "open": 47,
                        "high": 48,
                        "low": 46.5,
                        "close": 47.5,
                        "volume": 100,
                        "trade_count": 20,
                        "vwap": 47.25,
                    }
                ],
                "coverage": {
                    "status": "gaps_detected",
                    "fallback_recommended": True,
                    "symbols": [
                        {
                            "symbol": "RNG",
                            "status": "gaps_detected",
                            "missing_minute_count": 1,
                            "reason_codes": ["minute_gaps_detected"],
                        }
                    ],
                },
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        frame, coverage = fetch_sparse_bars_for_monitoring(
            ("RNG",),
            START,
            END,
            client=client,
        )

    assert frame.height == 1
    assert coverage["status"] == "gaps_detected"
    assert coverage["fallback_recommended"] is True
