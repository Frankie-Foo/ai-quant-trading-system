"""Backfill target-day premarket SIP bars for the daily event prefilter."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date, datetime
from pathlib import Path

import polars as pl

from data_plane.calendar import build_xnys_schedule
from data_plane.contracts import DataQualityCheck, DatasetSnapshot, QualitySeverity
from data_plane.providers.alpaca import fetch_bars, stock_data_policy_from_env
from data_plane.storage import persist_snapshot
from kernel.features.momentum import premarket_window_utc
from operations.local_env import load_project_env, project_data_root
from research.history import premarket_feature_cutoff_et

ROOT = Path(__file__).resolve().parents[1]
PREFILTER_SOURCE = "research.current_event_daily_prefilter"
SOURCE = "alpaca.sip.current_event_premarket"


def _snapshot(path: Path) -> DatasetSnapshot:
    return DatasetSnapshot.model_validate_json(
        (path.parent / "manifest.json").read_text(encoding="utf-8")
    )


def _latest_prefilter(data_root: Path) -> tuple[pl.DataFrame, DatasetSnapshot]:
    matches = [
        (_snapshot(path).asof_utc, path, _snapshot(path))
        for path in (data_root / "accepted").glob(f"{PREFILTER_SOURCE}-*/data.parquet")
    ]
    if not matches:
        raise FileNotFoundError("current event daily prefilter is missing")
    _, path, snapshot = max(matches)
    return pl.read_parquet(path), snapshot


def _provenance(target: date, symbols: tuple[str, ...]) -> str:
    digest = hashlib.sha256(",".join(symbols).encode()).hexdigest()[:16]
    return f"{SOURCE}@{target.isoformat()}|symbols={digest}"


def _cache(data_root: Path) -> dict[str, tuple[Path, DatasetSnapshot]]:
    result: dict[str, tuple[Path, DatasetSnapshot]] = {}
    for path in (data_root / "accepted").glob(f"{SOURCE}-*/data.parquet"):
        snapshot = _snapshot(path)
        for check in snapshot.checks:
            if check.provenance.startswith(f"{SOURCE}@"):
                result[check.provenance] = (path, snapshot)
    return result


def _checks(
    frame: pl.DataFrame,
    *,
    provenance: str,
    end_utc: datetime,
) -> tuple[DataQualityCheck, ...]:
    duplicates = frame.height - frame.select(
        pl.struct("symbol", "ts_utc").n_unique()
    ).item()
    future = frame.filter(pl.col("ts_utc") >= end_utc).height
    return (
        DataQualityCheck(
            name="provider_query_completed",
            severity=QualitySeverity.CRITICAL,
            passed=True,
            observed="complete",
            expected="complete",
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
        DataQualityCheck(
            name="no_post_decision_bar",
            severity=QualitySeverity.CRITICAL,
            passed=future == 0,
            observed=str(future),
            expected="0",
            provenance=provenance,
        ),
    )


def main() -> None:
    load_project_env(ROOT)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=project_data_root(ROOT))
    parser.add_argument("--session-start", type=int, default=1)
    parser.add_argument("--session-end", type=int)
    args = parser.parse_args()
    prefilter, prefilter_snapshot = _latest_prefilter(args.data_root)
    grouped = [
        (row["session_date"], tuple(sorted(row["symbols"])))
        for row in prefilter.group_by("session_date")
        .agg(pl.col("symbol").unique().alias("symbols"))
        .sort("session_date")
        .iter_rows(named=True)
    ]
    end_index = len(grouped) if args.session_end is None else args.session_end
    if args.session_start < 1 or end_index < args.session_start or end_index > len(grouped):
        raise ValueError("session range is outside the available sessions")
    selected = grouped[args.session_start - 1 : end_index]
    schedule = {
        row["trade_date"]: row
        for row in build_xnys_schedule(grouped[0][0], grouped[-1][0]).iter_rows(named=True)
    }
    policy = stock_data_policy_from_env()
    cached = _cache(args.data_root)
    failures: list[str] = []
    for offset, (target, symbols) in enumerate(selected, start=args.session_start):
        cutoff_et = premarket_feature_cutoff_et(target, provider_delay_minutes=0)
        start_utc, end_utc = premarket_window_utc(target, cutoff_et)
        if target not in schedule:
            failures.append(f"{target}:calendar_missing")
            continue
        provenance = _provenance(target, symbols)
        hit = cached.get(provenance)
        if hit is None:
            frame = fetch_bars(symbols, start_utc, end_utc, feed=policy.feed)
            snapshot, _ = persist_snapshot(
                frame,
                root=args.data_root,
                source=SOURCE,
                schema_version="current_event_premarket.v1",
                checks=_checks(frame, provenance=provenance, end_utc=end_utc),
                parent_snapshot_ids=(prefilter_snapshot.dataset_id,),
            )
            snapshot.assert_usable()
        else:
            path, snapshot = hit
            frame = pl.read_parquet(path)
        print(
            json.dumps(
                {
                    "session": offset,
                    "sessions": len(grouped),
                    "trade_date": target.isoformat(),
                    "symbols": len(symbols),
                    "rows": frame.height,
                    "cached": hit is not None,
                }
            ),
            flush=True,
        )
    print(
        json.dumps(
            {
                "status": "complete" if not failures else "partial",
                "session_start": args.session_start,
                "session_end": end_index,
                "failures": failures,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
