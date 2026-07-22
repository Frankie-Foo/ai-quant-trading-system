from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
import pytest

from data_plane.storage import persist_snapshot
from execution.locked_selection import load_locked_selection


def _persist(root: Path, frame: pl.DataFrame) -> None:
    persist_snapshot(
        frame,
        root=root,
        source="kernel.universe.selection_gates",
        schema_version="selection_gates.v1",
        checks=(),
    )


def test_loads_only_passed_point_in_time_candidates(tmp_path: Path) -> None:
    _persist(
        tmp_path,
        pl.DataFrame(
            {
                "symbol": ["AAPL", "MSFT"],
                "session_date": [date(2026, 7, 21)] * 2,
                "selection_rank": [1, 2],
                "pass_gate": [True, False],
                "rvol": [4.0, 5.0],
                "price": [225.0, 500.0],
                "adv_usd": [1_000_000_000.0, 800_000_000.0],
                "atr_pct": [0.03, 0.02],
                "tier": ["mega", "mega"],
            }
        ),
    )

    selection = load_locked_selection(tmp_path, date(2026, 7, 21), min_rvol=3.0)

    assert selection.symbols == ("AAPL",)
    assert selection.candidates[0].rvol == 4.0
    assert selection.snapshot.source == "kernel.universe.selection_gates"


def test_selection_loader_fails_closed_on_duplicate_or_invalid_rvol(tmp_path: Path) -> None:
    _persist(
        tmp_path,
        pl.DataFrame(
            {
                "symbol": ["AAPL", "AAPL"],
                "session_date": [date(2026, 7, 21)] * 2,
                "selection_rank": [1, 2],
                "pass_gate": [True, True],
                "rvol": [4.0, 3.0],
                "price": [225.0, 225.0],
                "adv_usd": [1_000_000_000.0, 1_000_000_000.0],
                "atr_pct": [0.03, 0.03],
                "tier": ["mega", "mega"],
            }
        ),
    )

    with pytest.raises(ValueError, match="duplicate|RVOL"):
        load_locked_selection(tmp_path, date(2026, 7, 21), min_rvol=3.0)
