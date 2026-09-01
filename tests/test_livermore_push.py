from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest
from pydantic import SecretStr

from operations.livermore_push import (
    LivermorePushClient,
    LivermorePushError,
)


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> LivermorePushClient:
    return LivermorePushClient(
        app_id="vbot-test",
        app_secret=SecretStr("secret-value"),
        channel_id="channel-1",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_livermore_push_requires_bot_identity_and_utf8_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-vertu-bot-app-id"] == "vbot-test"
        assert request.headers["x-vertu-bot-app-secret"] == "secret-value"
        payload = json.loads(request.content.decode("utf-8"))
        assert payload == {
            "channel_id": "channel-1",
            "body": "量化系统通知通道健康检查",
        }
        return httpx.Response(
            200,
            json={
                "message": {
                    "id": "message-1",
                    "sender_type": "bot",
                }
            },
        )

    assert (
        _client(handler).push("量化系统通知通道健康检查")
        == "message-1"
    )


def test_livermore_push_error_never_exposes_secret_or_response_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="secret-value internal detail")

    with pytest.raises(LivermorePushError) as captured:
        _client(handler).push("health probe")

    assert "secret-value" not in str(captured.value)
    assert "403" in str(captured.value)


@pytest.mark.parametrize("body", ["乱码\ufffd", "连续??问号"])
def test_livermore_rejects_invalid_chinese_before_network(body: str) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    with pytest.raises(ValueError, match="UTF-8"):
        _client(handler).push(body)
    assert calls == 0


def test_channel_health_requires_the_exact_configured_channel_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/im/user-robots/channels"
        return httpx.Response(
            200,
            json={
                "channels": [
                    {"channel_id": "channel-1", "name": "AI投资群"},
                    {"channel_id": "channel-2", "name": "AI投资群"},
                ]
            },
        )

    assert _client(handler).configured_channel_available() is True
