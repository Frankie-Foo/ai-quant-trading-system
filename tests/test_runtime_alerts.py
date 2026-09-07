import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event

import pytest

from operations.runtime_alerts import RuntimeAlertManager, bounded_retry

NOW = datetime(2026, 8, 24, 14, 0, tzinfo=UTC)


class _Push:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def push(self, body: str) -> str:
        self.messages.append(body)
        return f"message-{len(self.messages)}"


def test_fault_alerts_only_first_third_and_recovery_across_restart(tmp_path: Path) -> None:
    path = tmp_path / "alerts.sqlite3"
    push = _Push()
    manager = RuntimeAlertManager(path, push=push)

    manager.report_failure(
        "alpaca-sip",
        component="Alpaca SIP",
        error_type="TimeoutError",
        observed_at_utc=NOW,
    )
    manager.report_failure(
        "alpaca-sip",
        component="Alpaca SIP",
        error_type="TimeoutError",
        observed_at_utc=NOW + timedelta(seconds=1),
    )
    restarted = RuntimeAlertManager(path, push=push)
    restarted.report_failure(
        "alpaca-sip",
        component="Alpaca SIP",
        error_type="TimeoutError",
        observed_at_utc=NOW + timedelta(seconds=2),
    )
    assert restarted.is_frozen("alpaca-sip")
    restarted.report_recovery(
        "alpaca-sip",
        component="Alpaca SIP",
        observed_at_utc=NOW + timedelta(seconds=3),
    )
    restarted.report_recovery(
        "alpaca-sip",
        component="Alpaca SIP",
        observed_at_utc=NOW + timedelta(seconds=4),
    )

    assert len(push.messages) == 3
    assert "首次故障" in push.messages[0]
    assert "连续第3次" in push.messages[1]
    assert "已恢复" in push.messages[2]
    assert restarted.is_frozen("alpaca-sip")


def test_bounded_retry_stops_after_success_without_self_modification() -> None:
    attempts = 0
    delays: list[float] = []

    def action() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ConnectionError("temporary")
        return "ok"

    assert bounded_retry(action, sleep=delays.append) == "ok"
    assert attempts == 3
    assert delays == [0.25, 0.75]


def test_deferred_reports_never_push_and_preserve_freeze_across_restart(tmp_path: Path) -> None:
    path = tmp_path / "deferred.sqlite3"
    push = _Push()
    manager = RuntimeAlertManager(path, push=push, defer_delivery=True)
    for second in range(3):
        manager.report_failure(
            "alpaca-sip",
            component="Alpaca SIP",
            error_type="TimeoutError",
            observed_at_utc=NOW + timedelta(seconds=second),
        )
    assert manager.is_frozen("alpaca-sip")
    manager.report_recovery(
        "alpaca-sip", component="Alpaca SIP", observed_at_utc=NOW + timedelta(seconds=3)
    )
    restarted = RuntimeAlertManager(path, push=push, defer_delivery=True)
    assert restarted.is_frozen("alpaca-sip")
    restarted.report_recovery("alpaca-sip", component="Alpaca SIP", observed_at_utc=NOW)
    assert push.messages == []


def test_flush_pending_delivers_persisted_messages_in_order_once(tmp_path: Path) -> None:
    path = tmp_path / "deferred.sqlite3"
    push = _Push()
    manager = RuntimeAlertManager(path, push=push, defer_delivery=True)
    for second in range(3):
        manager.report_failure(
            "alpaca-sip",
            component="Alpaca SIP",
            error_type="TimeoutError",
            observed_at_utc=NOW + timedelta(seconds=second),
        )
    manager.report_recovery(
        "alpaca-sip", component="Alpaca SIP", observed_at_utc=NOW + timedelta(seconds=3)
    )
    restarted = RuntimeAlertManager(path, push=push, defer_delivery=True)

    assert restarted.flush_pending() == 3
    assert len(push.messages) == 3
    assert "首次故障" in push.messages[0]
    assert "连续第3次" in push.messages[1]
    assert "已恢复" in push.messages[2]
    assert restarted.flush_pending() == 0
    assert restarted.is_frozen("alpaca-sip")
    with sqlite3.connect(path) as connection:
        deliveries = connection.execute(
            "SELECT status, message_id, message_body FROM runtime_alert_delivery ORDER BY rowid"
        ).fetchall()
    assert deliveries == [
        ("sent", f"message-{index}", body) for index, body in enumerate(push.messages, start=1)
    ]


@pytest.mark.parametrize("deferred", [False, True])
@pytest.mark.parametrize("blank_message_id", [False, True])
def test_ambiguous_delivery_is_never_automatically_resent(
    tmp_path: Path, deferred: bool, blank_message_id: bool
) -> None:
    class AmbiguousPush(_Push):
        def push(self, body: str) -> str:
            super().push(body)
            if blank_message_id:
                return " "
            raise TimeoutError("accepted by remote, but no response received")

    path = tmp_path / "ambiguous.sqlite3"
    push = AmbiguousPush()
    manager = RuntimeAlertManager(path, push=push, defer_delivery=deferred)

    def report() -> None:
        manager.report_failure(
            "alpaca-sip", component="Alpaca SIP", error_type="TimeoutError", observed_at_utc=NOW
        )

    expected_error = RuntimeError if blank_message_id else TimeoutError
    if deferred:
        report()
        assert push.messages == []
        with pytest.raises(expected_error):
            manager.flush_pending()
    else:
        with pytest.raises(expected_error):
            report()
    assert len(push.messages) == 1
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT status, message_id FROM runtime_alert_delivery"
        ).fetchall() == [("sending", None)]

    next_push = _Push()
    restarted = RuntimeAlertManager(path, push=next_push, defer_delivery=True)
    assert restarted.flush_pending() == 0
    restarted.report_recovery("alpaca-sip", component="Alpaca SIP", observed_at_utc=NOW)
    assert restarted.flush_pending() == 1
    assert len(next_push.messages) == 1
    assert "已恢复" in next_push.messages[0]


def test_legacy_delivery_schema_migrates_without_resending_old_events(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE runtime_alert_delivery ("
            "event_key TEXT PRIMARY KEY, status TEXT NOT NULL, message_id TEXT, "
            "updated_at_utc TEXT NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO runtime_alert_delivery VALUES (?, ?, ?, ?)",
            [
                ("old:1:first", "sent", "old-message", NOW.isoformat()),
                ("old:1:escalated", "sending", None, NOW.isoformat()),
            ],
        )
    push = _Push()
    manager = RuntimeAlertManager(path, push=push, defer_delivery=True)
    assert manager.flush_pending() == 0
    manager.report_failure(
        "new", component="Alpaca SIP", error_type="TimeoutError", observed_at_utc=NOW
    )
    assert push.messages == []
    restarted = RuntimeAlertManager(path, push=push, defer_delivery=True)
    assert restarted.flush_pending() == 1
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT event_key, status, message_id, message_body FROM runtime_alert_delivery "
            "WHERE event_key LIKE 'old:%' ORDER BY rowid"
        ).fetchall() == [
            ("old:1:first", "sent", "old-message", ""),
            ("old:1:escalated", "sending", None, ""),
        ]


def test_slow_flush_does_not_block_fault_state_or_claim_same_event_twice(tmp_path: Path) -> None:
    entered = Event()
    release = Event()

    class SlowPush(_Push):
        def push(self, body: str) -> str:
            message_id = super().push(body)
            entered.set()
            if not release.wait(timeout=5):
                raise TimeoutError("test did not release fake delivery")
            return message_id

    path = tmp_path / "concurrent.sqlite3"
    push = SlowPush()
    manager = RuntimeAlertManager(path, push=push, defer_delivery=True)
    observer = RuntimeAlertManager(path, push=push, defer_delivery=True)
    manager.report_failure(
        "alpaca-sip", component="Alpaca SIP", error_type="TimeoutError", observed_at_utc=NOW
    )

    def update_fault_while_delivery_blocked() -> None:
        assert observer.flush_pending() == 0
        for second in (1, 2):
            observer.report_failure(
                "alpaca-sip",
                component="Alpaca SIP",
                error_type="TimeoutError",
                observed_at_utc=NOW + timedelta(seconds=second),
            )
        assert observer.is_frozen("alpaca-sip")
        observer.report_recovery(
            "alpaca-sip", component="Alpaca SIP", observed_at_utc=NOW + timedelta(seconds=3)
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        delivery = executor.submit(manager.flush_pending)
        try:
            assert entered.wait(timeout=2)
            executor.submit(update_fault_while_delivery_blocked).result(timeout=2)
            with sqlite3.connect(path) as connection:
                assert connection.execute(
                    "SELECT status FROM runtime_alert_delivery ORDER BY rowid"
                ).fetchall() == [("sending",), ("pending",), ("pending",)]
            assert len(push.messages) == 1
        finally:
            release.set()
        assert delivery.result(timeout=2) == 1
    assert observer.is_frozen("alpaca-sip")
    assert observer.flush_pending() == 2
    assert len(push.messages) == 3
    assert observer.flush_pending() == 0
