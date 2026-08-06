from __future__ import annotations

import argparse
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import polars as pl
from dotenv import load_dotenv

from data_plane.calendar import build_xnys_schedule
from data_plane.contracts import DataQualityCheck, DatasetSnapshot, QualitySeverity
from data_plane.providers.massive import fetch_ticker_reference
from data_plane.storage import persist_snapshot
from schedule.runtime import ProcessLock

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "massive.reference_tickers.cs"


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


def reference_plan(*, end_date: date, sessions: int) -> tuple[tuple[date, ...], tuple[date, ...]]:
    """Return target XNYS sessions and fixed week-end PIT reference anchors."""
    if sessions <= 0:
        raise ValueError("sessions must be positive")
    calendar_start = end_date - timedelta(days=max(400, sessions * 2))
    calendar_end = end_date + timedelta(days=7)
    values = build_xnys_schedule(calendar_start, calendar_end).get_column(
        "trade_date"
    ).to_list()
    eligible = [value for value in values if value <= end_date]
    if len(eligible) < sessions:
        raise ValueError("not enough XNYS sessions in calendar window")
    targets = tuple(eligible[-sessions:])

    weekly: dict[tuple[int, int], date] = {}
    for value in values:
        iso = value.isocalendar()
        weekly[(iso.year, iso.week)] = value
    first_target = targets[0]
    relevant = tuple(
        value
        for _, value in sorted(weekly.items())
        if value < end_date and value >= first_target - timedelta(days=10)
    )
    if not relevant or relevant[0] >= first_target:
        prior = [value for value in weekly.values() if value < first_target]
        if not prior:
            raise ValueError("no strictly prior reference anchor is available")
        relevant = (max(prior), *relevant)
    for target in targets:
        if not any(anchor < target for anchor in relevant):
            raise AssertionError(f"no prior reference anchor for {target}")
    return targets, tuple(sorted(set(relevant)))


def _manifest(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"manifest is not an object: {path}")
    return value


def _cached(data_root: Path, asof_date: date) -> DatasetSnapshot | None:
    matches: list[DatasetSnapshot] = []
    for path in (data_root / "accepted").glob(f"{SOURCE}-*/data.parquet"):
        dates = pl.read_parquet(path, columns=["asof_date"]).get_column(
            "asof_date"
        ).unique().to_list()
        if dates == [asof_date]:
            matches.append(
                DatasetSnapshot.model_validate(_manifest(path.parent / "manifest.json"))
            )
    return max(matches, key=lambda value: value.asof_utc) if matches else None


def _checks(frame: pl.DataFrame, asof_date: date) -> tuple[DataQualityCheck, ...]:
    provenance = f"massive.reference_tickers@{asof_date.isoformat()}"
    duplicates = frame.height - frame.get_column("symbol").n_unique()
    wrong_dates = frame.filter(pl.col("asof_date") != asof_date).height
    wrong_rows = frame.filter(
        (pl.col("security_type") != "CS") | ~pl.col("active")
    ).height

    def check(name: str, passed: bool, observed: object, expected: str) -> DataQualityCheck:
        return DataQualityCheck(
            name=name,
            severity=QualitySeverity.CRITICAL,
            passed=passed,
            observed=str(observed),
            expected=expected,
            provenance=provenance,
        )

    return (
        check("non_empty", frame.height > 0, frame.height, "row_count > 0"),
        check("unique_symbol", duplicates == 0, duplicates, "0 duplicate symbols"),
        check("point_in_time_date", wrong_dates == 0, wrong_dates, asof_date.isoformat()),
        check("active_common_stock_only", wrong_rows == 0, wrong_rows, "0 invalid rows"),
    )


def main() -> None:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser()
    parser.add_argument("--end", type=_parse_date, required=True)
    parser.add_argument("--sessions", type=int, default=252)
    parser.add_argument("--data-root", type=Path, default=ROOT / "data")
    parser.add_argument("--pace-seconds", type=float, default=12.5)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    if args.pace_seconds < 0:
        raise ValueError("pace-seconds must be nonnegative")

    targets, anchors = reference_plan(end_date=args.end, sessions=args.sessions)
    cache_hits = 0
    snapshot_ids: list[str] = []
    with ProcessLock(ROOT / "runs" / "massive-reference-history.lock"):
        for index, asof_date in enumerate(anchors, start=1):
            cached = None if args.refresh else _cached(args.data_root, asof_date)
            if cached is None:
                def report_page(page: int, rows: int, anchor: date = asof_date) -> None:
                    print(
                        json.dumps(
                            {
                                "event": "page",
                                "anchor": anchor.isoformat(),
                                "page": page,
                                "cumulative_rows": rows,
                            }
                        ),
                        flush=True,
                    )

                frame = fetch_ticker_reference(
                    asof_date,
                    active=True,
                    security_type="CS",
                    pace_seconds=args.pace_seconds,
                    on_page=report_page,
                )
                snapshot, _ = persist_snapshot(
                    frame,
                    root=args.data_root,
                    source=SOURCE,
                    schema_version="ticker_reference_pit.v1",
                    checks=_checks(frame, asof_date),
                )
                snapshot.assert_usable()
            else:
                snapshot = cached
                cache_hits += 1
            snapshot_ids.append(snapshot.dataset_id)
            print(
                json.dumps(
                    {
                        "event": "anchor_complete",
                        "anchor": asof_date.isoformat(),
                        "completed": index,
                        "total": len(anchors),
                        "cached": cached is not None,
                        "dataset_id": snapshot.dataset_id,
                    }
                ),
                flush=True,
            )

    print(
        json.dumps(
            {
                "status": "complete",
                "target_start": targets[0].isoformat(),
                "target_end": targets[-1].isoformat(),
                "target_sessions": len(targets),
                "reference_anchors": len(anchors),
                "cache_hits": cache_hits,
                "snapshot_ids": snapshot_ids,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
