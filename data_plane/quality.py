from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any

import polars as pl

from data_plane.contracts import DataQualityCheck, QualitySeverity

BAR_SCHEMA_VERSION = "bars_1m.v1"
BAR_COLUMNS = (
    "symbol",
    "ts_utc",
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


def canonicalize_bars(frame: pl.DataFrame) -> pl.DataFrame:
    """Cast provider output into the only minute-bar schema consumed downstream."""
    missing = sorted(set(BAR_COLUMNS) - set(frame.columns))
    if missing:
        raise ValueError(f"missing canonical bar columns: {missing}")

    timestamp = pl.col("ts_utc")
    if frame.schema["ts_utc"] == pl.String:
        timestamp = timestamp.str.to_datetime(time_zone="UTC", strict=True)

    return (
        frame.select(BAR_COLUMNS)
        .with_columns(
            pl.col("symbol").cast(pl.String),
            timestamp.cast(pl.Datetime("ms", "UTC")).alias("ts_utc"),
            pl.col("open").cast(pl.Float64),
            pl.col("high").cast(pl.Float64),
            pl.col("low").cast(pl.Float64),
            pl.col("close").cast(pl.Float64),
            pl.col("volume").cast(pl.Int64),
            pl.col("trade_count").cast(pl.Int64),
            pl.col("vwap").cast(pl.Float64),
            pl.col("source").cast(pl.String),
            pl.col("feed").cast(pl.String),
            pl.col("adjustment").cast(pl.String),
        )
        .sort("symbol", "ts_utc")
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


def audit_minute_bars(
    frame: pl.DataFrame,
    *,
    provenance: str,
    expected_symbols: Iterable[str] = (),
    research_approved: bool,
) -> tuple[DataQualityCheck, ...]:
    expected = tuple(sorted(set(expected_symbols)))
    actual = tuple(sorted(frame.get_column("symbol").unique().to_list())) if frame.height else ()
    duplicate_count = (
        frame.select(pl.struct("symbol", "ts_utc").is_duplicated().sum()).item()
        if frame.height
        else 0
    )
    invalid_ohlc = (
        frame.filter(
            (pl.col("low") > pl.min_horizontal("open", "close", "high"))
            | (pl.col("high") < pl.max_horizontal("open", "close", "low"))
        ).height
        if frame.height
        else 0
    )
    nonpositive_prices = (
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
    null_count = int(frame.null_count().sum_horizontal().item()) if frame.width else 0
    non_finite_count = 0
    for column in ("open", "high", "low", "close", "vwap"):
        if frame.height:
            non_finite_count += frame.filter(
                pl.col(column).is_not_null() & ~pl.col(column).is_finite()
            ).height
    minute_misaligned = (
        frame.filter(
            (pl.col("ts_utc").dt.second() != 0)
            | (pl.col("ts_utc").dt.millisecond() != 0)
        ).height
        if frame.height
        else 0
    )
    timezone = str(frame.schema.get("ts_utc"))

    checks = [
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
            tuple(frame.columns) == BAR_COLUMNS,
            tuple(frame.columns),
            str(BAR_COLUMNS),
            provenance,
        ),
        _check(
            "utc_timestamp",
            QualitySeverity.CRITICAL,
            timezone == "Datetime(time_unit='ms', time_zone='UTC')",
            timezone,
            "Datetime(ms, UTC)",
            provenance,
        ),
        _check(
            "unique_symbol_timestamp",
            QualitySeverity.CRITICAL,
            duplicate_count == 0,
            duplicate_count,
            "0 duplicate (symbol, ts_utc) keys",
            provenance,
        ),
        _check(
            "ohlc_logic",
            QualitySeverity.CRITICAL,
            invalid_ohlc == 0,
            invalid_ohlc,
            "low <= open/close <= high for every row",
            provenance,
        ),
        _check(
            "positive_prices",
            QualitySeverity.CRITICAL,
            nonpositive_prices == 0,
            nonpositive_prices,
            "all OHLC prices > 0",
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
            "finite_values",
            QualitySeverity.CRITICAL,
            non_finite_count == 0,
            non_finite_count,
            "no NaN or infinite numeric values",
            provenance,
        ),
        _check(
            "minute_alignment",
            QualitySeverity.CRITICAL,
            minute_misaligned == 0,
            minute_misaligned,
            "timestamps aligned to exact minute boundaries",
            provenance,
        ),
        _check(
            "null_visibility",
            QualitySeverity.WARNING,
            null_count == 0,
            null_count,
            "0 null cells; unavailable provider fields must remain explicit",
            provenance,
        ),
    ]

    if expected:
        missing_symbols = sorted(set(expected) - set(actual))
        checks.append(
            _check(
                "requested_symbol_coverage",
                QualitySeverity.CRITICAL,
                not missing_symbols,
                missing_symbols or "complete",
                f"all requested symbols present: {list(expected)}",
                provenance,
            )
        )

    checks.append(
        _check(
            "research_provenance_approval",
            QualitySeverity.CRITICAL,
            research_approved,
            research_approved,
            "source license, feed, adjustment, and universe are independently verified",
            provenance,
        )
    )
    return tuple(checks)


def nullable_float(value: Any) -> float | None:
    if value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) else None
