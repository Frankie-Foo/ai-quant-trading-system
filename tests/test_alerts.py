from __future__ import annotations

import json
from pathlib import Path

import httpx

from operations.alerts import WebhookAlerter


def test_webhook_alert_writes_acknowledged_receipt(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["severity"] == "critical"
        assert payload["details"] == {"service": "paper"}
        return httpx.Response(200, json={"ack_id": "ack-123"})

    log = tmp_path / "receipts.jsonl"
    alerter = WebhookAlerter(
        webhook_url="https://alerts.example.test/hook",
        receipt_log=log,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    receipt = alerter.send(
        alert_id="alert-1",
        severity="critical",
        summary="Paper session failed",
        details={"service": "paper"},
    )
    assert receipt.delivered is True
    assert receipt.acknowledgement_id == "ack-123"
    assert json.loads(log.read_text(encoding="utf-8"))["alert_id"] == "alert-1"
