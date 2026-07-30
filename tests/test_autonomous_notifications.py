from __future__ import annotations

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
    assert "模拟盘" in push.messages[0]
    assert "当日收益率：+1.23%" in push.messages[0]
    assert "初始保护幅度：-2.00%" in push.messages[0]
    assert "$" not in push.messages[0]


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
