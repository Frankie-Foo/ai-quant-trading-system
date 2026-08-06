"""Strict UTF-8 Livermore robot client with verified bot sender identity."""

from __future__ import annotations

import json
from typing import Any, cast

import httpx
from pydantic import SecretStr


class LivermorePushError(RuntimeError):
    """Sanitized push failure without secrets or response content."""


class LivermorePushClient:
    PUSH_URL = "https://vps-service.vertu.cn/v1/im/user-robots/push"
    CHANNELS_URL = "https://vps-service.vertu.cn/v1/im/user-robots/channels"

    def __init__(
        self,
        *,
        app_id: str,
        app_secret: SecretStr,
        channel_id: str,
        push_url: str = PUSH_URL,
        client: httpx.Client | None = None,
    ):
        if push_url != self.PUSH_URL:
            raise ValueError("Livermore push client must use the pinned HTTPS endpoint")
        if not app_id.strip():
            raise ValueError("Livermore app ID is required")
        if not app_secret.get_secret_value().strip():
            raise ValueError("Livermore app secret is required")
        if not channel_id.strip():
            raise ValueError("Livermore channel ID is required")
        self.app_id = app_id
        self.channel_id = channel_id
        self._secret = app_secret
        self._client = client or httpx.Client(timeout=20.0)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def push(self, body: str) -> str:
        if not body.strip():
            raise ValueError("Livermore message body is required")
        request_body = json.dumps(
            {"channel_id": self.channel_id, "body": body},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        try:
            response = self._client.post(
                self.PUSH_URL,
                headers={
                    "content-type": "application/json; charset=utf-8",
                    "x-vertu-bot-app-id": self.app_id,
                    "x-vertu-bot-app-secret": self._secret.get_secret_value(),
                },
                content=request_body,
            )
        except (httpx.HTTPError, OSError) as exc:
            raise LivermorePushError(
                f"Livermore push failed: {type(exc).__name__}"
            ) from exc
        if response.status_code != 200:
            raise LivermorePushError(
                f"Livermore push failed with HTTP {response.status_code}"
            )
        try:
            raw = response.json()
        except ValueError as exc:
            raise LivermorePushError(
                "Livermore push response was not valid JSON"
            ) from exc
        if not isinstance(raw, dict):
            raise LivermorePushError("Livermore push response contract is invalid")
        payload = cast(dict[str, Any], raw)
        message = payload.get("message")
        if not isinstance(message, dict):
            raise LivermorePushError("Livermore push message is missing")
        if message.get("sender_type") != "bot":
            raise LivermorePushError("Livermore sender identity was not a bot")
        message_id = message.get("id")
        if not isinstance(message_id, str) or not message_id.strip():
            raise LivermorePushError("Livermore message ID is missing")
        return message_id

    def configured_channel_available(self) -> bool:
        try:
            response = self._client.get(
                self.CHANNELS_URL,
                headers={
                    "x-vertu-bot-app-id": self.app_id,
                    "x-vertu-bot-app-secret": self._secret.get_secret_value(),
                },
            )
        except (httpx.HTTPError, OSError) as exc:
            raise LivermorePushError(
                f"Livermore channel check failed: {type(exc).__name__}"
            ) from exc
        if response.status_code != 200:
            raise LivermorePushError(
                f"Livermore channel check failed with HTTP {response.status_code}"
            )
        try:
            raw = response.json()
        except ValueError as exc:
            raise LivermorePushError(
                "Livermore channel response was not valid JSON"
            ) from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("channels"), list):
            raise LivermorePushError("Livermore channel response contract is invalid")
        channels = cast(list[object], raw["channels"])
        for channel in channels:
            if not isinstance(channel, dict):
                raise LivermorePushError(
                    "Livermore channel row contract is invalid"
                )
            identifier = channel.get("channel_id", channel.get("id"))
            if identifier == self.channel_id:
                return True
        return False
