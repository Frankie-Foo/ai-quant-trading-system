from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest

from data_plane.storage import persist_snapshot
from operations.autonomous_selection_handoff import (
    create_open_confirmation,
    format_selection_plan_message,
    load_open_confirmation,
    prepare_autonomous_selection_handoff,
)
from operations.feishu_base import InvestmentTable


class _Push:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def push(self, body: str) -> str:
        self.messages.append(body)
        return "message-1"


class _Audit:
    def __init__(self) -> None:
        self.records: list[str] = []

    def record_event(
        self,
        table: InvestmentTable,
        event_id: str,
        fields: Mapping[str, object],
    ) -> str:
        del table, fields
        record_id = f"record-{len(self.records) + 1}"
        self.records.append(f"{event_id}:{record_id}")
        return record_id


def test_selection_message_lists_only_symbols_entering_autonomous_monitoring() -> None:
    selection = SimpleNamespace(
        trade_date=date(2026, 8, 11),
        snapshot=SimpleNamespace(dataset_id="selection-snapshot"),
        symbols=("FIRST", "SECOND"),
        candidates=(
            SimpleNamespace(
                selection_rank=1,
                symbol="FIRST",
                rvol=7.2,
                premarket_return=0.12,
                premarket_close=10.5,
                price=10.0,
            ),
            SimpleNamespace(
                selection_rank=2,
                symbol="SECOND",
                rvol=5.1,
                premarket_return=None,
                premarket_close=None,
                price=20.0,
            ),
        ),
    )

    message = format_selection_plan_message(
        selection,  # type: ignore[arg-type]
        config_path=Path("runs/autonomous/2026-08-11/autonomous_paper.json"),
        symbols=("FIRST",),
    )

    assert "FIRST" in message
    assert "SECOND" not in message
    assert "实际模拟买入或卖出" in message
    assert "盘前 +12.00%" in message


def test_selection_message_renders_missing_premarket_facts_as_na() -> None:
    selection = SimpleNamespace(
        trade_date=date(2026, 8, 11),
        snapshot=SimpleNamespace(dataset_id="selection-snapshot"),
        symbols=("SECOND",),
        candidates=(
            SimpleNamespace(
                selection_rank=1,
                symbol="SECOND",
                rvol=5.1,
                premarket_return=None,
                premarket_close=None,
                price=20.0,
            ),
        ),
    )

    message = format_selection_plan_message(
        selection,  # type: ignore[arg-type]
        config_path=Path("autonomous_paper.json"),
    )

    assert "盘前 N/A" in message
    assert "盘前 +0.00%" not in message


def test_handoff_publishes_once_and_freezes_ranked_paper_plans(tmp_path: Path) -> None:
    trade_date = date(2026, 8, 11)
    persist_snapshot(
        pl.DataFrame(
            {
                "symbol": ["FIRST", "SECOND"],
                "session_date": [trade_date, trade_date],
                "selection_rank": [1, 2],
                "pass_gate": [True, True],
                "rvol": [8.0, 5.0],
                "price": [10.0, 20.0],
                "premarket_close": [10.5, 20.5],
                "premarket_above_vwap": [True, True],
                "directional_volume_confirmed": [True, True],
                "earnings_intensity_score": [90.0, 80.0],
                "gate_asof_utc": [
                    datetime(2026, 8, 11, 12, tzinfo=UTC),
                    datetime(2026, 8, 11, 12, tzinfo=UTC),
                ],
                "adv_usd": [10_000_000.0, 10_000_000.0],
                "atr_pct": [0.05, 0.05],
                "tier": ["small", "small"],
            }
        ),
        root=tmp_path / "data",
        source="kernel.universe.selection_gates",
        schema_version="selection_gates.v2",
        checks=(),
    )
    push = _Push()
    first = prepare_autonomous_selection_handoff(
        data_root=tmp_path / "data",
        trade_date=trade_date,
        output_path=tmp_path / "state" / "autonomous_paper.json",
        notification_db=tmp_path / "state" / "notifications.sqlite3",
        push=push,
        observed_at_utc=datetime(2026, 8, 11, 12, 1, tzinfo=UTC),
    )
    second = prepare_autonomous_selection_handoff(
        data_root=tmp_path / "data",
        trade_date=trade_date,
        output_path=tmp_path / "state" / "autonomous_paper.json",
        notification_db=tmp_path / "state" / "notifications.sqlite3",
        push=push,
        observed_at_utc=datetime(2026, 8, 11, 12, 2, tzinfo=UTC),
    )

    assert first.symbols == ("FIRST", "SECOND")
    assert first.message_id == "message-1"
    assert second.message_id is None
    assert first.authorization is None
    assert len(push.messages) == 1


def test_handoff_requires_audit_and_push_receipts_before_open_authorization(
    tmp_path: Path,
) -> None:
    trade_date = date(2026, 8, 11)
    persist_snapshot(
        pl.DataFrame(
            {
                "symbol": ["FIRST"],
                "session_date": [trade_date],
                "selection_rank": [1],
                "pass_gate": [True],
                "rvol": [8.0],
                "price": [10.0],
                "premarket_close": [10.5],
                "premarket_above_vwap": [True],
                "directional_volume_confirmed": [True],
                "earnings_intensity_score": [90.0],
                "gate_asof_utc": [datetime(2026, 8, 11, 13, 35, tzinfo=UTC)],
                "adv_usd": [10_000_000.0],
                "atr_pct": [0.05],
                "tier": ["small"],
            }
        ),
        root=tmp_path / "data",
        source="kernel.universe.selection_gates",
        schema_version="selection_gates.v2",
        checks=(),
    )
    confirmation_path = tmp_path / "state" / "open_confirmation.json"
    receipt = prepare_autonomous_selection_handoff(
        data_root=tmp_path / "data",
        trade_date=trade_date,
        output_path=tmp_path / "state" / "autonomous_paper.json",
        confirmation_path=confirmation_path,
        notification_db=tmp_path / "state" / "notifications.sqlite3",
        push=_Push(),
        audit=_Audit(),
        observed_at_utc=datetime(2026, 8, 11, 13, 36, tzinfo=UTC),
        strategy_version="modern-h15.v2",
    )

    assert receipt.authorization is not None
    assert receipt.authorization.candidate_pool == ("FIRST",)
    assert receipt.authorization.strategy_version == "modern-h15.v2"
    confirmation = load_open_confirmation(confirmation_path)
    assert confirmation.authorization == receipt.authorization
    assert confirmation.config_path == receipt.config_path


def test_existing_open_confirmation_rejects_changed_plan_bytes(tmp_path: Path) -> None:
    plan = tmp_path / "plan.json"
    confirmation = tmp_path / "open.json"
    plan.write_text('{"version": 1}', encoding="utf-8")
    generated_at = datetime(2026, 8, 24, 13, 35, tzinfo=UTC)
    create_open_confirmation(
        confirmation_path=confirmation,
        config_path=plan,
        trade_date=date(2026, 8, 24),
        selection_snapshot_id="selection-1",
        candidate_pool=("ABC",),
        feishu_record_ids=("record-1",),
        livermore_message_id="message-1",
        strategy_version="modern-h15.v1",
        generated_at_utc=generated_at,
    )
    plan.write_text('{"version": 2}', encoding="utf-8")

    with pytest.raises(ValueError, match="config hash"):
        create_open_confirmation(
            confirmation_path=confirmation,
            config_path=plan,
            trade_date=date(2026, 8, 24),
            selection_snapshot_id="selection-1",
            candidate_pool=("ABC",),
            feishu_record_ids=("record-1",),
            livermore_message_id="message-1",
            strategy_version="modern-h15.v1",
            generated_at_utc=generated_at,
        )
