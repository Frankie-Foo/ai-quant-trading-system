import hashlib
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from operations.loop_integration.outbox import LoopOutbox


def test_submission_intents_survive_timeout_and_exclude_concurrent_post(tmp_path: Path) -> None:
    outbox = LoopOutbox(tmp_path / "outbox.sqlite3")
    outbox.stage(event_id="test", event_type="daily_review", payload={}, payload_sha256="test")
    outbox.checkpoint("test", "creating_task", None, None)
    outbox.record_error("test", "TimeoutError")
    restarted = LoopOutbox(outbox.path)
    with pytest.raises(RuntimeError, match="already claimed"):
        restarted.checkpoint("test", "creating_task", None, None)
    with pytest.raises(RuntimeError, match="claimed"):
        restarted.mark_blocked_precondition("test", error_code="LATE_PRECONDITION_FAILURE")
    with pytest.raises(RuntimeError, match="claimed"):
        restarted.mark_audit_only_backfill("test", error_code="LATE_BACKFILL_CHECK")
    restarted.checkpoint("test", "task_created", "task-1", None)
    restarted.checkpoint("test", "starting_run", "task-1", None)
    with pytest.raises(RuntimeError, match="already claimed"):
        restarted.checkpoint("test", "starting_run", "task-1", None)
    restarted.checkpoint("test", "remote_processing", "task-1", "run-1")
    restarted.record_error("test", "HTTP403")
    item = restarted.get("test")
    assert item and item.status == "remote_processing"
    assert (item.remote_task_id, item.remote_run_id) == ("task-1", "run-1")


def test_backoff_and_date_lookup_do_not_need_current_inputs(tmp_path: Path) -> None:
    box = LoopOutbox(tmp_path / "loop.sqlite3")
    box.stage(event_id="frozen", event_type="daily_review",
              payload={"trading_date": "2026-09-22"}, payload_sha256="test")
    box.mark_remote_processing("frozen", remote_task_id="task", remote_run_id="run")
    assert box.submitted_review(date(2026, 9, 22)) is not None
    now = datetime(2026, 9, 22, 20, tzinfo=UTC)
    for minutes in (1, 5, 15, 60, 60):
        box.defer_retry("frozen", "HTTPError", now=now)
        assert not box.recoverable_reviews(now=now)
        now += timedelta(minutes=minutes)
        assert len(box.recoverable_reviews(now=now)) == 1


def test_changed_evidence_cannot_race_second_daily_submission(tmp_path: Path) -> None:
    box = LoopOutbox(tmp_path / "loop.sqlite3")
    for event_id in ("policy-A", "policy-B"):
        box.stage(event_id=event_id, event_type="daily_review",
                  payload={"trading_date": "2026-09-22"}, payload_sha256=event_id)
    box.checkpoint("policy-A", "creating_task", None, None)
    with pytest.raises(RuntimeError, match="different evidence"):
        box.checkpoint("policy-B", "creating_task", None, None)


@pytest.mark.parametrize("terminal", ["delivered", "remote_rejected"])
def test_late_poll_cannot_regress_terminal_state(tmp_path: Path, terminal: str) -> None:
    box = LoopOutbox(tmp_path / "loop.sqlite3")
    box.stage(event_id="test", event_type="daily_review", payload={}, payload_sha256="test")
    box.mark_remote_processing("test", remote_task_id="task", remote_run_id="run")
    if terminal == "delivered":
        box.mark_delivered("test", remote_task_id="task", remote_run_id="run")
    else:
        box.mark_remote_rejected("test", error_code="REMOTE_FAILED")
    box.mark_remote_processing("test", remote_task_id="task", remote_run_id="run")
    box.defer_retry("test", "LATE_RESPONSE", now=datetime.now(UTC))
    current = box.get("test")
    assert current and current.status == terminal and current.retry_after_utc is None


def test_event_review_outbox_persists_remote_processing_then_completion(tmp_path: Path) -> None:
    outbox = LoopOutbox(tmp_path / "loop.sqlite3")
    payload = {"schema_version": "ai_quant.event_loop_review.v1", "trade_date": "2026-09-14"}
    digest = hashlib.sha256(b"event-review").hexdigest()

    staged = outbox.stage(
        event_id="event-review-2026-09-14-v1",
        event_type="event_review",
        payload=payload,
        payload_sha256=digest,
    )
    assert staged.status == "pending"

    outbox.mark_remote_processing(
        staged.event_id,
        remote_task_id="task-1",
        remote_run_id="run-1",
    )
    processing = outbox.get(staged.event_id)
    assert processing is not None
    assert processing.status == "remote_processing"
    assert processing.remote_task_id == "task-1"
    assert outbox.pending() == ()

    outbox.mark_remote_completed(staged.event_id, remote_task_id="task-1", remote_run_id="run-1")
    completed = outbox.get(staged.event_id)
    assert completed is not None
    assert completed.status == "remote_completed"
    assert completed.attempts == 2
