from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

import pytest

from schedule.runtime import JsonEventLogger, LockUnavailableError, ProcessLock


def test_process_lock_rejects_a_concurrent_owner(tmp_path: Path) -> None:
    path = tmp_path / "postmarket.lock"
    with ProcessLock(path):
        with pytest.raises(LockUnavailableError):
            with ProcessLock(path):
                raise AssertionError("second owner must not enter")
    with ProcessLock(path):
        pass


def test_json_event_logger_emits_one_utc_record() -> None:
    stream = StringIO()
    logger = JsonEventLogger(stream=stream, service="postmarket")
    logger.emit("job_completed", trade_date="2026-07-20", artifact_count=3)

    lines = stream.getvalue().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["service"] == "postmarket"
    assert payload["event"] == "job_completed"
    assert payload["trade_date"] == "2026-07-20"
    assert payload["artifact_count"] == 3
    assert payload["ts_utc"].endswith("+00:00")
