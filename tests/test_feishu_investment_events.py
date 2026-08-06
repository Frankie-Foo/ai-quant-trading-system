from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from hashlib import sha256

from data_plane.contracts import DatasetSnapshot
from execution.locked_selection import LockedCandidate, LockedSelection
from operations.feishu_base import InvestmentTable
from operations.feishu_investment_events import (
    record_locked_selection,
    record_postmarket_review,
)

NOW = datetime(2026, 8, 6, 14, 0, tzinfo=UTC)


class FakeEventWriter:
    def __init__(self) -> None:
        self.events: list[tuple[InvestmentTable, str, Mapping[str, object]]] = []

    def record_event(
        self,
        table: InvestmentTable,
        event_id: str,
        fields: Mapping[str, object],
    ) -> str:
        self.events.append((table, event_id, fields))
        return f"rec-{len(self.events)}"


def _selection() -> LockedSelection:
    return LockedSelection(
        trade_date=date(2026, 8, 6),
        snapshot=DatasetSnapshot(
            dataset_id="selection-snapshot-1",
            source="alpaca.sip",
            asof_utc=NOW,
            content_sha256=sha256(b"selection").hexdigest(),
            schema_version="selection_gates.v2",
            row_count=1,
        ),
        candidates=(
            LockedCandidate(
                symbol="NVDA",
                selection_rank=1,
                rvol=4.2,
                price=100.0,
                adv_usd=20_000_000,
                atr_pct=0.05,
                tier="mega",
            ),
        ),
    )


def test_selection_records_candidates_with_provenance_but_not_polls() -> None:
    writer = FakeEventWriter()

    record_ids = record_locked_selection(writer, _selection(), observed_at_utc=NOW)

    assert record_ids == ("rec-1",)
    table, event_id, fields = writer.events[0]
    assert table is InvestmentTable.SELECTION
    assert event_id == "selection:2026-08-06:selection-snapshot-1:NVDA"
    assert fields["选择快照ID"] == "selection-snapshot-1"
    assert "sha256=" in str(fields["来源"])
    assert "轮询" not in str(fields["消息正文"])


def test_postmarket_review_records_all_evidence_once() -> None:
    writer = FakeEventWriter()

    record_id = record_postmarket_review(
        writer,
        trade_date=date(2026, 8, 6),
        program_review_id="program-review-1",
        selection_review_id="selection-review-1",
        evidence_ids=("signal-1", "episode-1", "program-review-1"),
        observed_at_utc=NOW,
    )

    assert record_id == "rec-1"
    table, event_id, fields = writer.events[0]
    assert table is InvestmentTable.REVIEW
    assert event_id == "review:2026-08-06:program-review-1:selection-review-1"
    assert fields["复盘类型"] == "收盘复盘"
    assert fields["关联证据"] == "signal-1|episode-1|program-review-1"
