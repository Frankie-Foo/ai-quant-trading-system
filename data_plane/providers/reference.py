from __future__ import annotations

import io
import os

import polars as pl

from data_plane.http import get_json, get_response

NASDAQ_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
SEC_TICKERS = "https://www.sec.gov/files/company_tickers.json"


def fetch_nasdaq_symbol_directory() -> pl.DataFrame:
    nasdaq = _read_pipe_table(get_response(NASDAQ_LISTED).text).filter(
        ~pl.col("Symbol").str.starts_with("File Creation Time")
    )
    other = _read_pipe_table(get_response(OTHER_LISTED).text).filter(
        ~pl.col("ACT Symbol").str.starts_with("File Creation Time")
    )
    nasdaq_normalized = nasdaq.select(
        pl.col("Symbol").alias("symbol"),
        pl.col("Security Name").alias("security_name"),
        pl.lit("NASDAQ").alias("listing_exchange"),
        pl.when(pl.col("ETF") == "Y").then(pl.lit("etf")).otherwise(pl.lit("other")).alias(
            "asset_type"
        ),
        pl.col("Test Issue").alias("test_issue"),
        pl.lit("nasdaq_trader.symbol_directory").alias("source"),
    )
    other_normalized = other.select(
        pl.col("ACT Symbol").alias("symbol"),
        pl.col("Security Name").alias("security_name"),
        pl.col("Exchange").alias("listing_exchange"),
        pl.when(pl.col("ETF") == "Y").then(pl.lit("etf")).otherwise(pl.lit("other")).alias(
            "asset_type"
        ),
        pl.col("Test Issue").alias("test_issue"),
        pl.lit("nasdaq_trader.symbol_directory").alias("source"),
    )
    return pl.concat((nasdaq_normalized, other_normalized)).sort("symbol")


def fetch_sec_company_tickers() -> pl.DataFrame:
    user_agent = os.getenv("SEC_USER_AGENT", "").strip()
    if not user_agent:
        raise RuntimeError(
            "missing SEC_USER_AGENT; set a descriptive value with a contact email, for "
            "example 'Frank research your-email@example.com'"
        )
    payload = get_json(SEC_TICKERS, headers={"User-Agent": user_agent})
    rows = [value for value in payload.values() if isinstance(value, dict)]
    return (
        pl.DataFrame(rows)
        .select(
            pl.col("ticker").cast(pl.String).alias("symbol"),
            pl.col("cik_str").cast(pl.Int64).alias("cik"),
            pl.col("title").cast(pl.String).alias("issuer_name"),
            pl.lit("sec.company_tickers").alias("source"),
        )
        .sort("symbol")
    )


def _read_pipe_table(text: str) -> pl.DataFrame:
    return pl.read_csv(
        io.StringIO(text),
        separator="|",
        infer_schema_length=10_000,
        truncate_ragged_lines=True,
    )
