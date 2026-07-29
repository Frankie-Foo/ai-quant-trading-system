"""Restart-safe Chinese notifications for autonomous Alpaca Paper actions."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from execution.autonomous_paper_session import (
    AutonomousPaperPlan,
    PaperSessionResult,
    SessionAction,
)
from operations.runtime_agent_safety import (
    PushHealthEvidence,
    write_push_health_evidence,
)


class PushPort(Protocol):
    def push(self, body: str) -> str: ...


class NotificationFailClosedPort(Protocol):
    def fail_closed_plan(
        self,
        *,
        plan_id: str,
        observed_at_utc: datetime,
        reason: str,
    ) -> PaperSessionResult: ...


@dataclass(frozen=True)
class NotificationDelivery:
    notified: bool
    failure_reason: str | None
    fail_closed_result: PaperSessionResult | None


class AutonomousNotificationLedger:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS autonomous_notifications (
                    notification_key TEXT PRIMARY KEY,
                    message_id TEXT NOT NULL,
                    sent_at_utc TEXT NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def sent(self, notification_key: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM autonomous_notifications
                WHERE notification_key=?
                """,
                (notification_key,),
            ).fetchone()
        return row is not None

    def record(
        self,
        notification_key: str,
        *,
        message_id: str,
        sent_at_utc: datetime,
    ) -> None:
        _require_utc(sent_at_utc)
        if not notification_key.strip() or not message_id.strip():
            raise ValueError("notification identity is incomplete")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO autonomous_notifications (
                    notification_key, message_id, sent_at_utc
                ) VALUES (?, ?, ?)
                """,
                (notification_key, message_id, sent_at_utc.isoformat()),
            )


class AutonomousPaperNotifier:
    _ACTION_NAMES = {
        SessionAction.DATA_BLOCKED: "数据异常，禁止交易",
        SessionAction.WRITES_BLOCKED: "写入未授权，禁止交易",
        SessionAction.SOFT_LOSS_BLOCK: "触及单日软停止，禁止新开仓",
        SessionAction.ENTRY_SUBMITTED: "已提交入场或升级订单",
        SessionAction.REDUCE_SUBMITTED: "已提交减仓订单",
        SessionAction.EXIT_SUBMITTED: "已提交清仓订单",
        SessionAction.STOP_EXIT_SUBMITTED: "已触发保护退出",
        SessionAction.PROTECTION_SUBMITTED: "已接管盘前仓位保护止损",
        SessionAction.ACCOUNT_GUARDIAN_LOCK: "账户出现未知状态，已锁定",
        SessionAction.HARD_LOSS_FLATTEN: "触及单日硬停止，已执行清仓",
        SessionAction.DAY_LOCKED: "当日交易已锁定",
    }

    def __init__(
        self,
        *,
        push: PushPort,
        ledger: AutonomousNotificationLedger,
    ):
        self.push = push
        self.ledger = ledger

    def notify(
        self,
        plan: AutonomousPaperPlan,
        result: PaperSessionResult,
        *,
        observed_at_utc: datetime,
    ) -> bool:
        _require_utc(observed_at_utc)
        action_name = self._ACTION_NAMES.get(result.action)
        if action_name is None:
            return False
        notification_key = _notification_key(plan, result)
        if self.ledger.sent(notification_key):
            return False
        daily_return = result.daily_return * 100
        stop_return = (
            (plan.hard_stop / plan.reference_price) - 1
        ) * 100
        reason_text = "、".join(result.reasons) if result.reasons else "无"
        message = (
            f"【量化系统｜模拟盘｜{plan.symbol}】\n"
            f"动作：{action_name}\n"
            f"当日收益率：{daily_return:+.2f}%\n"
            f"初始保护幅度：{stop_return:+.2f}%\n"
            f"原因：{reason_text}\n"
            f"当日锁定：{'是' if result.day_locked else '否'}"
        )
        message_id = self.push.push(message)
        self.ledger.record(
            notification_key,
            message_id=message_id,
            sent_at_utc=observed_at_utc,
        )
        return True


def record_push_delivery_health(
    path: Path,
    *,
    observed_at_utc: datetime,
    healthy: bool,
    event_identity: str,
) -> None:
    """Publish an actual delivery result for the runtime safety envelope."""

    _require_utc(observed_at_utc)
    if not event_identity.strip():
        raise ValueError("push delivery event identity is required")
    material = "|".join(
        (
            observed_at_utc.isoformat(),
            str(healthy).lower(),
            event_identity,
        )
    )
    source_id = (
        "livermore-delivery-"
        + hashlib.sha256(material.encode()).hexdigest()[:24]
    )
    write_push_health_evidence(
        path,
        PushHealthEvidence(
            generated_at_utc=observed_at_utc,
            expires_at_utc=observed_at_utc
            + (
                timedelta(seconds=45)
                if healthy
                else timedelta(minutes=5)
            ),
            healthy=healthy,
            source_snapshot_id=source_id,
            provenance="operations.autonomous_notifications.delivery.v1",
        ),
    )


def deliver_notification_or_fail_closed(
    *,
    plan: AutonomousPaperPlan,
    result: PaperSessionResult,
    observed_at_utc: datetime,
    notifier: AutonomousPaperNotifier,
    push_health_path: Path,
    fail_closed: NotificationFailClosedPort,
) -> NotificationDelivery:
    """Deliver one action notification or immediately unwind/block the plan."""

    try:
        notified = notifier.notify(
            plan,
            result,
            observed_at_utc=observed_at_utc,
        )
    except Exception:
        return _delivery_failure(
            plan=plan,
            observed_at_utc=observed_at_utc,
            event_identity=_notification_key(plan, result),
            push_health_path=push_health_path,
            fail_closed=fail_closed,
            reason="notification_push_failed",
        )
    if not notified:
        return NotificationDelivery(
            notified=False,
            failure_reason=None,
            fail_closed_result=None,
        )
    try:
        record_push_delivery_health(
            push_health_path,
            observed_at_utc=observed_at_utc,
            healthy=True,
            event_identity=_notification_key(plan, result),
        )
    except Exception:
        return _delivery_failure(
            plan=plan,
            observed_at_utc=observed_at_utc,
            event_identity=_notification_key(plan, result),
            push_health_path=push_health_path,
            fail_closed=fail_closed,
            reason="push_health_persistence_failed",
        )
    return NotificationDelivery(
        notified=True,
        failure_reason=None,
        fail_closed_result=None,
    )


def _delivery_failure(
    *,
    plan: AutonomousPaperPlan,
    observed_at_utc: datetime,
    event_identity: str,
    push_health_path: Path,
    fail_closed: NotificationFailClosedPort,
    reason: str,
) -> NotificationDelivery:
    try:
        record_push_delivery_health(
            push_health_path,
            observed_at_utc=observed_at_utc,
            healthy=False,
            event_identity=event_identity,
        )
    except Exception:
        pass
    fail_closed_result = fail_closed.fail_closed_plan(
        plan_id=plan.plan_id,
        observed_at_utc=observed_at_utc,
        reason=reason,
    )
    return NotificationDelivery(
        notified=False,
        failure_reason=reason,
        fail_closed_result=fail_closed_result,
    )


def _notification_key(
    plan: AutonomousPaperPlan,
    result: PaperSessionResult,
) -> str:
    material = "|".join(
        (
            plan.plan_id,
            result.action.value,
            *result.reasons,
            *result.submitted_order_ids,
            *result.flatten_order_ids,
        )
    )
    return hashlib.sha256(material.encode()).hexdigest()


def _require_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("notification timestamp must be UTC")
