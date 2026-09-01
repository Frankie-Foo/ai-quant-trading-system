"""Backfill daily SIP bars only for symbols in the current-lock event cohort."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import polars as pl

from data_plane.contracts import DataQualityCheck, DatasetSnapshot, QualitySeverity
from data_plane.providers.alpaca import fetch_daily_bars, stock_data_policy_from_env
from data_plane.storage import persist_snapshot
from operations.local_env import load_project_env, project_data_root

ROOT = Path(__file__).resolve().parents[1]
COHORT_SOURCE = "research.current_event_cohort"
SOURCE = "alpaca.sip.daily_event_symbols"


def _snapshot(path: Path) -> DatasetSnapshot:
    return DatasetSnapshot.model_validate_json(
        (path.parent / "manifest.json").read_text(encoding="utf-8")
    )


def _latest_cohort(data_root: Path) -> tuple[pl.DataFrame, DatasetSnapshot]:
    matches = [
        (_snapshot(path).asof_utc, path)
        for path in (data_root / "accepted").glob(f"{COHORT_SOURCE}-*/data.parquet")
    ]
    if not matches:
        raise FileNotFoundError("current event cohort is missing")
    _, path = max(matches)
    return pl.read_parquet(path), _snapshot(path)


def _chunks(values: tuple[str, ...], size: int) -> list[tuple[str, ...]]:
    if size <= 0:
        raise ValueError("chunk size must be positive")
    return [values[index : index + size] for index in range(0, len(values), size)]


def _provenance(symbols: tuple[str, ...], start: date, end: date) -> str:
    digest = hashlib.sha256(",".join(symbols).encode()).hexdigest()[:16]
    return f"{SOURCE}@{start.isoformat()}..{end.isoformat()}|symbols={digest}"


def _checks(frame: pl.DataFrame, provenance: str) -> tuple[DataQualityCheck, ...]:
    duplicates = frame.height - frame.select(
        pl.struct("symbol", "ts_utc").n_unique()
    ).item()
    return (
        DataQualityCheck(
            name="non_empty",
            severity=QualitySeverity.CRITICAL,
            passed=frame.height > 0,
            observed=str(frame.height),
            expected=">0",
            provenance=provenance,
        ),
        DataQualityCheck(
            name="unique_symbol_timestamp",
            severity=QualitySeverity.CRITICAL,
            passed=duplicates == 0,
            observed=str(duplicates),
            expected="0",
            provenance=provenance,
        ),
    )


def _cache(data_root: Path) -> dict[str, tuple[Path, DatasetSnapshot]]:
    result: dict[str, tuple[Path, DatasetSnapshot]] = {}
    for path in (data_root / "accepted").glob(f"{SOURCE}-*/data.parquet"):
        snapshot = _snapshot(path)
        for check in snapshot.checks:
            if check.provenance.startswith(f"{SOURCE}@"):
                result[check.provenance] = (path, snapshot)
    return result


def main() -> None:
    load_project_env(ROOT)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--chunk-size", type=int, default=200)
    parser.add_argument("--chunk-start", type=int, default=1)
    parser.add_argument("--chunk-end", type=int)
    parser.add_argument("--data-root", type=Path, default=project_data_root(ROOT))
    args = parser.parse_args()
    if args.end < args.start:
        raise ValueError("end must not precede start")
    cohort, cohort_snapshot = _latest_cohort(args.data_root)
    symbols = tuple(sorted(cohort.get_column("symbol").unique().to_list()))
    all_chunks = _chunks(symbols, args.chunk_size)
    chunk_end = len(all_chunks) if args.chunk_end is None else args.chunk_end
    if args.chunk_start < 1 or chunk_end < args.chunk_start or chunk_end > len(all_chunks):
        raise ValueError("chunk range is outside the available chunks")
    selected_chunks = list(
        enumerate(all_chunks[args.chunk_start - 1 : chunk_end], start=args.chunk_start)
    )
    query_start = args.start - timedelta(days=400)
    query_end = args.end + timedelta(days=1)
    start_utc = datetime.combine(query_start, datetime.min.time(), UTC)
    end_utc = datetime.combine(query_end, datetime.min.time(), UTC)
    policy = stock_data_policy_from_env()
    cached = _cache(args.data_root)
    frames: list[pl.DataFrame] = []
    parents: list[str] = [cohort_snapshot.dataset_id]
    for index, chunk in selected_chunks:
        provenance = _provenance(chunk, query_start, query_end)
        hit = cached.get(provenance)
        if hit is None:
            frame = fetch_daily_bars(chunk, start_utc, end_utc, feed=policy.feed)
            snapshot, _ = persist_snapshot(
                frame,
                root=args.data_root,
                source=SOURCE,
                schema_version="alpaca_daily_bars.v1",
                checks=_checks(frame, provenance),
                parent_snapshot_ids=(cohort_snapshot.dataset_id,),
            )
            snapshot.assert_usable()
        else:
            path, snapshot = hit
            frame = pl.read_parquet(path)
        frames.append(frame)
        parents.append(snapshot.dataset_id)
        print(
            json.dumps(
                {
                    "chunk": index,
                    "chunks": len(all_chunks),
                    "symbols": len(chunk),
                    "rows": frame.height,
                    "cached": hit is not None,
                }
            ),
            flush=True,
        )
    if len(selected_chunks) != len(all_chunks):
        print(
            json.dumps(
                {
                    "status": "shard_complete",
                    "chunk_start": args.chunk_start,
                    "chunk_end": chunk_end,
                    "rows": sum(frame.height for frame in frames),
                },
                sort_keys=True,
            )
        )
        return
    combined = pl.concat(frames).unique(("symbol", "ts_utc"), keep="last").sort(
        "symbol", "ts_utc"
    )
    provenance = f"{SOURCE}.combined@{query_start.isoformat()}..{query_end.isoformat()}"
    snapshot, path = persist_snapshot(
        combined,
        root=args.data_root,
        source=f"{SOURCE}.combined",
        schema_version="alpaca_daily_bars.v1",
        checks=_checks(combined, provenance),
        parent_snapshot_ids=tuple(parents),
    )
    snapshot.assert_usable()
    print(
        json.dumps(
            {
                "status": "complete",
                "symbols": len(symbols),
                "rows": combined.height,
                "dataset_id": snapshot.dataset_id,
                "path": str(path),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
