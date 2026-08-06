from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
from perp_risk.backup import create_backup, restore_backup
from perp_risk.config import load_config
from perp_risk.models import RiskSnapshot
from perp_risk.notifications import WebhookNotifier
from perp_risk.store import RiskStore

NOW = datetime(2026, 7, 30, 14, tzinfo=UTC)


def _snapshot() -> RiskSnapshot:
    return RiskSnapshot(
        skill_version="0.1.0",
        snapshot_id="snapshot",
        asof_utc=NOW,
        data_cutoff_utc=NOW,
        config_hash="hash",
        actionable=True,
        session_state="actionable",
        provider_status=(),
        targets=(),
        production_eligible=False,
        execution_eligible=False,
        orders_submitted=0,
    )


def test_encrypted_backup_round_trip(tmp_path: Path) -> None:
    store = RiskStore(tmp_path / "source.sqlite3")
    store.set_metadata("proof", "present")
    backup = create_backup(
        store,
        output=tmp_path / "backup.bin",
        passphrase="correct horse battery staple",
    )
    store.close()

    restored = restore_backup(
        source=backup,
        destination=tmp_path / "restored.sqlite3",
        passphrase="correct horse battery staple",
    )
    restored_store = RiskStore(restored)
    try:
        assert restored_store.get_metadata("proof") == "present"
    finally:
        restored_store.close()


def test_webhook_sends_utf8_once_then_deduplicates(tmp_path: Path) -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"ok": True})

    config = load_config().notification.model_copy(
        update={"enabled": True, "url": "https://example.test/push"}
    )
    notifier = WebhookNotifier(
        config,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    store = RiskStore(tmp_path / "notify.sqlite3")

    try:
        first = notifier.maybe_send(_snapshot(), store=store)
        second = notifier.maybe_send(_snapshot(), store=store)
    finally:
        notifier.close()
        store.close()

    assert first == "sent"
    assert second == "deduplicated"
    assert len(bodies) == 1
    assert "永续合约风险仓位" in str(bodies[0]["body"])
    assert "?" not in str(bodies[0]["body"])
