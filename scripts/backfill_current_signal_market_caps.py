"""Backfill strictly prior-session market caps for preliminary modern signals."""

from __future__ import annotations

import argparse
import json
import time
from datetime import date, timedelta
from pathlib import Path

import polars as pl

from data_plane.calendar import build_xnys_schedule
from data_plane.contracts import DataQualityCheck, DatasetSnapshot, QualitySeverity
from data_plane.providers.massive import fetch_ticker_details
from data_plane.storage import persist_snapshot
from operations.local_env import load_project_env, project_data_root

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "massive.ticker_details.current_modern_signals"


def _snapshot(path: Path) -> DatasetSnapshot:
    return DatasetSnapshot.model_validate_json(
        (path.parent / "manifest.json").read_text(encoding="utf-8")
    )


def _cache(data_root: Path) -> dict[tuple[date, str], tuple[Path, DatasetSnapshot]]:
    result: dict[tuple[date, str], tuple[Path, DatasetSnapshot]] = {}
    for pattern in (
        "massive.ticker_details-*",
        f"{SOURCE}-*",
        "sec.companyfacts.derived_market_cap-*",
    ):
        for path in (data_root / "accepted").glob(f"{pattern}/data.parquet"):
            snapshot = _snapshot(path)
            frame = pl.read_parquet(path)
            if not {"asof_date", "symbol", "market_cap"}.issubset(frame.columns):
                continue
            for row in frame.iter_rows(named=True):
                if row["market_cap"] is not None:
                    result[(row["asof_date"], str(row["symbol"]))] = (path, snapshot)
    return result


def _checks(frame: pl.DataFrame, target: date, symbol: str) -> tuple[DataQualityCheck, ...]:
    row = frame.row(0, named=True)
    provenance = f"{SOURCE}:{symbol}@{target.isoformat()}"

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
        check("exact_symbol", row["symbol"] == symbol, row["symbol"], symbol),
        check(
            "strict_asof_date",
            row["asof_date"] == target,
            row["asof_date"],
            target.isoformat(),
        ),
        check("provider_success", row["fetch_error"] is None, row["fetch_error"], "None"),
        check(
            "market_cap_available",
            isinstance(row["market_cap"], (int, float)) and row["market_cap"] > 0,
            row["market_cap"],
            ">0",
        ),
    )


def main() -> None:
    load_project_env(ROOT)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--signals", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, default=project_data_root(ROOT))
    parser.add_argument("--pace-seconds", type=float, default=12.5)
    args = parser.parse_args()
    if args.pace_seconds < 0:
        raise ValueError("pace seconds cannot be negative")
    signals = pl.read_parquet(args.signals)
    targets = sorted(signals.get_column("session_date").unique().to_list())
    sessions = build_xnys_schedule(targets[0] - timedelta(days=10), targets[-1]).get_column(
        "trade_date"
    ).to_list()
    previous = {
        target: max(item for item in sessions if item < target)
        for target in targets
    }
    requests = sorted(
        {
            (previous[row["session_date"]], str(row["symbol"]))
            for row in signals.select("session_date", "symbol").iter_rows(named=True)
        }
    )
    cached = _cache(args.data_root)
    failures: list[dict[str, str]] = []
    previous_started = 0.0
    for index, (asof_date, symbol) in enumerate(requests, start=1):
        hit = cached.get((asof_date, symbol))
        usable = True
        if hit is None:
            elapsed = time.monotonic() - previous_started
            if previous_started and elapsed < args.pace_seconds:
                time.sleep(args.pace_seconds - elapsed)
            previous_started = time.monotonic()
            frame = fetch_ticker_details((symbol,), asof_date, pace_seconds=0)
            checks = _checks(frame, asof_date, symbol)
            snapshot, _ = persist_snapshot(
                frame,
                root=args.data_root,
                source=SOURCE,
                schema_version="ticker_details.v1",
                checks=checks,
            )
            if not snapshot.usable:
                failures.append({"asof_date": asof_date.isoformat(), "symbol": symbol})
                usable = False
        print(
            json.dumps(
                {
                    "completed": index,
                    "total": len(requests),
                    "asof_date": asof_date.isoformat(),
                    "symbol": symbol,
                    "cached": hit is not None,
                    "usable": usable,
                }
            ),
            flush=True,
        )
    print(
        json.dumps(
            {"status": "complete", "requests": len(requests), "failures": failures},
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
