"""Backfill 20-session premarket history for +4% event-gap candidates."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date, timedelta
from pathlib import Path

import polars as pl

from data_plane.calendar import build_xnys_schedule
from data_plane.contracts import DatasetSnapshot
from data_plane.providers.alpaca import fetch_bars, stock_data_policy_from_env
from data_plane.storage import persist_snapshot
from kernel.features.momentum import premarket_window_utc
from operations.local_env import load_project_env, project_data_root
from research.history import premarket_feature_cutoff_et, required_premarket_symbols
from scripts.backfill_event_premarket_current import _checks

ROOT = Path(__file__).resolve().parents[1]
GAP_SOURCE = "research.current_event_premarket_gap"
SOURCE = "alpaca.sip.current_event_premarket_rvol_history"
HISTORY_SESSIONS = 20


def _snapshot(path: Path) -> DatasetSnapshot:
    return DatasetSnapshot.model_validate_json(
        (path.parent / "manifest.json").read_text(encoding="utf-8")
    )


def _latest_gap(data_root: Path) -> tuple[pl.DataFrame, DatasetSnapshot]:
    matches = [
        (_snapshot(path).asof_utc, path, _snapshot(path))
        for path in (data_root / "accepted").glob(f"{GAP_SOURCE}-*/data.parquet")
    ]
    if not matches:
        raise FileNotFoundError("current event premarket gap cohort is missing")
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


def main() -> None:
    load_project_env(ROOT)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=project_data_root(ROOT))
    parser.add_argument("--session-start", type=int, default=1)
    parser.add_argument("--session-end", type=int)
    args = parser.parse_args()
    gap, gap_snapshot = _latest_gap(args.data_root)
    target_symbols = {
        row["session_date"]: tuple(sorted(row["symbols"]))
        for row in gap.group_by("session_date")
        .agg(pl.col("symbol").unique().alias("symbols"))
        .iter_rows(named=True)
    }
    targets = sorted(target_symbols)
    schedule = build_xnys_schedule(targets[0] - timedelta(days=60), targets[-1])
    plan = list(
        required_premarket_symbols(
            target_symbols,
            schedule=schedule,
            history_sessions=HISTORY_SESSIONS,
        ).items()
    )
    end_index = len(plan) if args.session_end is None else args.session_end
    if args.session_start < 1 or end_index < args.session_start or end_index > len(plan):
        raise ValueError("session range is outside the available plan")
    selected = plan[args.session_start - 1 : end_index]
    policy = stock_data_policy_from_env()
    cached = _cache(args.data_root)
    for offset, (session_date, symbols) in enumerate(selected, start=args.session_start):
        cutoff_et = premarket_feature_cutoff_et(session_date, provider_delay_minutes=0)
        start_utc, end_utc = premarket_window_utc(session_date, cutoff_et)
        provenance = _provenance(session_date, symbols)
        hit = cached.get(provenance)
        if hit is None:
            frame = fetch_bars(symbols, start_utc, end_utc, feed=policy.feed)
            snapshot, _ = persist_snapshot(
                frame,
                root=args.data_root,
                source=SOURCE,
                schema_version="current_event_premarket_rvol_history.v1",
                checks=_checks(frame, provenance=provenance, end_utc=end_utc),
                parent_snapshot_ids=(gap_snapshot.dataset_id,),
            )
            snapshot.assert_usable()
        else:
            path, snapshot = hit
            frame = pl.read_parquet(path)
        print(
            json.dumps(
                {
                    "session": offset,
                    "sessions": len(plan),
                    "trade_date": session_date.isoformat(),
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
                "status": "complete",
                "session_start": args.session_start,
                "session_end": end_index,
                "planned_sessions": len(plan),
                "symbol_session_pairs": sum(len(symbols) for _, symbols in plan),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
