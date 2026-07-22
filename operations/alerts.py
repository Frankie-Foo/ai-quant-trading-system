"""Secret-safe operational alert delivery with durable local receipts."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx


@dataclass(frozen=True)
class AlertReceipt:
    alert_id: str
    delivered: bool
    delivered_at_utc: datetime
    acknowledgement_id: str | None
    channel: str


class WebhookAlerter:
    def __init__(
        self,
        *,
        webhook_url: str,
        receipt_log: Path,
        client: httpx.Client | None = None,
    ):
        if not webhook_url.startswith("https://"):
            raise ValueError("alert webhook must use HTTPS")
        self.webhook_url = webhook_url
        self.receipt_log = receipt_log
        self.receipt_log.parent.mkdir(parents=True, exist_ok=True)
        self.client = client or httpx.Client(timeout=15)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def send(
        self,
        *,
        alert_id: str,
        severity: str,
        summary: str,
        details: dict[str, object],
    ) -> AlertReceipt:
        if not alert_id.strip() or not summary.strip():
            raise ValueError("alert ID and summary are required")
        if severity not in {"warning", "critical"}:
            raise ValueError("alert severity must be warning or critical")
        response = self.client.post(
            self.webhook_url,
            json={
                "alert_id": alert_id,
                "severity": severity,
                "summary": summary,
                "details": details,
                "sent_at_utc": datetime.now(UTC).isoformat(),
            },
        )
        response.raise_for_status()
        acknowledgement: str | None = None
        try:
            payload: Any = response.json()
        except (json.JSONDecodeError, ValueError):
            payload = None
        if isinstance(payload, dict):
            raw = payload.get("ack_id") or payload.get("id")
            acknowledgement = str(raw) if raw is not None else None
        receipt = AlertReceipt(
            alert_id=alert_id,
            delivered=True,
            delivered_at_utc=datetime.now(UTC),
            acknowledgement_id=acknowledgement,
            channel="https_webhook",
        )
        with self.receipt_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(receipt), default=str, sort_keys=True) + "\n")
        return receipt
