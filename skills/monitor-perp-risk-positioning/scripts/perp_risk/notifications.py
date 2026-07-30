"""Generic transition-only webhook notification."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

import httpx

from .config import NotificationConfig
from .models import RiskSnapshot
from .secrets import resolve_secret
from .store import RiskStore


class WebhookNotifier:
    def __init__(
        self,
        config: NotificationConfig,
        *,
        client: httpx.Client | None = None,
    ):
        self._config = config
        self._client = client or httpx.Client(timeout=20)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def maybe_send(
        self,
        snapshot: RiskSnapshot,
        *,
        store: RiskStore,
    ) -> str:
        if not self._config.enabled:
            return "disabled"
        signature = _material_signature(snapshot)
        previous_signature = store.get_metadata("notification_signature")
        previous_time_raw = store.get_metadata("notification_sent_at_utc")
        heartbeat_due = True
        if previous_time_raw:
            previous_time = datetime.fromisoformat(previous_time_raw)
            heartbeat_due = (
                snapshot.asof_utc - previous_time
            ).total_seconds() >= self._config.heartbeat_seconds
        if signature == previous_signature and not heartbeat_due:
            return "deduplicated"
        headers = {"content-type": "application/json"}
        secret = resolve_secret(
            keyring_service=self._config.keyring_service,
            keyring_username=self._config.keyring_username,
            environment_name=self._config.secret_env,
        )
        if secret:
            headers[self._config.secret_header] = secret
        payload = {
            "event_type": "perp_risk_positioning",
            "schema_version": snapshot.schema_version,
            "body": _message(snapshot, language=self._config.language),
            "data": snapshot.model_dump(mode="json"),
        }
        try:
            response = self._client.post(
                str(self._config.url),
                headers=headers,
                content=json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8"),
            )
            response.raise_for_status()
        except (httpx.HTTPError, OSError) as exc:
            raise RuntimeError(f"webhook push failed: {type(exc).__name__}") from exc
        store.set_metadata("notification_signature", signature)
        store.set_metadata(
            "notification_sent_at_utc",
            snapshot.asof_utc.isoformat(),
        )
        return "sent"


def _material_signature(snapshot: RiskSnapshot) -> str:
    body = [
        (
            item.target_id,
            item.regime.value,
            item.effective_multiplier,
        )
        for item in snapshot.targets
    ]
    body.extend(
        (
            item.venue,
            item.status,
            item.observation_count,
        )
        for item in snapshot.provider_status
    )
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()


def _message(snapshot: RiskSnapshot, *, language: str) -> str:
    lines = []
    for target in snapshot.targets:
        lines.append(
            f"{target.target_id}: {target.regime.value}, "
            f"{target.effective_multiplier:.1f}x, "
            f"coverage {target.coverage:.0%}"
        )
    if language == "en":
        prefix = "Perpetual risk positioning"
        state = "actionable" if snapshot.actionable else "research only"
    else:
        prefix = "永续合约风险仓位"
        state = "可执行时段" if snapshot.actionable else "仅研究时段"
    return f"{prefix}（{state}）\n" + "\n".join(lines)
