from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import polars as pl

from data_plane.storage import persist_snapshot
from scripts.evaluate_strategy_shadow import evaluate_shadow


def test_shadow_evaluation_compares_frozen_lists_without_orders(tmp_path: Path) -> None:
    first_wave = tmp_path / "runs/autonomous/2026-08-26/first_wave_pool.json"
    first_wave.parent.mkdir(parents=True)
    first_wave.write_text(
        json.dumps(
            {
                "trade_date": "2026-08-26",
                "candidates": [{"symbol": "A"}, {"symbol": "B"}, {"symbol": "C"}],
                "strategy_context": {
                    "active_version": "selection-baseline",
                    "active_policy_hash": "b" * 64,
                    "challenger": {
                        "version": "challenger-test",
                        "policy_hash": "a" * 64,
                        "symbols": ["A", "C"],
                        "execution_eligible": False,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    postmortem = pl.DataFrame(
        {
            "session_date": [date(2026, 8, 26)] * 8,
            "symbol": ["A", "X", *[f"M{i}" for i in range(6)]],
            "close_return": [0.2, 0.1, *([0.05] * 6)],
        }
    )
    snapshot, path = persist_snapshot(
        postmortem,
        root=tmp_path / "data",
        source="research.intraday_selection_postmortem",
        schema_version="intraday_selection_postmortem.v1",
        checks=(),
    )

    frame = evaluate_shadow(first_wave, path, snapshot, trade_date=date(2026, 8, 26))

    assert frame.row(0, named=True) == {
        "session_date": date(2026, 8, 26),
        "active_version": "selection-baseline",
        "active_policy_hash": "b" * 64,
        "challenger_version": "challenger-test",
        "challenger_policy_hash": "a" * 64,
        "first_wave_sha256": frame["first_wave_sha256"][0],
        "champion_candidate_count": 3,
        "challenger_candidate_count": 2,
        "champion_capture_count": 1,
        "challenger_capture_count": 1,
        "evidence_complete": True,
        "orders_submitted": 0,
    }
