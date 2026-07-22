from __future__ import annotations

import argparse
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from data_plane.calendar import build_xnys_schedule
from data_plane.contracts import DataQualityCheck, DatasetSnapshot, QualitySeverity
from data_plane.storage import persist_snapshot
from kernel.catalysts import prepare_catalysts
from kernel.config import load_config
from research.history import (
    HISTORICAL_SELECTION_PROFILE,
    build_pit_selection_session,
    catalyst_lock_asof_utc,
    target_sessions,
)
from schedule.runtime import ProcessLock

ROOT = Path(__file__).resolve().parents[1]
DAILY_SOURCE = "kernel.universe.daily_precheck"
CANDIDATE_SOURCE = "kernel.catalysts.overnight_candidates"
INDEX_SOURCE = "research.history.pit_selection_index"


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


def _manifest(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"manifest is not an object: {path}")
    return value


def _snapshot(path: Path) -> DatasetSnapshot:
    return DatasetSnapshot.model_validate(_manifest(path.parent / "manifest.json"))


def _load_daily_history(data_root: Path) -> tuple[pl.DataFrame, dict[date, str]]:
    paths = sorted((data_root / "accepted").glob("massive.grouped_daily-*/data.parquet"))
    if not paths:
        raise FileNotFoundError("accepted Massive grouped-daily snapshots are missing")
    frame = (
        pl.scan_parquet([str(path) for path in paths])
        .select("symbol", "trade_date", "high", "low", "close", "volume")
        .collect()
        .unique(subset=["symbol", "trade_date"], keep="first")
        .sort("symbol", "trade_date")
    )
    by_date: dict[date, str] = {}
    for path in paths:
        value = pl.read_parquet(path, columns=["trade_date"]).get_column(
            "trade_date"
        ).unique().to_list()
        if len(value) == 1:
            by_date[value[0]] = _snapshot(path).dataset_id
    return frame, by_date


def _load_references(data_root: Path) -> list[tuple[date, pl.DataFrame, DatasetSnapshot]]:
    values: list[tuple[date, pl.DataFrame, DatasetSnapshot]] = []
    for path in (data_root / "accepted").glob(
        "massive.reference_tickers.cs-*/data.parquet"
    ):
        frame = pl.read_parquet(path)
        dates = frame.get_column("asof_date").unique().to_list()
        if len(dates) == 1:
            values.append((dates[0], frame, _snapshot(path)))
    if not values:
        raise FileNotFoundError("accepted PIT common-stock reference snapshots are missing")
    return sorted(values, key=lambda item: item[0])


def _load_news(data_root: Path) -> tuple[pl.DataFrame, DatasetSnapshot]:
    matches = [
        (_snapshot(path).asof_utc, path)
        for path in (data_root / "accepted").glob(
            "massive.news.history.combined-*/data.parquet"
        )
    ]
    if not matches:
        raise FileNotFoundError("combined Massive history news snapshot is missing")
    _, path = max(matches)
    return pl.read_parquet(path), _snapshot(path)


def _existing_by_date(
    data_root: Path, *, source: str, date_column: str
) -> dict[date, tuple[Path, DatasetSnapshot]]:
    result: dict[date, tuple[Path, DatasetSnapshot]] = {}
    for path in (data_root / "accepted").glob(f"{source}-*/data.parquet"):
        frame = pl.read_parquet(path, columns=[date_column])
        values = frame.get_column(date_column).unique().to_list()
        if len(values) != 1:
            continue
        snapshot = _snapshot(path)
        current = result.get(values[0])
        if current is None or current[1].asof_utc < snapshot.asof_utc:
            result[values[0]] = (path, snapshot)
    return result


def _check(
    name: str,
    passed: bool,
    observed: object,
    expected: str,
    provenance: str,
) -> DataQualityCheck:
    return DataQualityCheck(
        name=name,
        severity=QualitySeverity.CRITICAL,
        passed=passed,
        observed=str(observed),
        expected=expected,
        provenance=provenance,
    )


def _daily_checks(frame: pl.DataFrame, target: date) -> tuple[DataQualityCheck, ...]:
    provenance = f"{HISTORICAL_SELECTION_PROFILE}.daily@{target.isoformat()}"
    duplicates = frame.height - frame.get_column("symbol").n_unique()
    asof = frame.get_column("asof_date").unique().to_list()
    invalid = frame.filter(
        (pl.col("security_type") != "CS")
        | pl.col("price").is_null()
        | (pl.col("price") <= 0)
        | pl.col("pass_gate")
    ).height
    return (
        _check("unique_symbol", duplicates == 0, duplicates, "0", provenance),
        _check(
            "strict_point_in_time_asof",
            len(asof) == 1 and asof[0] < target,
            asof,
            f"one date before {target}",
            provenance,
        ),
        _check("candidate_precheck_valid", invalid == 0, invalid, "0", provenance),
    )


def _candidate_checks(
    frame: pl.DataFrame, target: date, universe: pl.DataFrame
) -> tuple[DataQualityCheck, ...]:
    provenance = f"{HISTORICAL_SELECTION_PROFILE}.lock@{target.isoformat()}"
    duplicates = frame.height - frame.get_column("symbol").n_unique()
    symbols = set(universe.filter(pl.col("precheck_pass"))["symbol"].to_list())
    outside = sum(value not in symbols for value in frame["symbol"].to_list())
    wrong_date = frame.filter(pl.col("session_date") != target).height
    future = frame.filter(pl.col("latest_event_utc") > catalyst_lock_asof_utc(target)).height
    return (
        _check("unique_symbol", duplicates == 0, duplicates, "0", provenance),
        _check("precheck_membership", outside == 0, outside, "0", provenance),
        _check("target_session", wrong_date == 0, wrong_date, "0", provenance),
        _check("no_future_event", future == 0, future, "0", provenance),
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--end", type=_parse_date, required=True)
    parser.add_argument("--sessions", type=int, default=252)
    parser.add_argument("--data-root", type=Path, default=ROOT / "data")
    args = parser.parse_args()

    targets = target_sessions(end_date=args.end, sessions=args.sessions)
    schedule = build_xnys_schedule(targets[0] - timedelta(days=10), targets[-1])
    daily, daily_ids = _load_daily_history(args.data_root)
    references = _load_references(args.data_root)
    news, news_snapshot = _load_news(args.data_root)
    prepared = prepare_catalysts(
        news,
        asof_utc=catalyst_lock_asof_utc(targets[-1]),
    )
    cfg = load_config(ROOT / "config.yaml")
    existing_daily = _existing_by_date(
        args.data_root, source=DAILY_SOURCE, date_column="asof_date"
    )
    existing_candidates = _existing_by_date(
        args.data_root, source=CANDIDATE_SOURCE, date_column="session_date"
    )
    rows: list[dict[str, object]] = []
    cache_hits = 0

    with ProcessLock(ROOT / "runs" / "historical-selection.lock"):
        for index, target in enumerate(targets, start=1):
            prior_daily_dates = sorted(value for value in daily_ids if value < target)[-300:]
            if not prior_daily_dates:
                raise ValueError(f"no prior daily data for {target}")
            previous_session = prior_daily_dates[-1]
            reference_matches = [item for item in references if item[0] < target]
            if not reference_matches:
                raise ValueError(f"no strictly prior reference snapshot for {target}")
            reference_date, reference, reference_snapshot = reference_matches[-1]

            daily_cached = existing_daily.get(previous_session)
            candidate_cached = existing_candidates.get(target)
            if daily_cached is not None and candidate_cached is not None:
                daily_path, daily_snapshot = daily_cached
                candidate_path, candidate_snapshot = candidate_cached
                universe = pl.read_parquet(daily_path)
                candidates = pl.read_parquet(candidate_path)
                cache_hits += 1
            else:
                universe, candidates = build_pit_selection_session(
                    daily=daily,
                    reference=reference,
                    prepared_news=prepared,
                    schedule=schedule,
                    trade_date=target,
                    cfg=cfg,
                    daily_provenance=(
                        f"massive.grouped_daily[{prior_daily_dates[0]}..{previous_session}]"
                    ),
                    reference_provenance=reference_snapshot.dataset_id,
                )
                daily_snapshot, daily_path = persist_snapshot(
                    universe,
                    root=args.data_root,
                    source=DAILY_SOURCE,
                    schema_version="universe_daily_precheck.v1",
                    checks=_daily_checks(universe, target),
                    parent_snapshot_ids=(
                        *(daily_ids[value] for value in prior_daily_dates),
                        reference_snapshot.dataset_id,
                    ),
                )
                daily_snapshot.assert_usable()
                candidate_snapshot, candidate_path = persist_snapshot(
                    candidates,
                    root=args.data_root,
                    source=CANDIDATE_SOURCE,
                    schema_version="catalyst_candidates.v1",
                    checks=_candidate_checks(candidates, target, universe),
                    parent_snapshot_ids=(
                        news_snapshot.dataset_id,
                        daily_snapshot.dataset_id,
                        reference_snapshot.dataset_id,
                    ),
                )
                candidate_snapshot.assert_usable()

            rows.append(
                {
                    "trade_date": target,
                    "previous_session": previous_session,
                    "reference_date": reference_date,
                    "lock_asof_utc": catalyst_lock_asof_utc(target),
                    "selection_profile": HISTORICAL_SELECTION_PROFILE,
                    "daily_rows": universe.height,
                    "precheck_pass": universe.filter(pl.col("precheck_pass")).height,
                    "candidate_count": candidates.height,
                    "daily_snapshot_id": daily_snapshot.dataset_id,
                    "candidate_snapshot_id": candidate_snapshot.dataset_id,
                }
            )
            print(
                json.dumps(
                    {
                        "event": "session_complete",
                        "completed": index,
                        "total": len(targets),
                        "trade_date": target.isoformat(),
                        "reference_date": reference_date.isoformat(),
                        "candidates": candidates.height,
                        "cached": daily_cached is not None and candidate_cached is not None,
                    }
                ),
                flush=True,
            )

    index_frame = pl.DataFrame(rows).with_columns(
        pl.col("trade_date").cast(pl.Date),
        pl.col("previous_session").cast(pl.Date),
        pl.col("reference_date").cast(pl.Date),
        pl.col("lock_asof_utc").cast(pl.Datetime("ms", "UTC")),
    )
    duplicates = index_frame.height - index_frame["trade_date"].n_unique()
    bad_reference_dates = index_frame.filter(
        pl.col("reference_date") >= pl.col("trade_date")
    ).height
    checks = (
        _check(
            "exact_session_count",
            index_frame.height == len(targets),
            index_frame.height,
            str(len(targets)),
            INDEX_SOURCE,
        ),
        _check("unique_trade_date", duplicates == 0, duplicates, "0", INDEX_SOURCE),
        _check(
            "strict_reference_dates",
            bad_reference_dates == 0,
            bad_reference_dates,
            "0",
            INDEX_SOURCE,
        ),
    )
    snapshot, path = persist_snapshot(
        index_frame,
        root=args.data_root,
        source=INDEX_SOURCE,
        schema_version="pit_selection_index.v1",
        checks=checks,
        parent_snapshot_ids=tuple(str(row["candidate_snapshot_id"]) for row in rows),
    )
    snapshot.assert_usable()
    print(
        json.dumps(
            {
                "status": "complete",
                "sessions": index_frame.height,
                "start": targets[0].isoformat(),
                "end": targets[-1].isoformat(),
                "candidates": int(index_frame["candidate_count"].sum()),
                "cache_hits": cache_hits,
                "dataset_id": snapshot.dataset_id,
                "path": str(path),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
