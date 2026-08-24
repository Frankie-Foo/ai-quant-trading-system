"""Point-in-time first-wave pool for modern H15 momentum shadowing."""

from __future__ import annotations

from collections.abc import Mapping

import polars as pl

HARD_CATALYSTS = (
    "earnings",
    "contract_partnership",
    "regulatory_clinical",
    "merger_acquisition",
)


def select_forward_pool(
    gates: pl.DataFrame,
    *,
    market_caps: Mapping[str, float],
    limit: int = 10,
) -> pl.DataFrame:
    """Rank hard-safe names without consulting the old strategy's soft gate."""
    caps = pl.DataFrame(
        {"symbol": list(market_caps), "forward_market_cap": list(market_caps.values())}
    )
    return (
        gates.join(caps, on="symbol", how="left")
        .filter(
            (pl.col("forward_market_cap").fill_null(0) >= 1_000_000_000)
            & (pl.col("rvol").fill_null(0) >= 1.5)
            & ~pl.col("current_halt").fill_null(True)
            & ~pl.col("luld_risk").fill_null(True)
        )
        .with_columns(
            pl.when(
                pl.col("catalyst_categories")
                .list.eval(pl.element().is_in(HARD_CATALYSTS))
                .list.any()
            )
            .then(pl.lit(1))
            .otherwise(pl.lit(0))
            .alias("hard_catalyst")
        )
        .sort(
            "hard_catalyst",
            "premarket_return",
            "rvol",
            descending=True,
            nulls_last=True,
        )
        .head(limit)
        .with_row_index("forward_rank", offset=1)
    )
