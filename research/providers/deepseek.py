from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx

from research.catalyst_scoring import ModelScoreResponse

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-v4-pro"


class DeepSeekProviderError(RuntimeError):
    """A sanitized provider failure that never includes credentials or prompts."""


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


@dataclass(frozen=True)
class DeepSeekClient:
    """Minimal auditable DeepSeek V4-Pro probability-scoring client."""

    api_key: str
    model_id: str = DEEPSEEK_MODEL
    base_url: str = DEEPSEEK_BASE_URL
    timeout_seconds: float = 60.0
    attempts: int = 4
    transport: httpx.BaseTransport | None = None

    def __post_init__(self) -> None:
        if not self.api_key.strip():
            raise ValueError("DeepSeek API key is required")
        if self.model_id != DEEPSEEK_MODEL:
            raise ValueError(f"model_id must be the frozen model {DEEPSEEK_MODEL}")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.attempts <= 0:
            raise ValueError("attempts must be positive")

    @classmethod
    def from_env(cls) -> DeepSeekClient:
        return cls(api_key=os.environ.get("DEEPSEEK_API_KEY", ""))

    def score(self, prompt: str) -> ModelScoreResponse:
        if not prompt.strip():
            raise ValueError("score prompt is required")
        return self._complete(prompt, max_tokens=16, json_object=False)

    def complete_json(self, prompt: str, *, max_tokens: int = 2048) -> ModelScoreResponse:
        """Return one validated JSON-object completion without exposing provider data."""
        if not prompt.strip():
            raise ValueError("completion prompt is required")
        response = self._complete(prompt, max_tokens=max_tokens, json_object=True)
        try:
            value = json.loads(response.content)
        except json.JSONDecodeError as exc:
            raise DeepSeekProviderError(
                "DeepSeek structured response was not valid JSON"
            ) from exc
        if not isinstance(value, dict):
            raise DeepSeekProviderError(
                "DeepSeek structured response must be a JSON object"
            )
        return response

    def _complete(
        self, prompt: str, *, max_tokens: int, json_object: bool
    ) -> ModelScoreResponse:
        if max_tokens <= 0:
            raise ValueError("max_tokens must be positive")
        payload: dict[str, Any] = {
            "model": self.model_id,
            "messages": [{"role": "user", "content": prompt}],
            # Catalyst scoring has a one-number output contract. Non-thinking mode
            # permits temperature=0 and avoids storing or paying for unused CoT.
            "thinking": {"type": "disabled"},
            "temperature": 0,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if json_object:
            payload["response_format"] = {"type": "json_object"}
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        response: httpx.Response | None = None
        last_error: Exception | None = None
        with httpx.Client(
            timeout=self.timeout_seconds,
            transport=self.transport,
        ) as client:
            for attempt in range(self.attempts):
                try:
                    response = client.post(
                        f"{self.base_url.rstrip('/')}/chat/completions",
                        headers=headers,
                        json=payload,
                    )
                    response.raise_for_status()
                    break
                except (httpx.HTTPError, OSError) as exc:
                    last_error = exc
                    retryable = not isinstance(exc, httpx.HTTPStatusError) or (
                        exc.response.status_code == 429
                        or exc.response.status_code >= 500
                    )
                    if not retryable or attempt + 1 >= self.attempts:
                        response = None
                        break
                    retry_after = 0.0
                    if isinstance(exc, httpx.HTTPStatusError):
                        value = exc.response.headers.get("retry-after")
                        if value:
                            try:
                                retry_after = float(value)
                            except ValueError:
                                retry_after = 0.0
                    time.sleep(max(retry_after, min(60.0, float(2**attempt))))
        if response is None:
            detail = type(last_error).__name__ if last_error else "unknown error"
            if isinstance(last_error, httpx.HTTPStatusError):
                detail = f"HTTP {last_error.response.status_code}"
            raise DeepSeekProviderError(f"DeepSeek request failed: {detail}")
        try:
            body = response.json()
        except ValueError as exc:
            raise DeepSeekProviderError("DeepSeek response was not valid JSON") from exc
        if not isinstance(body, dict):
            raise DeepSeekProviderError("DeepSeek response was not a JSON object")
        return self._parse_response(body)

    def _parse_response(self, body: dict[str, Any]) -> ModelScoreResponse:
        response_model = _optional_text(body.get("model"))
        if response_model != self.model_id:
            raise DeepSeekProviderError(
                f"DeepSeek returned unexpected model: {response_model or 'missing'}"
            )
        choices = body.get("choices")
        if not isinstance(choices, list) or len(choices) != 1:
            raise DeepSeekProviderError("DeepSeek response must contain exactly one choice")
        choice = choices[0]
        if not isinstance(choice, dict) or choice.get("finish_reason") != "stop":
            raise DeepSeekProviderError("DeepSeek response did not finish normally")
        message = choice.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise DeepSeekProviderError("DeepSeek response content was empty")
        usage = body.get("usage")
        usage_values = usage if isinstance(usage, dict) else {}
        return ModelScoreResponse(
            content=content,
            provider_request_id=_optional_text(body.get("id")),
            response_model=response_model,
            system_fingerprint=_optional_text(body.get("system_fingerprint")),
            prompt_tokens=_optional_int(usage_values.get("prompt_tokens")),
            completion_tokens=_optional_int(usage_values.get("completion_tokens")),
        )
