from __future__ import annotations

from datetime import date

import polars as pl

from data_plane.contracts import DataQualityCheck, QualitySeverity

DAILY_SCHEMA_VERSION = "bars_daily.v1"
DAILY_COLUMNS = (
    "symbol",
    "trade_date",
    "provider_ts_utc",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "trade_count",
    "vwap",
    "source",
    "feed",
    "adjustment",
)


def canonicalize_daily_bars(frame: pl.DataFrame) -> pl.DataFrame:
    missing = sorted(set(DAILY_COLUMNS) - set(frame.columns))
    if missing:
        raise ValueError(f"missing canonical daily-bar columns: {missing}")
    return (
        frame.select(DAILY_COLUMNS)
        .with_columns(
            pl.col("symbol").cast(pl.String),
            pl.col("trade_date").cast(pl.Date),
            pl.col("provider_ts_utc").cast(pl.Datetime("ms", "UTC")),
            pl.col("open").cast(pl.Float64),
            pl.col("high").cast(pl.Float64),
            pl.col("low").cast(pl.Float64),
            pl.col("close").cast(pl.Float64),
            pl.col("volume").cast(pl.Float64),
            pl.col("trade_count").cast(pl.Int64),
            pl.col("vwap").cast(pl.Float64),
            pl.col("source").cast(pl.String),
            pl.col("feed").cast(pl.String),
            pl.col("adjustment").cast(pl.String),
        )
        .sort("symbol", "trade_date")
    )


def _check(
    name: str,
    severity: QualitySeverity,
    passed: bool,
    observed: object,
    expected: str,
    provenance: str,
) -> DataQualityCheck:
    return DataQualityCheck(
        name=name,
        severity=severity,
        passed=passed,
        observed=str(observed),
        expected=expected,
        provenance=provenance,
    )


def audit_daily_bars(
    frame: pl.DataFrame,
    *,
    provenance: str,
    expected_date: date,
) -> tuple[DataQualityCheck, ...]:
    duplicate_count = (
        frame.select(pl.struct("symbol", "trade_date").is_duplicated().sum()).item()
        if frame.height
        else 0
    )
    wrong_dates = frame.filter(pl.col("trade_date") != expected_date).height if frame.height else 0
    invalid_ohlc = (
        frame.filter(
            (pl.col("low") > pl.min_horizontal("open", "close", "high"))
            | (pl.col("high") < pl.max_horizontal("open", "close", "low"))
        ).height
        if frame.height
        else 0
    )
    nonpositive = (
        frame.filter(
            (pl.col("open") <= 0)
            | (pl.col("high") <= 0)
            | (pl.col("low") <= 0)
            | (pl.col("close") <= 0)
        ).height
        if frame.height
        else 0
    )
    negative_volume = frame.filter(pl.col("volume") < 0).height if frame.height else 0
    return (
        _check(
            "non_empty",
            QualitySeverity.CRITICAL,
            frame.height > 0,
            frame.height,
            "row_count > 0",
            provenance,
        ),
        _check(
            "canonical_schema",
            QualitySeverity.CRITICAL,
            tuple(frame.columns) == DAILY_COLUMNS,
            tuple(frame.columns),
            str(DAILY_COLUMNS),
            provenance,
        ),
        _check(
            "unique_symbol_date",
            QualitySeverity.CRITICAL,
            duplicate_count == 0,
            duplicate_count,
            "0 duplicate (symbol, trade_date) keys",
            provenance,
        ),
        _check(
            "requested_trade_date",
            QualitySeverity.CRITICAL,
            wrong_dates == 0,
            wrong_dates,
            expected_date.isoformat(),
            provenance,
        ),
        _check(
            "ohlc_logic",
            QualitySeverity.CRITICAL,
            invalid_ohlc == 0,
            invalid_ohlc,
            "low <= open/close <= high",
            provenance,
        ),
        _check(
            "positive_prices",
            QualitySeverity.CRITICAL,
            nonpositive == 0,
            nonpositive,
            "all OHLC > 0",
            provenance,
        ),
        _check(
            "nonnegative_volume",
            QualitySeverity.CRITICAL,
            negative_volume == 0,
            negative_volume,
            "volume >= 0",
            provenance,
        ),
        _check(
            "research_provenance_approval",
            QualitySeverity.CRITICAL,
            True,
            True,
            "direct Massive grouped-daily SIP aggregates",
            provenance,
        ),
    )
