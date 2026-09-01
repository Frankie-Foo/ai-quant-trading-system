"""Restart-safe Chinese notifications for autonomous Alpaca Paper actions."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from db.migrations.sqlite import SQLiteMigration, apply_sqlite_migrations
from execution.autonomous_paper_session import (
    AutonomousPaperPlan,
    PaperSessionResult,
    SessionAction,
)
from operations.feishu_base import FeishuBaseError, InvestmentTable
from operations.feishu_investment_events import (
    record_autonomous_monitor_trigger,
    record_autonomous_trade,
)
from operations.runtime_agent_safety import (
    PushHealthEvidence,
    write_push_health_evidence,
)

LOGGER = logging.getLogger(__name__)


class PushPort(Protocol):
    def push(self, body: str) -> str: ...


class FeishuBasePort(Protocol):
    def record_event(
        self,
        table: InvestmentTable,
        event_id: str,
        fields: Mapping[str, object],
    ) -> str: ...


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


class NotificationInFlightError(RuntimeError):
    """An earlier process may have pushed this event but did not finish auditing it."""


def _create_autonomous_notifications(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS autonomous_notifications (
            notification_key TEXT PRIMARY KEY,
            message_id TEXT NOT NULL,
            sent_at_utc TEXT NOT NULL,
            message_body TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'sent',
            claimed_at_utc TEXT NOT NULL DEFAULT ''
        )
        """
    )


def _add_autonomous_notification_columns(connection: sqlite3.Connection) -> None:
    columns = {
        str(row[1])
        for row in connection.execute(
            "PRAGMA table_info(autonomous_notifications)"
        ).fetchall()
    }
    additions = (
        ("message_body", "TEXT NOT NULL DEFAULT ''"),
        ("payload_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("status", "TEXT NOT NULL DEFAULT 'sent'"),
        ("claimed_at_utc", "TEXT NOT NULL DEFAULT ''"),
    )
    for name, definition in additions:
        if name not in columns:
            connection.execute(
                f"ALTER TABLE autonomous_notifications ADD COLUMN {name} {definition}"
            )


AUTONOMOUS_NOTIFICATION_MIGRATIONS = (
    SQLiteMigration(
        version=1,
        name="autonomous_notifications",
        signature="autonomous_notifications.v1",
        apply=_create_autonomous_notifications,
    ),
    SQLiteMigration(
        version=2,
        name="autonomous_notification_delivery_columns",
        signature="autonomous_notifications.delivery_columns.v1",
        apply=_add_autonomous_notification_columns,
    ),
)


class AutonomousNotificationLedger:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            apply_sqlite_migrations(
                connection,
                owner="operations.autonomous_notifications",
                migrations=AUTONOMOUS_NOTIFICATION_MIGRATIONS,
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
                WHERE notification_key=? AND status='sent'
                """,
                (notification_key,),
            ).fetchone()
        return row is not None

    def claim(self, notification_key: str, *, claimed_at_utc: datetime) -> str:
        """Claim an event before an external push; never reclaim unknown success."""

        _require_utc(claimed_at_utc)
        if not notification_key.strip():
            raise ValueError("notification identity is incomplete")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM autonomous_notifications "
                "WHERE notification_key=?",
                (notification_key,),
            ).fetchone()
            if row is not None:
                status = str(row[0])
                return "sent" if status == "sent" else "in_flight"
            connection.execute(
                """
                INSERT INTO autonomous_notifications (
                    notification_key, message_id, sent_at_utc, message_body,
                    payload_json, status, claimed_at_utc
                ) VALUES (?, '', '', '', '{}', 'claimed', ?)
                """,
                (notification_key, claimed_at_utc.isoformat()),
            )
        return "claimed"

    def release_claim(self, notification_key: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM autonomous_notifications "
                "WHERE notification_key=? AND status='claimed'",
                (notification_key,),
            )

    def record(
        self,
        notification_key: str,
        *,
        message_id: str,
        sent_at_utc: datetime,
        message_body: str,
        payload: Mapping[str, object],
    ) -> None:
        _require_utc(sent_at_utc)
        if (
            not notification_key.strip()
            or not message_id.strip()
            or not message_body.strip()
        ):
            raise ValueError("notification identity is incomplete")
        payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        with self._connect() as connection:
            updated = connection.execute(
                """
                UPDATE autonomous_notifications
                SET message_id=?, sent_at_utc=?, message_body=?,
                    payload_json=?, status='sent'
                WHERE notification_key=?
                """,
                (
                    message_id,
                    sent_at_utc.isoformat(),
                    message_body,
                    payload_json,
                    notification_key,
                ),
            ).rowcount
            if updated == 0:
                connection.execute(
                    """
                    INSERT INTO autonomous_notifications (
                        notification_key, message_id, sent_at_utc, message_body,
                        payload_json, status, claimed_at_utc
                    ) VALUES (?, ?, ?, ?, ?, 'sent', ?)
                    """,
                    (
                        notification_key,
                        message_id,
                        sent_at_utc.isoformat(),
                        message_body,
                        payload_json,
                        sent_at_utc.isoformat(),
                    ),
                )

    def list_records(self) -> tuple[dict[str, object], ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT notification_key, message_id, sent_at_utc,
                       message_body, payload_json
                FROM autonomous_notifications
                ORDER BY sent_at_utc, notification_key
                """
            ).fetchall()
        records: list[dict[str, object]] = []
        for row in rows:
            payload = json.loads(str(row[4]))
            if not isinstance(payload, dict):
                raise RuntimeError("notification audit payload is not an object")
            records.append(
                {
                    "notification_key": str(row[0]),
                    "message_id": str(row[1]),
                    "sent_at_utc": str(row[2]),
                    "message_body": str(row[3]),
                    "payload": payload,
                }
            )
        return tuple(records)


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
        base: FeishuBasePort | None = None,
    ):
        self.push = push
        self.ledger = ledger
        self.base = base

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
        claim_status = self.ledger.claim(
            notification_key,
            claimed_at_utc=observed_at_utc,
        )
        if claim_status == "sent":
            return False
        if claim_status == "in_flight":
            raise NotificationInFlightError(
                f"notification delivery is already in flight: {notification_key}"
            )
        daily_return = result.daily_return * 100
        stop_return = (
            (plan.hard_stop / plan.reference_price) - 1
        ) * 100
        reason_text = "、".join(result.reasons) if result.reasons else "无"
        order_ids = (
            *result.submitted_order_ids,
            *result.cancelled_order_ids,
            *result.flatten_order_ids,
        )
        order_text = ",".join(order_ids) or "none"
        message = (
            f"plan_id={plan.plan_id}\n"
            f"provenance={plan.provenance}\n"
            f"order_ids={order_text}\n"
            f"【量化系统｜模拟盘｜{plan.symbol}】\n"
            f"动作：{action_name}\n"
            f"当日收益率：{daily_return:+.2f}%\n"
            f"初始保护幅度：{stop_return:+.2f}%\n"
            f"原因：{reason_text}\n"
            f"当日锁定：{'是' if result.day_locked else '否'}"
        )
        if self.base is not None:
            try:
                record_autonomous_monitor_trigger(
                    self.base,
                    plan,
                    result,
                    observed_at_utc=observed_at_utc,
                    action_name=action_name,
                    message=message,
                )
                record_autonomous_trade(
                    self.base,
                    plan,
                    result,
                    observed_at_utc=observed_at_utc,
                    action_name=action_name,
                    message=message,
                )
            except FeishuBaseError as exc:
                # Base is an audit projection, not an execution dependency.
                LOGGER.warning(
                    "feishu investment audit write failed: %s",
                    type(exc).__name__,
                )
        try:
            message_id = self.push.push(message)
        except Exception:
            self.ledger.release_claim(notification_key)
            raise
        self.ledger.record(
            notification_key,
            message_id=message_id,
            sent_at_utc=observed_at_utc,
            message_body=message,
            payload={
                "plan_id": plan.plan_id,
                "symbol": plan.symbol,
                "action": result.action.value,
                "reasons": list(result.reasons),
                "provenance": result.provenance,
                "submitted_order_ids": list(result.submitted_order_ids),
                "cancelled_order_ids": list(result.cancelled_order_ids),
                "flatten_order_ids": list(result.flatten_order_ids),
            },
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
    except NotificationInFlightError:
        return _delivery_failure(
            plan=plan,
            observed_at_utc=observed_at_utc,
            event_identity=_notification_key(plan, result),
            push_health_path=push_health_path,
            fail_closed=fail_closed,
            reason="notification_delivery_in_flight",
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
            *result.cancelled_order_ids,
            *result.flatten_order_ids,
        )
    )
    return hashlib.sha256(material.encode()).hexdigest()


def _require_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("notification timestamp must be UTC")
