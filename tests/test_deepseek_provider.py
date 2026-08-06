from __future__ import annotations

import json

import httpx
import pytest

from research.providers.deepseek import (
    DEEPSEEK_MODEL,
    DeepSeekClient,
    DeepSeekProviderError,
)


def test_deepseek_v4_pro_request_is_non_thinking_and_deterministic() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.deepseek.com/chat/completions"
        assert request.headers["Authorization"] == "Bearer test-secret"
        payload = json.loads(request.content)
        assert payload["model"] == "deepseek-v4-pro"
        assert payload["thinking"] == {"type": "disabled"}
        assert payload["temperature"] == 0
        assert payload["max_tokens"] == 16
        assert payload["messages"] == [{"role": "user", "content": "score this"}]
        return httpx.Response(
            200,
            json={
                "id": "request-123",
                "model": "deepseek-v4-pro",
                "system_fingerprint": "fp_test",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "0.73"},
                    }
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 3,
                    "total_tokens": 103,
                },
            },
        )

    client = DeepSeekClient(
        api_key="test-secret",
        transport=httpx.MockTransport(handler),
    )
    response = client.score("score this")
    assert DEEPSEEK_MODEL == "deepseek-v4-pro"
    assert response.content == "0.73"
    assert response.response_model == "deepseek-v4-pro"
    assert response.provider_request_id == "request-123"
    assert response.system_fingerprint == "fp_test"
    assert response.prompt_tokens == 100
    assert response.completion_tokens == 3


def test_deepseek_provider_rejects_bad_response_without_leaking_key() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "invalid key"}})

    client = DeepSeekClient(
        api_key="never-print-this",
        transport=httpx.MockTransport(handler),
        attempts=1,
    )
    with pytest.raises(DeepSeekProviderError) as exc_info:
        client.score("score this")
    assert "HTTP 401" in str(exc_info.value)
    assert "never-print-this" not in str(exc_info.value)


@pytest.mark.parametrize("api_key", ["", "   "])
def test_deepseek_provider_requires_api_key(api_key: str) -> None:
    with pytest.raises(ValueError, match="API key"):
        DeepSeekClient(api_key=api_key)


def test_complete_json_requests_and_validates_json_object() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["response_format"] == {"type": "json_object"}
        assert payload["max_tokens"] == 512
        return httpx.Response(
            200,
            json={
                "id": "request-json",
                "model": DEEPSEEK_MODEL,
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": '{"summary":"ok"}'},
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    client = DeepSeekClient(
        api_key="not-a-real-key", transport=httpx.MockTransport(handler)
    )
    response = client.complete_json("prompt", max_tokens=512)
    assert json.loads(response.content) == {"summary": "ok"}


def test_complete_json_rejects_non_object_content() -> None:
    client = DeepSeekClient(
        api_key="not-a-real-key",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "model": DEEPSEEK_MODEL,
                    "choices": [
                        {"finish_reason": "stop", "message": {"content": "[]"}}
                    ],
                },
            )
        ),
    )
    with pytest.raises(DeepSeekProviderError, match="JSON object"):
        client.complete_json("prompt")
