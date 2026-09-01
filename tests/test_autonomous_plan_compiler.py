from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl
import pytest

from data_plane.storage import persist_snapshot
from operations.autonomous_paper_config import load_autonomous_paper_config
from operations.autonomous_plan_compiler import (
    compile_autonomous_paper_plan,
    compile_autonomous_paper_plans,
)

TRADE_DATE = date(2026, 7, 31)


def _persist_selection(root: Path, rows: dict[str, object]) -> None:
    persist_snapshot(
        pl.DataFrame(rows),
        root=root,
        source="kernel.universe.selection_gates",
        schema_version="selection_gates.v2",
        checks=(),
    )


def _rows() -> dict[str, object]:
    return {
        "symbol": ["SECOND", "FIRST"],
        "session_date": [TRADE_DATE, TRADE_DATE],
        "selection_rank": [2, 1],
        "pass_gate": [True, True],
        "rvol": [5.0, 12.0],
        "price": [80.0, 100.0],
        "premarket_close": [82.0, 102.0],
        "premarket_above_vwap": [True, True],
        "directional_volume_confirmed": [True, True],
        "earnings_intensity_score": [76.0, 88.0],
        "gate_asof_utc": [
            datetime(2026, 7, 31, 12, 0, tzinfo=UTC),
            datetime(2026, 7, 31, 12, 0, tzinfo=UTC),
        ],
    }


def test_compiler_freezes_top_current_selection_into_one_paper_plan(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    output_path = tmp_path / "runs" / "paper-autopilot" / "approved.json"
    _persist_selection(data_root, _rows())

    prepared = compile_autonomous_paper_plan(
        data_root=data_root,
        trade_date=TRADE_DATE,
        output_path=output_path,
    )

    config = load_autonomous_paper_config(output_path)
    bundle = config.plans[0]
    assert prepared.symbol == "FIRST"
    assert prepared.selection_snapshot_id in bundle.plan.source_snapshot_ids
    assert bundle.plan.plan_id == "auto-20260731-FIRST"
    assert str(bundle.plan.reference_price) == "102.00"
    assert str(bundle.plan.hard_stop) == "99.96"
    assert str(bundle.plan.max_notional_fraction) == "0.10"
    assert str(bundle.plan.full_risk_fraction) == "0.0035"
    assert bundle.benchmark_symbol == "SPY"
    assert bundle.sector_symbol == "N/A"
    assert bundle.evidence.catalyst.value == 88.0
    assert bundle.evidence.first_target_reward_r == 2.5
    assert bundle.evidence.weighted_expected_reward_r == 3.0
    raw = output_path.read_text(encoding="utf-8").lower()
    assert "secret" not in raw
    assert "api_key" not in raw


def test_compiler_marks_missing_observations_unavailable_instead_of_inventing_them(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    rows = _rows()
    rows["earnings_intensity_score"] = [None, None]
    _persist_selection(data_root, rows)

    compile_autonomous_paper_plan(
        data_root=data_root,
        trade_date=TRADE_DATE,
        output_path=tmp_path / "approved.json",
    )

    bundle = load_autonomous_paper_config(tmp_path / "approved.json").plans[0]
    assert bundle.evidence.catalyst.value is None
    assert bundle.sector_symbol == "N/A"
    assert "unavailable" in bundle.evidence.catalyst.provenance
    assert "fallback" not in bundle.market_context_provenance


def test_compiler_rejects_incomplete_survivor_instead_of_inventing_plan(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    rows = _rows()
    rows["premarket_above_vwap"] = [True, False]
    _persist_selection(data_root, rows)

    with pytest.raises(ValueError, match="no eligible"):
        compile_autonomous_paper_plan(
            data_root=data_root,
            trade_date=TRADE_DATE,
            output_path=tmp_path / "approved.json",
        )


def test_compiler_can_freeze_ranked_candidates_for_autonomous_monitoring(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    output_path = tmp_path / "runs" / "autonomous" / "paper.json"
    _persist_selection(data_root, _rows())

    prepared = compile_autonomous_paper_plans(
        data_root=data_root,
        trade_date=TRADE_DATE,
        output_path=output_path,
    )

    config = load_autonomous_paper_config(output_path)
    assert [item.symbol for item in prepared] == ["FIRST", "SECOND"]
    assert config.poll_seconds == 1
    assert [bundle.plan.symbol for bundle in config.plans] == ["FIRST", "SECOND"]
    assert config.plans[0].safety_envelope_path == (
        output_path.parent / "safety" / "auto-20260731-FIRST.json"
    )


def test_compiler_refuses_stale_selection_snapshot(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    _persist_selection(data_root, _rows())

    with pytest.raises(FileNotFoundError, match="current accepted selection"):
        compile_autonomous_paper_plan(
            data_root=data_root,
            trade_date=date(2026, 8, 3),
            output_path=tmp_path / "approved.json",
        )
