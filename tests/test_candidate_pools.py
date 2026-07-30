from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl

from data_plane.candidate_pools import load_premarket_pool
from data_plane.storage import persist_snapshot

TRADE_DATE = date(2026, 7, 28)


def _persist(
    root: Path,
    *,
    source: str,
    frame: pl.DataFrame,
) -> None:
    persist_snapshot(
        frame,
        root=root,
        source=source,
        schema_version="test.v1",
        checks=(),
    )


def test_factor_premarket_pool_comes_from_daily_precheck_not_catalysts(
    tmp_path: Path,
) -> None:
    _persist(
        tmp_path,
        source="kernel.universe.daily_precheck",
        frame=pl.DataFrame(
            {
                "symbol": ["FACTOR", "BLOCKED"],
                "session_date": [TRADE_DATE, TRADE_DATE],
                "precheck_pass": [True, False],
                "reject_reason": [
                    "pending:rvol,market_cap,earnings,luld",
                    "beta_below_min",
                ],
            }
        ),
    )
    _persist(
        tmp_path,
        source="kernel.catalysts.overnight_candidates",
        frame=pl.DataFrame(
            {
                "symbol": ["CAT"],
                "session_date": [TRADE_DATE],
            }
        ),
    )

    factor = load_premarket_pool(tmp_path, TRADE_DATE, pool="factor")
    catalyst = load_premarket_pool(tmp_path, TRADE_DATE, pool="catalyst")

    assert factor.frame.get_column("symbol").to_list() == ["FACTOR"]
    assert factor.source == "kernel.universe.daily_precheck"
    assert catalyst.frame.get_column("symbol").to_list() == ["CAT"]
    assert catalyst.source == "kernel.catalysts.overnight_candidates"


def test_premarket_pool_rejects_duplicate_symbols(tmp_path: Path) -> None:
    _persist(
        tmp_path,
        source="kernel.universe.daily_precheck",
        frame=pl.DataFrame(
            {
                "symbol": ["DUP", "DUP"],
                "session_date": [TRADE_DATE, TRADE_DATE],
                "precheck_pass": [True, True],
                "reject_reason": ["pending", "pending"],
            }
        ),
    )

    try:
        load_premarket_pool(tmp_path, TRADE_DATE, pool="factor")
    except ValueError as exc:
        assert "duplicate symbols" in str(exc)
    else:
        raise AssertionError("duplicate point-in-time symbols must fail closed")
