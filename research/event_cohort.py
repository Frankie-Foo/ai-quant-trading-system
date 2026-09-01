"""Point-in-time catalyst cohort for the current 20:00 Beijing selection lock."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date

import polars as pl

from kernel.catalysts import build_catalyst_candidates, select_overnight_catalysts
from research.history import premarket_decision_asof_utc

HARD_CATALYSTS = (
    "earnings",
    "contract_partnership",
    "regulatory_clinical",
    "merger_acquisition",
)
MEDIUM_CATALYSTS = ("other_material", "corporate_action")


def build_event_cohort(
    prepared_news: pl.DataFrame,
    *,
    schedule: pl.DataFrame,
    target_dates: Iterable[date],
) -> pl.DataFrame:
    """Build all eligible event symbols known by the fixed 20:00 Beijing lock."""
    rows: list[pl.DataFrame] = []
    for target in target_dates:
        decision_asof = premarket_decision_asof_utc(target)
        overnight = select_overnight_catalysts(
            prepared_news,
            schedule=schedule,
            target_date=target,
            asof_utc=decision_asof,
        )
        symbols = sorted(
            {
                str(symbol)
                for values in overnight.get_column("symbols").to_list()
                if isinstance(values, list)
                for symbol in values
            }
        )
        if not symbols:
            continue
        candidates = build_catalyst_candidates(
            pl.DataFrame({"symbol": symbols, "precheck_pass": [True] * len(symbols)}),
            overnight,
        )
        tier = (
            pl.when(
                pl.col("catalyst_categories")
                .list.eval(pl.element().is_in(HARD_CATALYSTS))
                .list.any()
            )
            .then(pl.lit(2))
            .when(
                pl.col("catalyst_categories")
                .list.eval(pl.element().is_in(MEDIUM_CATALYSTS))
                .list.any()
            )
            .then(pl.lit(1))
            .otherwise(pl.lit(0))
            .alias("catalyst_tier")
        )
        rows.append(
            candidates.with_columns(
                tier,
                pl.lit(decision_asof)
                .cast(pl.Datetime("ms", "UTC"))
                .alias("decision_asof_utc"),
            )
        )
    if not rows:
        return pl.DataFrame()
    return (
        pl.concat(rows, how="diagonal_relaxed")
        .unique(("session_date", "symbol"), keep="last")
        .sort("session_date", "symbol")
    )
