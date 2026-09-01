from __future__ import annotations

import json

import httpx
import pytest

from research.providers.openrouter_runtime import (
    OPENROUTER_BASE_URL,
    OpenRouterRuntimeClient,
    OpenRouterRuntimeError,
)


def test_runtime_client_requires_exact_model_and_json_object() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == f"{OPENROUTER_BASE_URL}/chat/completions"
        assert request.headers["Authorization"] == "Bearer test-secret"
        payload = json.loads(request.content)
        assert payload["model"] == "openai/gpt-5.6"
        assert payload["response_format"] == {"type": "json_object"}
        assert payload["temperature"] == 0
        return httpx.Response(
            200,
            json={
                "id": "runtime-1",
                "model": "openai/gpt-5.6",
                "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 3},
            },
        )

    response = OpenRouterRuntimeClient(
        api_key="test-secret", transport=httpx.MockTransport(handler)
    ).complete_json("facts", model_id="openai/gpt-5.6")

    assert response.response_model == "openai/gpt-5.6"
    assert response.provider_request_id == "runtime-1"


def test_runtime_client_fails_closed_without_leaking_key() -> None:
    client = OpenRouterRuntimeClient(
        api_key="never-print-this",
        attempts=1,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(401, json={"error": {"message": "bad key"}})
        ),
    )

    with pytest.raises(OpenRouterRuntimeError) as exc_info:
        client.complete_json("facts", model_id="openai/gpt-5.6")

    assert "HTTP 401" in str(exc_info.value)
    assert "never-print-this" not in str(exc_info.value)
