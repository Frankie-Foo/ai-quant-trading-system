import hashlib
from pathlib import Path

from operations.loop_integration.outbox import LoopOutbox


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
