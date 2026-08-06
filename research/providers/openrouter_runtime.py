"""Strict OpenRouter JSON client used only by Paper runtime safety agents."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import httpx

from research.catalyst_scoring import ModelScoreResponse

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterRuntimeError(RuntimeError):
    """Sanitized provider failure that never includes credentials or prompts."""


@dataclass(frozen=True)
class OpenRouterRuntimeClient:
    api_key: str
    base_url: str = OPENROUTER_BASE_URL
    timeout_seconds: float = 45.0
    attempts: int = 2
    transport: httpx.BaseTransport | None = None

    def __post_init__(self) -> None:
        if len(self.api_key.strip()) < 8:
            raise ValueError("OpenRouter API key is required")
        if self.base_url.rstrip("/") != OPENROUTER_BASE_URL:
            raise ValueError("OpenRouter runtime must use the pinned API endpoint")
        if self.timeout_seconds <= 0 or self.attempts <= 0:
            raise ValueError("OpenRouter runtime timeout and attempts must be positive")

    def complete_json(
        self,
        prompt: str,
        *,
        model_id: str,
        max_tokens: int = 1024,
    ) -> ModelScoreResponse:
        if not prompt.strip() or not model_id.strip() or max_tokens <= 0:
            raise ValueError("OpenRouter runtime completion inputs are invalid")
        payload: dict[str, Any] = {
            "model": model_id,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "stream": False,
        }
        response = self._post(payload)
        try:
            body = response.json()
        except ValueError as exc:
            raise OpenRouterRuntimeError("OpenRouter response was not valid JSON") from exc
        if not isinstance(body, dict):
            raise OpenRouterRuntimeError("OpenRouter response was not a JSON object")
        returned_model = body.get("model")
        if returned_model != model_id:
            raise OpenRouterRuntimeError("OpenRouter returned an unexpected model")
        choices = body.get("choices")
        if not isinstance(choices, list) or len(choices) != 1:
            raise OpenRouterRuntimeError("OpenRouter response must contain exactly one choice")
        choice = choices[0]
        if not isinstance(choice, dict) or choice.get("finish_reason") != "stop":
            raise OpenRouterRuntimeError("OpenRouter response did not finish normally")
        message = choice.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise OpenRouterRuntimeError("OpenRouter response content was empty")
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise OpenRouterRuntimeError(
                "OpenRouter structured response was not valid JSON"
            ) from exc
        if not isinstance(parsed, dict):
            raise OpenRouterRuntimeError(
                "OpenRouter structured response must be a JSON object"
            )
        usage = body.get("usage")
        usage_values = usage if isinstance(usage, dict) else {}
        return ModelScoreResponse(
            content=content,
            provider_request_id=_optional_text(body.get("id")),
            response_model=model_id,
            system_fingerprint=_optional_text(body.get("system_fingerprint")),
            prompt_tokens=_optional_int(usage_values.get("prompt_tokens")),
            completion_tokens=_optional_int(usage_values.get("completion_tokens")),
        )

    def _post(self, payload: dict[str, Any]) -> httpx.Response:
        last_error: Exception | None = None
        with httpx.Client(
            timeout=self.timeout_seconds,
            transport=self.transport,
        ) as client:
            for attempt in range(self.attempts):
                try:
                    response = client.post(
                        f"{self.base_url.rstrip('/')}/chat/completions",
                        headers={
                            "Authorization": f"Bearer {self.api_key}",
                            "Content-Type": "application/json",
                            "HTTP-Referer": "https://local.quant-research.app",
                            "X-Title": "AI Quant Research Desk",
                        },
                        json=payload,
                    )
                    response.raise_for_status()
                    return response
                except (httpx.HTTPError, OSError) as exc:
                    last_error = exc
                    retryable = not isinstance(exc, httpx.HTTPStatusError) or (
                        exc.response.status_code == 429
                        or exc.response.status_code >= 500
                    )
                    if not retryable or attempt + 1 >= self.attempts:
                        break
                    time.sleep(min(30.0, float(2**attempt)))
        detail = type(last_error).__name__ if last_error else "unknown"
        if isinstance(last_error, httpx.HTTPStatusError):
            detail = f"HTTP {last_error.response.status_code}"
        raise OpenRouterRuntimeError(f"OpenRouter request failed: {detail}")


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
