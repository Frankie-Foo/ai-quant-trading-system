from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from execution.autonomous_paper_session import (
    AutonomousPaperPlan,
    PaperSessionResult,
    SessionAction,
)
from operations.autonomous_notifications import (
    AutonomousNotificationLedger,
    AutonomousPaperNotifier,
    deliver_notification_or_fail_closed,
    record_push_delivery_health,
)
from operations.feishu_base import InvestmentTable
from operations.runtime_agent_safety import load_push_health_evidence

NOW = datetime(2026, 7, 29, 14, 0, tzinfo=UTC)


class FakePush:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def push(self, body: str) -> str:
        self.messages.append(body)
        return f"message-{len(self.messages)}"


class FailingPush:
    def push(self, body: str) -> str:
        del body
        raise RuntimeError("secret-bearing upstream error")


class FakeBase:
    def __init__(self) -> None:
        self.events: list[tuple[InvestmentTable, str, dict[str, object]]] = []

    def record_event(
        self,
        table: InvestmentTable,
        event_id: str,
        fields: Mapping[str, object],
    ) -> str:
        self.events.append((table, event_id, dict(fields)))
        return f"record-{len(self.events)}"


class RecordingFailClosed:
    def __init__(self) -> None:
        self.reasons: list[str] = []

    def fail_closed_plan(
        self,
        *,
        plan_id: str,
        observed_at_utc: datetime,
        reason: str,
    ) -> PaperSessionResult:
        assert plan_id == _plan().plan_id
        self.reasons.append(reason)
        return PaperSessionResult(
            action=SessionAction.DATA_BLOCKED,
            decision=None,
            daily_return=Decimal("0"),
            day_locked=False,
            new_entries_allowed=False,
            cancelled_order_ids=("pending-entry-1",),
            flatten_order_ids=(),
            reasons=(reason,),
            provenance="test.fail-closed",
        )


def _plan() -> AutonomousPaperPlan:
    return AutonomousPaperPlan(
        plan_id="auto-20260729-XYZ",
        symbol="XYZ",
        trade_date=date(2026, 7, 29),
        reference_price=Decimal("100"),
        hard_stop=Decimal("98"),
        max_notional_fraction=Decimal("0.20"),
        full_risk_fraction=Decimal("0.0035"),
        source_snapshot_ids=("selection-1",),
        provenance="accepted.selection.v1",
    )


def _result() -> PaperSessionResult:
    return PaperSessionResult(
        action=SessionAction.ENTRY_SUBMITTED,
        decision=None,
        daily_return=Decimal("0.0123"),
        day_locked=False,
        new_entries_allowed=True,
        cancelled_order_ids=(),
        flatten_order_ids=(),
        reasons=("catalyst_route_passed",),
        provenance="execution.autonomous_paper.entry.v1",
        submitted_order_ids=("order-1",),
    )


def test_order_notification_is_chinese_percentage_only_and_restart_deduped(
    tmp_path: Path,
) -> None:
    push = FakePush()
    ledger_path = tmp_path / "notifications.sqlite3"
    first = AutonomousPaperNotifier(
        push=push,
        ledger=AutonomousNotificationLedger(ledger_path),
    ).notify(_plan(), _result(), observed_at_utc=NOW)
    replay = AutonomousPaperNotifier(
        push=push,
        ledger=AutonomousNotificationLedger(ledger_path),
    ).notify(_plan(), _result(), observed_at_utc=NOW)

    assert first is True
    assert replay is False
    assert len(push.messages) == 1
    assert f"plan_id={_plan().plan_id}" in push.messages[0]
    assert f"provenance={_plan().provenance}" in push.messages[0]
    records = AutonomousNotificationLedger(ledger_path).list_records()
    assert len(records) == 1
    assert records[0]["message_id"] == "message-1"
    assert records[0]["message_body"] == push.messages[0]
    payload = records[0]["payload"]
    assert isinstance(payload, dict)
    assert payload["plan_id"] == _plan().plan_id
    assert payload["provenance"] == _result().provenance
    assert "模拟盘" in push.messages[0]
    assert "当日收益率：+1.23%" in push.messages[0]
    assert "初始保护幅度：-2.00%" in push.messages[0]
    assert "$" not in push.messages[0]


def test_notification_ledger_migrates_legacy_rows(tmp_path: Path) -> None:
    ledger_path = tmp_path / "notifications.sqlite3"
    with sqlite3.connect(ledger_path) as connection:
        connection.execute(
            """
            CREATE TABLE autonomous_notifications (
                notification_key TEXT PRIMARY KEY,
                message_id TEXT NOT NULL,
                sent_at_utc TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO autonomous_notifications VALUES (?, ?, ?)",
            ("legacy-key", "legacy-message", NOW.isoformat()),
        )

    records = AutonomousNotificationLedger(ledger_path).list_records()

    with sqlite3.connect(ledger_path) as connection:
        versions = connection.execute(
            """
            SELECT version
            FROM schema_migrations
            WHERE owner = 'operations.autonomous_notifications'
            ORDER BY version
            """
        ).fetchall()

    assert records == (
        {
            "notification_key": "legacy-key",
            "message_id": "legacy-message",
            "sent_at_utc": NOW.isoformat(),
            "message_body": "",
            "payload": {},
        },
    )
    assert versions == [(1,), (2,)]


def test_action_notification_reuses_original_message_and_records_monitor_and_trade_events(
    tmp_path: Path,
) -> None:
    base = FakeBase()
    notified = AutonomousPaperNotifier(
        push=FakePush(),
        base=base,
        ledger=AutonomousNotificationLedger(tmp_path / "notifications.sqlite3"),
    ).notify(_plan(), _result(), observed_at_utc=NOW)

    assert notified is True
    assert len(base.events) == 2
    assert [event[0] for event in base.events] == [
        InvestmentTable.MONITOR,
        InvestmentTable.TRADE,
    ]
    table, event_id, fields = base.events[1]
    assert table is InvestmentTable.TRADE
    assert event_id.startswith("trade:")
    assert fields["运行ID"] == event_id
    assert fields["成交时间"] == NOW
    assert fields["股票代码"] == "XYZ"
    assert fields["模拟账户"] == "paper"
    assert _plan().plan_id in str(fields["触发来源"])
    assert fields["执行摘要"]


def test_push_delivery_failure_creates_five_minute_fail_closed_latch(
    tmp_path: Path,
) -> None:
    path = tmp_path / "push-health.json"

    record_push_delivery_health(
        path,
        observed_at_utc=NOW,
        healthy=False,
        event_identity="entry-order-1",
    )

    evidence = load_push_health_evidence(path)
    assert evidence.healthy is False
    assert evidence.generated_at_utc == NOW
    assert evidence.expires_at_utc == NOW + timedelta(minutes=5)
    assert evidence.provenance == "operations.autonomous_notifications.delivery.v1"


def test_failed_delivery_immediately_invokes_runtime_fail_closed(
    tmp_path: Path,
) -> None:
    fail_closed = RecordingFailClosed()
    delivery = deliver_notification_or_fail_closed(
        plan=_plan(),
        result=_result(),
        observed_at_utc=NOW,
        notifier=AutonomousPaperNotifier(
            push=FailingPush(),
            ledger=AutonomousNotificationLedger(
                tmp_path / "notifications.sqlite3"
            ),
        ),
        push_health_path=tmp_path / "push-health.json",
        fail_closed=fail_closed,
    )

    assert delivery.notified is False
    assert delivery.failure_reason == "notification_push_failed"
    assert delivery.fail_closed_result is not None
    assert fail_closed.reasons == ["notification_push_failed"]
    assert load_push_health_evidence(
        tmp_path / "push-health.json"
    ).healthy is False


def test_inflight_delivery_fails_closed_without_a_duplicate_push(tmp_path: Path) -> None:
    ledger = AutonomousNotificationLedger(tmp_path / "notifications.sqlite3")
    from operations.autonomous_notifications import _notification_key

    ledger.claim(_notification_key(_plan(), _result()), claimed_at_utc=NOW)
    fail_closed = RecordingFailClosed()
    push = FakePush()
    delivery = deliver_notification_or_fail_closed(
        plan=_plan(),
        result=_result(),
        observed_at_utc=NOW,
        notifier=AutonomousPaperNotifier(push=push, ledger=ledger),
        push_health_path=tmp_path / "push-health.json",
        fail_closed=fail_closed,
    )

    assert delivery.failure_reason == "notification_delivery_in_flight"
    assert push.messages == []
    assert fail_closed.reasons == ["notification_delivery_in_flight"]
