"""Freeze accepted selection-gate winners for causal H30 research."""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

import polars as pl

from data_plane.contracts import DataQualityCheck, DatasetSnapshot, QualitySeverity
from data_plane.storage import persist_snapshot
from operations.local_env import project_data_root

ROOT = Path(__file__).resolve().parents[1]
GATE_SOURCE = "kernel.universe.selection_gates"
SOURCE = "research.h30_candidate_cohort"
MIN_MARKET_CAP_USD = 1_000_000_000.0


def _manifest(path: Path) -> DatasetSnapshot:
    return DatasetSnapshot.model_validate_json(
        (path.parent / "manifest.json").read_text(encoding="utf-8")
    )


def latest_gate_paths(data_root: Path) -> dict[date, tuple[Path, DatasetSnapshot]]:
    """Return the latest accepted gate snapshot for every single-session date."""
    result: dict[date, tuple[Path, DatasetSnapshot]] = {}
    for path in (data_root / "accepted").glob(f"{GATE_SOURCE}-*/data.parquet"):
        frame = pl.read_parquet(path, columns=["session_date"])
        dates = frame.get_column("session_date").unique().to_list()
        if len(dates) != 1 or not isinstance(dates[0], date):
            continue
        snapshot = _manifest(path)
        current = result.get(dates[0])
        if current is None or current[1].asof_utc < snapshot.asof_utc:
            result[dates[0]] = (path, snapshot)
    return result


def build_candidate_cohort(
    gates: dict[date, tuple[Path, DatasetSnapshot]],
    *,
    start: date | None = None,
    end: date | None = None,
) -> tuple[pl.DataFrame, tuple[str, ...]]:
    rows: list[pl.DataFrame] = []
    parents: list[str] = []
    for session_date, (path, snapshot) in sorted(gates.items()):
        if start is not None and session_date < start:
            continue
        if end is not None and session_date > end:
            continue
        frame = pl.read_parquet(path).filter(
            pl.col("pass_gate").fill_null(False)
            & (pl.col("market_cap").fill_null(0) >= MIN_MARKET_CAP_USD)
        )
        if frame.is_empty():
            continue
        rows.append(
            frame.select(
                "session_date",
                "symbol",
                "selection_rank",
                "market_cap",
                "market_cap_asof_date",
                "market_cap_provenance",
                "rvol",
                "rvol_provenance",
                "catalyst_categories",
                "evidence_sources",
                "evidence_event_ids",
                "gate_asof_utc",
            ).with_columns(pl.lit(snapshot.dataset_id).alias("gate_snapshot_id"))
        )
        parents.append(snapshot.dataset_id)
    if not rows:
        raise FileNotFoundError("no accepted selection-gate winners satisfy the cohort")
    cohort = (
        pl.concat(rows, how="diagonal_relaxed")
        .sort("session_date", "selection_rank", "symbol")
        .unique(("session_date", "symbol"), keep="last")
    )
    return cohort, tuple(parents)


def _check(name: str, passed: bool, observed: object, expected: str) -> DataQualityCheck:
    return DataQualityCheck(
        name=name,
        severity=QualitySeverity.CRITICAL,
        passed=passed,
        observed=str(observed),
        expected=expected,
        provenance=SOURCE,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--end", type=date.fromisoformat)
    parser.add_argument("--data-root", type=Path, default=project_data_root(ROOT))
    args = parser.parse_args()
    if args.start and args.end and args.end < args.start:
        raise ValueError("end date must not precede start date")

    cohort, parents = build_candidate_cohort(
        latest_gate_paths(args.data_root), start=args.start, end=args.end
    )
    duplicates = cohort.height - cohort.select("session_date", "symbol").unique().height
    minimum_market_cap = cohort.get_column("market_cap").min()
    if not isinstance(minimum_market_cap, (int, float)):
        raise ValueError("candidate cohort market cap must be numeric")
    invalid_asof = cohort.filter(
        pl.col("market_cap_asof_date") > pl.col("session_date")
    ).height
    snapshot, path = persist_snapshot(
        cohort,
        root=args.data_root,
        source=SOURCE,
        schema_version="h30_candidate_cohort.v1",
        checks=(
            _check("non_empty", cohort.height > 0, cohort.height, ">0"),
            _check("unique_candidate_day", duplicates == 0, duplicates, "0"),
            _check(
                "minimum_market_cap",
                minimum_market_cap >= MIN_MARKET_CAP_USD,
                minimum_market_cap,
                f">={MIN_MARKET_CAP_USD}",
            ),
            _check("causal_market_cap_asof", invalid_asof == 0, invalid_asof, "0"),
        ),
        parent_snapshot_ids=parents,
    )
    snapshot.assert_usable()
    print(
        json.dumps(
            {
                "status": "complete",
                "sessions": cohort.get_column("session_date").n_unique(),
                "candidate_symbol_days": cohort.height,
                "dataset_id": snapshot.dataset_id,
                "path": str(path),
                "production_eligible": False,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
