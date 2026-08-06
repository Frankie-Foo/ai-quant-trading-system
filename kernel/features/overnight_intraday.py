"""Point-in-time overnight/intraday return decomposition features."""

from __future__ import annotations

import polars as pl


def decompose(df_daily_oc: pl.DataFrame) -> pl.DataFrame:
    """Decompose close-to-close returns without crossing symbol histories."""
    required = {"trade_date", "open", "close"}
    missing = required - set(df_daily_oc.columns)
    if missing:
        raise ValueError(f"daily data missing required columns: {sorted(missing)}")
    group = ["symbol"] if "symbol" in df_daily_oc.columns else []
    sorted_frame = df_daily_oc.sort(*group, "trade_date")
    previous_close = (
        pl.col("close").shift(1).over(group) if group else pl.col("close").shift(1)
    )
    return sorted_frame.with_columns(previous_close.alias("previous_close")).with_columns(
        (pl.col("open") / pl.col("previous_close") - 1).alias("r_overnight"),
        (pl.col("close") / pl.col("open") - 1).alias("r_intraday"),
    )
