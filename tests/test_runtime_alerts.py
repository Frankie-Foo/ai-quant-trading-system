from datetime import UTC, datetime, timedelta
from pathlib import Path

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
