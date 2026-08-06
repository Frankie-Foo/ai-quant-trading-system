from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from execution.session_ledger import (
    PaperSessionLedger,
    PaperSessionStatus,
)

START = datetime(2026, 7, 21, 13, 25, tzinfo=UTC)
CLOSE = datetime(2026, 7, 21, 20, 0, tzinfo=UTC)


def test_complete_session_is_durable_and_cannot_be_restarted(tmp_path: Path) -> None:
    ledger = PaperSessionLedger(tmp_path / "orders.sqlite3")
    ledger.start(
        trade_date=date(2026, 7, 21),
        started_at_utc=START,
        expected_close_utc=CLOSE,
        reconciliation_match_rate=1.0,
    )
    ledger.finish(
        trade_date=date(2026, 7, 21),
        ended_at_utc=CLOSE,
        status=PaperSessionStatus.COMPLETED,
        event_count=100,
        orders_submitted=2,
    )

    record = ledger.records()[0]
    assert record.status is PaperSessionStatus.COMPLETED
    assert record.event_count == 100
    with pytest.raises(RuntimeError, match="cannot be restarted"):
        ledger.start(
            trade_date=date(2026, 7, 21),
            started_at_utc=START,
            expected_close_utc=CLOSE,
            reconciliation_match_rate=1.0,
        )


def test_interrupted_session_can_restart_then_record_failure(tmp_path: Path) -> None:
    ledger = PaperSessionLedger(tmp_path / "orders.sqlite3")
    for _ in range(2):
        ledger.start(
            trade_date=date(2026, 7, 21),
            started_at_utc=START,
            expected_close_utc=CLOSE,
            reconciliation_match_rate=0.75,
        )
    ledger.finish(
        trade_date=date(2026, 7, 21),
        ended_at_utc=START,
        status=PaperSessionStatus.FAILED,
        event_count=4,
        orders_submitted=0,
        error_type="ConnectionError",
    )
    assert ledger.records()[0].error_type == "ConnectionError"

