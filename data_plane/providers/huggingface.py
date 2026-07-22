from __future__ import annotations

from datetime import datetime

import polars as pl

from data_plane.quality import canonicalize_bars

DATASET_URL = (
    "https://huggingface.co/datasets/CryptoSpartan/stocks_bars_1m/"
    "resolve/main/stocks_bars_1m.parquet"
)


def fetch_staging_bars(
    symbols: tuple[str, ...],
    start_utc: datetime,
    end_utc: datetime,
) -> pl.DataFrame:
    if not symbols:
        raise ValueError("at least one symbol is required")
    if start_utc.tzinfo is None or end_utc.tzinfo is None:
        raise ValueError("start_utc and end_utc must be timezone-aware")
    if start_utc >= end_utc:
        raise ValueError("start_utc must be before end_utc")

    raw = (
        pl.scan_parquet(DATASET_URL)
        .filter(
            pl.col("ticker").is_in(symbols)
            & (pl.col("timestamp") >= start_utc)
            & (pl.col("timestamp") < end_utc)
        )
        .select(
            pl.col("ticker").alias("symbol"),
            pl.col("timestamp").alias("ts_utc"),
            "open",
            "high",
            "low",
            "close",
            "volume",
            "trade_count",
            pl.col("vol_weighted_avg_price").alias("vwap"),
            pl.lit("huggingface.crypto_spartan").alias("source"),
            pl.lit("unknown").alias("feed"),
            pl.lit("unknown").alias("adjustment"),
        )
        .collect()
    )
    return canonicalize_bars(raw)
