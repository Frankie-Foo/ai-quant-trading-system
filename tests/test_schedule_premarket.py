from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast

import pytest

from operations.feishu_base import FeishuBaseEventClient
from schedule import premarket
from schedule.premarket import phase_times, recovery_due, target_for_tick
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


def test_recovery_shadow_waits_for_six_complete_five_minute_bars() -> None:
    assert recovery_due(date(2026, 7, 21)) == datetime(
        2026, 7, 21, 14, 5, tzinfo=UTC
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


def test_shadow_failure_does_not_invalidate_primary_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = date(2026, 7, 22)
    now = datetime(2026, 7, 22, 13, 0, tzinfo=UTC)
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
    monkeypatch.setattr(
        premarket,
        "_lock_stage",
        lambda *args, **kwargs: ("lock-snapshot",),
    )
    monkeypatch.setattr(
        premarket,
        "_selection_stage",
        lambda *args, **kwargs: ("selection-snapshot",),
    )

    def failed_shadow(*args: object, **kwargs: object) -> tuple[str, ...]:
        raise RuntimeError("shadow unavailable")

    monkeypatch.setattr(premarket, "_shadow_stage", failed_shadow)

    assert premarket.run(argv, now_utc=now) == 0
    ledger = JobLedger(state_db)
    primary = ledger.get(
        premarket.SELECTION_JOB,
        target,
        premarket.SELECTION_VERSION,
    )
    shadow = ledger.get(
        premarket.SHADOW_JOB,
        target,
        premarket.SHADOW_VERSION,
    )
    assert primary is not None and primary.status is JobStatus.SUCCEEDED
    assert shadow is not None and shadow.status is JobStatus.FAILED


def test_completed_selection_projects_to_feishu_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, date, float]] = []
    selection = object()

    def fake_load(
        data_root: object,
        trade_date: date,
        min_rvol: float,
    ) -> object:
        calls.append((data_root, trade_date, min_rvol))
        return selection

    monkeypatch.setattr(
        premarket,
        "load_locked_selection",
        fake_load,
    )
    recorded: list[tuple[object, object, datetime]] = []
    monkeypatch.setattr(
        premarket,
        "record_locked_selection",
        lambda client, value, observed_at_utc: recorded.append(
            (client, value, observed_at_utc)
        ),
    )

    premarket._project_selection_event(
        cast(FeishuBaseEventClient, object()),
        trade_date=date(2026, 7, 22),
        data_root=tmp_path / "data",
        observed_at_utc=datetime(2026, 7, 22, 13, 0, tzinfo=UTC),
    )

    assert len(calls) == 1
    assert recorded == [
        (
            recorded[0][0],
            selection,
            datetime(2026, 7, 22, 13, 0, tzinfo=UTC),
        )
    ]


def test_selection_stage_cannot_succeed_without_accepted_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(premarket, "_run", lambda *args, **kwargs: ("artifact",))

    with pytest.raises(FileNotFoundError, match="no accepted locked selection"):
        premarket._selection_stage(
            date(2026, 7, 22),
            tmp_path / "data",
            logger=JsonEventLogger(service="test"),
        )
