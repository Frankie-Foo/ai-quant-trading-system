from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from schedule import premarket
from schedule.premarket import phase_times, target_for_tick
from schedule.state import JobLedger, JobStatus


def test_premarket_phase_times_are_explicit_beijing_deadlines() -> None:
    lock, selection = phase_times(date(2026, 7, 21))
    assert lock == datetime(2026, 7, 21, 0, 0, tzinfo=UTC)
    assert selection == datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


def test_tick_resolves_current_session_after_lock_and_none_before() -> None:
    assert target_for_tick(datetime(2026, 7, 21, 9, 0, tzinfo=UTC)) == date(
        2026, 7, 21
    )
    # Monday lock is not yet due during Sunday Beijing daytime.
    assert target_for_tick(datetime(2026, 7, 19, 4, 0, tzinfo=UTC)) == date(
        2026, 7, 17
    )


def test_abruptly_interrupted_lock_stage_is_recoverable_on_next_tick(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = date(2026, 7, 22)
    now = datetime(2026, 7, 22, 3, 0, tzinfo=UTC)
    state_db = tmp_path / "jobs.sqlite3"
    argv = [
        "--trade-date",
        target.isoformat(),
        "--data-root",
        str(tmp_path / "data"),
        "--state-db",
        str(state_db),
        "--lock-file",
        str(tmp_path / "premarket.lock"),
    ]

    def interrupted(*args: object, **kwargs: object) -> tuple[str, ...]:
        raise KeyboardInterrupt

    monkeypatch.setattr(premarket, "_lock_stage", interrupted)
    with pytest.raises(KeyboardInterrupt):
        premarket.run(argv, now_utc=now)

    monkeypatch.setattr(premarket, "_lock_stage", lambda *args, **kwargs: ("snapshot",))
    assert premarket.run(argv, now_utc=now) == 0
    record = JobLedger(state_db).get(
        premarket.LOCK_JOB,
        target,
        premarket.LOCK_VERSION,
    )
    assert record is not None
    assert record.status is JobStatus.SUCCEEDED
    assert record.attempts == 2
