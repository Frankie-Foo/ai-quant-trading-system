from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from research.sandbox import evaluate_rvol_challengers


def _labels(*, challenger_wins: bool) -> pl.DataFrame:
    start = date(2026, 1, 2)
    rows: list[dict[str, object]] = []
    for offset in range(60):
        trade_date = start + timedelta(days=offset)
        rows.extend(
            (
                {
                    "trade_date": trade_date,
                    "symbol": f"L{offset}",
                    "selection_rank": 1,
                    "rvol": 3.2,
                    "net_pnl": 200.0 if not challenger_wins else -100.0,
                    "net_return_on_notional": 0.02 if not challenger_wins else -0.01,
                },
                {
                    "trade_date": trade_date,
                    "symbol": f"H{offset}",
                    "selection_rank": 2,
                    "rvol": 4.2,
                    "net_pnl": 200.0,
                    "net_return_on_notional": 0.02,
                },
            )
        )
    return pl.DataFrame(rows).with_columns(pl.col("trade_date").cast(pl.Date))


def test_rvol_challenger_requires_discovery_and_untouched_holdout() -> None:
    metrics, decision = evaluate_rvol_challengers(
        _labels(challenger_wins=True), baseline=3.0
    )
    assert metrics.height == 20
    assert decision.status == "research_champion_promoted"
    assert decision.selected == 3.5
    assert decision.production_eligible is False


def test_rvol_champion_is_retained_without_material_improvement() -> None:
    _, decision = evaluate_rvol_challengers(
        _labels(challenger_wins=False), baseline=3.0
    )
    assert decision.status == "champion_retained"
    assert decision.selected == 3.0
