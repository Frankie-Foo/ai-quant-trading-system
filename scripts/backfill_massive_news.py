from __future__ import annotations

import argparse
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import polars as pl
from dotenv import load_dotenv

from data_plane.catalysts import CATALYST_SCHEMA_VERSION, audit_catalysts
from data_plane.contracts import DataQualityCheck, DatasetSnapshot, QualitySeverity
from data_plane.providers.catalyst_news import fetch_massive_news
from data_plane.storage import persist_snapshot

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "massive.news.history"


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


def _next_month(value: date) -> date:
    if value.month == 12:
        return date(value.year + 1, 1, 1)
    return date(value.year, value.month + 1, 1)


def _partitions(start: date, end: date) -> list[tuple[date, date]]:
    values: list[tuple[date, date]] = []
    current = start
    while current < end:
        boundary = min(end, _next_month(current))
        values.append((current, boundary))
        current = boundary
    return values


def _manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"manifest is not an object: {path}")
    return payload


def _provenance(start_utc: datetime, end_utc: datetime) -> str:
    return f"{SOURCE}@{start_utc.isoformat()}..{end_utc.isoformat()}"


def _cached(
    data_root: Path, start_utc: datetime, end_utc: datetime
) -> tuple[pl.DataFrame, DatasetSnapshot] | None:
    expected = _provenance(start_utc, end_utc)
    matches: list[tuple[datetime, Path, DatasetSnapshot]] = []
    for path in (data_root / "accepted").glob(f"{SOURCE}-*/data.parquet"):
        snapshot = DatasetSnapshot.model_validate(_manifest(path.parent / "manifest.json"))
        if any(check.provenance == expected for check in snapshot.checks):
            matches.append((snapshot.asof_utc, path, snapshot))
    if not matches:
        return None
    _, path, snapshot = max(matches)
    return pl.read_parquet(path), snapshot


def _combined_checks(
    frame: pl.DataFrame, *, start_utc: datetime, end_utc: datetime
) -> tuple[DataQualityCheck, ...]:
    provenance = f"{SOURCE}.combined@{start_utc.isoformat()}..{end_utc.isoformat()}"
    duplicate_count = frame.height - frame.select(
        pl.struct("source", "source_event_id").n_unique()
    ).item()
    outside = frame.filter(
        (pl.col("published_utc") < start_utc) | (pl.col("published_utc") >= end_utc)
    ).height
    return (
        DataQualityCheck(
            name="non_empty",
            severity=QualitySeverity.CRITICAL,
            passed=frame.height > 0,
            observed=str(frame.height),
            expected="row_count > 0",
            provenance=provenance,
        ),
        DataQualityCheck(
            name="unique_source_event",
            severity=QualitySeverity.CRITICAL,
            passed=duplicate_count == 0,
            observed=str(duplicate_count),
            expected="0 duplicate source event IDs",
            provenance=provenance,
        ),
        DataQualityCheck(
            name="point_in_time_window",
            severity=QualitySeverity.CRITICAL,
            passed=outside == 0,
            observed=str(outside),
            expected=f"all events in [{start_utc.isoformat()}, {end_utc.isoformat()})",
            provenance=provenance,
        ),
    )


def main() -> None:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=_parse_date, required=True)
    parser.add_argument("--end", type=_parse_date, required=True)
    parser.add_argument("--data-root", type=Path, default=ROOT / "data")
    parser.add_argument("--pace-seconds", type=float, default=12.5)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    if args.end <= args.start:
        raise ValueError("end must be after start and is exclusive")

    frames: list[pl.DataFrame] = []
    snapshots: list[DatasetSnapshot] = []
    cache_hits = 0
    partitions = _partitions(args.start, args.end)
    for index, (part_start, part_end) in enumerate(partitions, start=1):
        start_utc = datetime.combine(part_start, datetime.min.time(), UTC)
        end_utc = datetime.combine(part_end, datetime.min.time(), UTC)
        cached = None if args.refresh else _cached(args.data_root, start_utc, end_utc)
        if cached is None:
            frame = fetch_massive_news(
                start_utc, end_utc, pace_seconds=args.pace_seconds
            )
            checks = audit_catalysts(
                frame,
                provenance=_provenance(start_utc, end_utc),
                start_utc=start_utc,
                end_utc=end_utc,
                require_non_empty=True,
            )
            snapshot, _ = persist_snapshot(
                frame,
                root=args.data_root,
                source=SOURCE,
                schema_version=CATALYST_SCHEMA_VERSION,
                checks=checks,
            )
            snapshot.assert_usable()
        else:
            frame, snapshot = cached
            cache_hits += 1
        frames.append(frame)
        snapshots.append(snapshot)
        print(
            json.dumps(
                {
                    "partition": index,
                    "partition_count": len(partitions),
                    "start": part_start.isoformat(),
                    "end_exclusive": part_end.isoformat(),
                    "rows": frame.height,
                    "cached": cached is not None,
                    "dataset_id": snapshot.dataset_id,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    combined = pl.concat(frames).unique(
        subset=["source", "source_event_id"], keep="first"
    ).sort("published_utc", "source_event_id")
    start_utc = datetime.combine(args.start, datetime.min.time(), UTC)
    end_utc = datetime.combine(args.end, datetime.min.time(), UTC)
    combined_snapshot, path = persist_snapshot(
        combined,
        root=args.data_root,
        source=f"{SOURCE}.combined",
        schema_version=CATALYST_SCHEMA_VERSION,
        checks=_combined_checks(combined, start_utc=start_utc, end_utc=end_utc),
        parent_snapshot_ids=tuple(snapshot.dataset_id for snapshot in snapshots),
    )
    combined_snapshot.assert_usable()
    print(
        json.dumps(
            {
                "status": "complete",
                "start": args.start.isoformat(),
                "end_exclusive": args.end.isoformat(),
                "partitions": len(partitions),
                "cache_hits": cache_hits,
                "rows": combined.height,
                "dataset_id": combined_snapshot.dataset_id,
                "path": str(path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
