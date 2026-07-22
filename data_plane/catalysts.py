from __future__ import annotations

from datetime import datetime

import polars as pl

from data_plane.contracts import DataQualityCheck, QualitySeverity

CATALYST_SCHEMA_VERSION = "catalyst_events.v1"
CATALYST_COLUMNS = (
    "source",
    "source_event_id",
    "event_type",
    "event_subtype",
    "published_utc",
    "updated_utc",
    "retrieved_utc",
    "symbols",
    "headline",
    "summary",
    "publisher",
    "url",
    "cik",
    "accession_number",
    "form_items",
    "tags",
    "provenance",
)


def empty_catalyst_frame() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "source": pl.String,
            "source_event_id": pl.String,
            "event_type": pl.String,
            "event_subtype": pl.String,
            "published_utc": pl.Datetime("ms", "UTC"),
            "updated_utc": pl.Datetime("ms", "UTC"),
            "retrieved_utc": pl.Datetime("ms", "UTC"),
            "symbols": pl.List(pl.String),
            "headline": pl.String,
            "summary": pl.String,
            "publisher": pl.String,
            "url": pl.String,
            "cik": pl.String,
            "accession_number": pl.String,
            "form_items": pl.List(pl.String),
            "tags": pl.List(pl.String),
            "provenance": pl.String,
        }
    )


def canonicalize_catalysts(frame: pl.DataFrame) -> pl.DataFrame:
    """Normalize provider events without inventing unavailable values."""
    missing = sorted(set(CATALYST_COLUMNS) - set(frame.columns))
    if missing:
        raise ValueError(f"missing canonical catalyst columns: {missing}")
    published_utc = _utc_timestamp(frame, "published_utc")
    updated_utc = _utc_timestamp(frame, "updated_utc")
    retrieved_utc = _utc_timestamp(frame, "retrieved_utc")
    return (
        frame.select(CATALYST_COLUMNS)
        .with_columns(
            pl.col("source").cast(pl.String),
            pl.col("source_event_id").cast(pl.String),
            pl.col("event_type").cast(pl.String),
            pl.col("event_subtype").cast(pl.String),
            published_utc,
            updated_utc,
            retrieved_utc,
            pl.col("symbols")
            .cast(pl.List(pl.String))
            .list.eval(pl.element().str.to_uppercase())
            .list.unique()
            .list.sort(),
            pl.col("headline").cast(pl.String),
            pl.col("summary").cast(pl.String),
            pl.col("publisher").cast(pl.String),
            pl.col("url").cast(pl.String),
            pl.col("cik").cast(pl.String),
            pl.col("accession_number").cast(pl.String),
            pl.col("form_items").cast(pl.List(pl.String)),
            pl.col("tags").cast(pl.List(pl.String)),
            pl.col("provenance").cast(pl.String),
        )
        .sort("published_utc", "source", "source_event_id")
    )


def _utc_timestamp(frame: pl.DataFrame, column: str) -> pl.Expr:
    if frame.schema[column] == pl.String:
        return pl.col(column).str.to_datetime(
            time_unit="ms", time_zone="UTC", strict=True
        )
    return pl.col(column).cast(pl.Datetime("ms", "UTC"))


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


def audit_catalysts(
    frame: pl.DataFrame,
    *,
    provenance: str,
    start_utc: datetime,
    end_utc: datetime,
    require_non_empty: bool = True,
) -> tuple[DataQualityCheck, ...]:
    if start_utc.tzinfo is None or end_utc.tzinfo is None:
        raise ValueError("catalyst audit bounds must be timezone-aware")
    duplicate_count = (
        frame.select(pl.struct("source", "source_event_id").is_duplicated().sum()).item()
        if frame.height
        else 0
    )
    out_of_window = frame.filter(
        (pl.col("published_utc") < start_utc) | (pl.col("published_utc") >= end_utc)
    ).height
    missing_symbols = frame.filter(pl.col("symbols").list.len() == 0).height
    missing_provenance = frame.filter(
        pl.col("provenance").is_null() | (pl.col("provenance").str.len_chars() == 0)
    ).height
    expected_datetime = "Datetime(time_unit='ms', time_zone='UTC')"
    timestamp_types = (
        str(frame.schema.get("published_utc")),
        str(frame.schema.get("updated_utc")),
        str(frame.schema.get("retrieved_utc")),
    )
    return (
        _check(
            "non_empty",
            QualitySeverity.CRITICAL if require_non_empty else QualitySeverity.INFO,
            frame.height > 0 or not require_non_empty,
            frame.height,
            "row_count > 0" if require_non_empty else "zero rows allowed",
            provenance,
        ),
        _check(
            "canonical_schema",
            QualitySeverity.CRITICAL,
            tuple(frame.columns) == CATALYST_COLUMNS,
            tuple(frame.columns),
            str(CATALYST_COLUMNS),
            provenance,
        ),
        _check(
            "unique_source_event",
            QualitySeverity.CRITICAL,
            duplicate_count == 0,
            duplicate_count,
            "0 duplicate (source, source_event_id) keys",
            provenance,
        ),
        _check(
            "requested_publication_window",
            QualitySeverity.CRITICAL,
            out_of_window == 0,
            out_of_window,
            f"{start_utc.isoformat()} <= published_utc < {end_utc.isoformat()}",
            provenance,
        ),
        _check(
            "utc_timestamps",
            QualitySeverity.CRITICAL,
            all(item == expected_datetime for item in timestamp_types),
            timestamp_types,
            expected_datetime,
            provenance,
        ),
        _check(
            "symbols_present",
            QualitySeverity.CRITICAL,
            missing_symbols == 0,
            missing_symbols,
            "0 events without symbols",
            provenance,
        ),
        _check(
            "provenance_present",
            QualitySeverity.CRITICAL,
            missing_provenance == 0,
            missing_provenance,
            "0 events without provenance",
            provenance,
        ),
    )
