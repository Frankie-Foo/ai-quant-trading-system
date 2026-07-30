from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl
from dotenv import load_dotenv

from data_plane.calendar import build_xnys_schedule
from data_plane.contracts import DataQualityCheck, DatasetSnapshot, QualitySeverity
from data_plane.providers.alpaca import fetch_bars, stock_data_policy_from_env
from data_plane.quality import BAR_SCHEMA_VERSION
from data_plane.storage import persist_snapshot
from kernel.config import load_config
from kernel.features.momentum import premarket_window_utc, rvol
from research.history import (
    HISTORICAL_SELECTION_PROFILE,
    premarket_data_cutoff_utc,
    premarket_decision_asof_utc,
    premarket_feature_cutoff_et,
    required_premarket_symbols,
)
from schedule.runtime import ProcessLock
from scripts.build_premarket_rvol import (
    FEATURE_SOURCE,
    HISTORY_SESSIONS,
    RAW_SOURCE,
    _feature_checks,
    _query_provenance,
    _raw_checks,
)

ROOT = Path(__file__).resolve().parents[1]
INDEX_SOURCE = "research.history.pit_selection_index"
RVOL_INDEX_SOURCE = "research.history.rvol_index"


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


def _latest_index(data_root: Path, end_date: date) -> tuple[pl.DataFrame, DatasetSnapshot]:
    matches: list[tuple[datetime, Path, DatasetSnapshot]] = []
    for path in (data_root / "accepted").glob(f"{INDEX_SOURCE}-*/data.parquet"):
        frame = pl.read_parquet(path, columns=["trade_date"])
        if frame["trade_date"].max() != end_date:
            continue
        snapshot = _snapshot(path)
        matches.append((snapshot.asof_utc, path, snapshot))
    if not matches:
        raise FileNotFoundError(f"no completed PIT selection index ending {end_date}")
    _, path, snapshot = max(matches, key=lambda value: value[0])
    return pl.read_parquet(path), snapshot


def _dataset_paths(data_root: Path) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for path in (data_root / "accepted").glob("*/data.parquet"):
        result[_snapshot(path).dataset_id] = path
    return result


def _raw_cache(
    data_root: Path,
) -> dict[str, tuple[Path, DatasetSnapshot]]:
    result: dict[str, tuple[Path, DatasetSnapshot]] = {}
    for path in (data_root / "accepted").glob(f"{RAW_SOURCE}-*/data.parquet"):
        snapshot = _snapshot(path)
        for check in snapshot.checks:
            if check.provenance.startswith(f"{RAW_SOURCE}@"):
                current = result.get(check.provenance)
                if current is None or current[1].asof_utc < snapshot.asof_utc:
                    result[check.provenance] = (path, snapshot)
    return result


def _feature_cache(
    data_root: Path,
) -> dict[tuple[date, datetime, str, int, str], tuple[pl.DataFrame, DatasetSnapshot]]:
    result: dict[
        tuple[date, datetime, str, int, str],
        tuple[pl.DataFrame, DatasetSnapshot],
    ] = {}
    for path in (data_root / "accepted").glob(f"{FEATURE_SOURCE}-*/data.parquet"):
        snapshot = _snapshot(path)
        frame = pl.read_parquet(path)
        dates = frame.get_column("session_date").unique().to_list()
        decisions = frame.get_column("decision_asof_utc").unique().to_list()
        candidates = tuple(
            value
            for value in snapshot.parent_snapshot_ids
            if value.startswith("kernel.catalysts.overnight_candidates-")
        )
        delays = (
            frame.get_column("provider_delay_minutes").unique().to_list()
            if "provider_delay_minutes" in frame.columns
            else []
        )
        feeds = (
            frame.get_column("market_data_feed").unique().to_list()
            if "market_data_feed" in frame.columns
            else []
        )
        if (
            len(dates) != 1
            or not isinstance(dates[0], date)
            or len(decisions) != 1
            or not isinstance(decisions[0], datetime)
            or len(candidates) != 1
            or len(delays) != 1
            or not isinstance(delays[0], int)
            or len(feeds) != 1
            or not isinstance(feeds[0], str)
        ):
            continue
        key = (dates[0], decisions[0], candidates[0], delays[0], feeds[0])
        current = result.get(key)
        if current is None or current[1].asof_utc < snapshot.asof_utc:
            result[key] = (frame, snapshot)
    return result


def _chunks(values: tuple[str, ...], size: int) -> list[tuple[str, ...]]:
    if size <= 0:
        raise ValueError("chunk size must be positive")
    return [values[index : index + size] for index in range(0, len(values), size)]


def _check(name: str, passed: bool, observed: object, expected: str) -> DataQualityCheck:
    return DataQualityCheck(
        name=name,
        severity=QualitySeverity.CRITICAL,
        passed=passed,
        observed=str(observed),
        expected=expected,
        provenance=RVOL_INDEX_SOURCE,
    )


def main() -> None:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser()
    parser.add_argument("--end", type=_parse_date, required=True)
    parser.add_argument("--data-root", type=Path, default=ROOT / "data")
    parser.add_argument("--symbol-chunk-size", type=int, default=200)
    parser.add_argument("--plan-only", action="store_true")
    args = parser.parse_args()
    policy = stock_data_policy_from_env()

    index, index_snapshot = _latest_index(args.data_root, args.end)
    paths = _dataset_paths(args.data_root)
    candidate_frames: dict[date, pl.DataFrame] = {}
    candidate_snapshots: dict[date, DatasetSnapshot] = {}
    candidate_symbols: dict[date, tuple[str, ...]] = {}
    for row in index.iter_rows(named=True):
        trade_date = row["trade_date"]
        if not isinstance(trade_date, date):
            raise ValueError("PIT index contains an invalid trade date")
        snapshot_id = str(row["candidate_snapshot_id"])
        path = paths.get(snapshot_id)
        if path is None:
            raise FileNotFoundError(f"candidate snapshot is missing: {snapshot_id}")
        frame = pl.read_parquet(path).sort("symbol")
        candidate_frames[trade_date] = frame
        candidate_snapshots[trade_date] = _snapshot(path)
        candidate_symbols[trade_date] = tuple(frame["symbol"].to_list())

    targets = tuple(sorted(candidate_symbols))
    raw_prefetch_cutoff_et = max(
        premarket_feature_cutoff_et(
            target,
            provider_delay_minutes=policy.delay_minutes,
        )
        for target in targets
    )
    schedule = build_xnys_schedule(targets[0] - timedelta(days=60), targets[-1])
    plan = required_premarket_symbols(
        candidate_symbols,
        schedule=schedule,
        history_sessions=HISTORY_SESSIONS,
    )
    plan_result = {
        "status": "plan",
        "targets": len(targets),
        "query_sessions": len(plan),
        "symbol_session_pairs": sum(len(value) for value in plan.values()),
        "max_symbols_per_session": max(map(len, plan.values()), default=0),
        "symbol_chunk_size": args.symbol_chunk_size,
        "market_data_feed": policy.feed,
        "provider_delay_minutes": policy.delay_minutes,
        "estimated_requests_before_pagination": sum(
            len(_chunks(value, args.symbol_chunk_size)) for value in plan.values()
        ),
    }
    print(json.dumps(plan_result), flush=True)
    if args.plan_only:
        return

    raw_cache = _raw_cache(args.data_root)
    feature_cache = _feature_cache(args.data_root)
    raw_frames: dict[date, pl.DataFrame] = {}
    raw_snapshots: dict[date, DatasetSnapshot] = {}
    cache_hits = 0
    with ProcessLock(ROOT / "runs" / "historical-premarket.lock"):
        for index_number, (session_date, symbols) in enumerate(plan.items(), start=1):
            start_utc, end_utc = premarket_window_utc(
                session_date,
                raw_prefetch_cutoff_et,
            )
            provenance = _query_provenance(
                symbols,
                start_utc,
                end_utc,
                feed=policy.feed,
            )
            cached = raw_cache.get(provenance)
            if cached is None:
                parts = [
                    fetch_bars(chunk, start_utc, end_utc, feed=policy.feed)
                    for chunk in _chunks(symbols, args.symbol_chunk_size)
                ]
                frame = pl.concat(parts) if len(parts) > 1 else parts[0]
                checks = _raw_checks(
                    frame,
                    symbols=symbols,
                    start_utc=start_utc,
                    end_utc=end_utc,
                    provenance=provenance,
                )
                dependent = tuple(
                    candidate_snapshots[target].dataset_id
                    for target, target_symbols in candidate_symbols.items()
                    if session_date <= target
                    and session_date
                    in schedule.filter(pl.col("trade_date") <= target)
                    .get_column("trade_date")
                    .tail(HISTORY_SESSIONS + 1)
                    .to_list()
                    and set(symbols).intersection(target_symbols)
                )
                snapshot, path = persist_snapshot(
                    frame,
                    root=args.data_root,
                    source=RAW_SOURCE,
                    schema_version=BAR_SCHEMA_VERSION,
                    checks=checks,
                    parent_snapshot_ids=dependent,
                )
                snapshot.assert_usable()
            else:
                path, snapshot = cached
                frame = pl.read_parquet(path)
                cache_hits += 1
            raw_frames[session_date] = frame
            raw_snapshots[session_date] = snapshot
            print(
                json.dumps(
                    {
                        "event": "raw_session_complete",
                        "completed": index_number,
                        "total": len(plan),
                        "trade_date": session_date.isoformat(),
                        "symbols": len(symbols),
                        "rows": frame.height,
                        "cached": cached is not None,
                    }
                ),
                flush=True,
            )

        cfg = load_config(ROOT / "config.yaml")
        output_rows: list[dict[str, object]] = []
        for index_number, target in enumerate(targets, start=1):
            symbols = candidate_symbols[target]
            decision_asof = premarket_decision_asof_utc(target)
            data_cutoff_utc = premarket_data_cutoff_utc(
                target,
                provider_delay_minutes=policy.delay_minutes,
            )
            cutoff_et = premarket_feature_cutoff_et(
                target,
                provider_delay_minutes=policy.delay_minutes,
            )
            cached_feature = feature_cache.get(
                (
                    target,
                    decision_asof,
                    candidate_snapshots[target].dataset_id,
                    policy.delay_minutes,
                    policy.feed,
                )
            )
            session_dates = (
                schedule.filter(pl.col("trade_date") <= target)
                .get_column("trade_date")
                .tail(HISTORY_SESSIONS + 1)
                .to_list()
            )
            if cached_feature is not None:
                output, snapshot = cached_feature
            else:
                session_parts = [
                    raw_frames[value] for value in session_dates if value in raw_frames
                ]
                if session_parts:
                    bars = pl.concat(session_parts)
                else:
                    bars = pl.DataFrame(
                        schema={
                            "symbol": pl.String,
                            "ts_utc": pl.Datetime("ms", "UTC"),
                            "open": pl.Float64,
                            "high": pl.Float64,
                            "low": pl.Float64,
                            "close": pl.Float64,
                            "volume": pl.Int64,
                            "vwap": pl.Float64,
                        }
                    )
                features = rvol(
                    bars,
                    schedule=schedule,
                    target_date=target,
                    symbols=symbols,
                    complete_session_dates=session_dates,
                    cutoff_et=cutoff_et,
                    n=HISTORY_SESSIONS,
                    min_rvol=cfg.universe.min_rvol,
                    min_premarket_return=cfg.universe.min_premarket_return,
                    min_premarket_close_location=(
                        cfg.universe.min_premarket_close_location
                    ),
                    provenance=(
                        f"alpaca.{policy.feed}.split_adjusted"
                        f"[{len(session_dates)}sessions]"
                        f"@{data_cutoff_utc.isoformat()}"
                    ),
                ).with_columns(
                    pl.lit(decision_asof).cast(pl.Datetime("ms", "UTC")).alias("decision_asof_utc"),
                    pl.lit(policy.delay_minutes).alias("provider_delay_minutes"),
                    pl.lit(policy.feed).alias("market_data_feed"),
                    pl.lit(policy.is_realtime).alias("market_data_is_realtime"),
                )
                output = candidate_frames[target].join(features, on="symbol", how="left")
                checks = _feature_checks(
                    output,
                    symbols=symbols,
                    trade_date=target,
                    decision_asof_utc=decision_asof,
                    min_rvol=cfg.universe.min_rvol,
                    min_premarket_return=cfg.universe.min_premarket_return,
                    min_premarket_close_location=(
                        cfg.universe.min_premarket_close_location
                    ),
                )
                snapshot, _ = persist_snapshot(
                    output,
                    root=args.data_root,
                    source=FEATURE_SOURCE,
                    schema_version="premarket_rvol_candidates.v2",
                    checks=checks,
                    parent_snapshot_ids=(
                        candidate_snapshots[target].dataset_id,
                        *(
                            raw_snapshots[value].dataset_id
                            for value in session_dates
                            if value in raw_snapshots
                        ),
                    ),
                )
                snapshot.assert_usable()
            output_rows.append(
                {
                    "trade_date": target,
                    "selection_profile": HISTORICAL_SELECTION_PROFILE,
                    "locked_symbols": len(symbols),
                    "available_rvol": output.filter(pl.col("availability") == "available").height,
                    "rvol_pass": output.filter(pl.col("rvol_pass")).height,
                    "rvol_snapshot_id": snapshot.dataset_id,
                }
            )
            print(
                json.dumps(
                    {
                        "event": "rvol_session_complete",
                        "completed": index_number,
                        "total": len(targets),
                        "trade_date": target.isoformat(),
                        "rvol_pass": output_rows[-1]["rvol_pass"],
                        "cutoff_et_exclusive": cutoff_et.isoformat(),
                        "cached": cached_feature is not None,
                    }
                ),
                flush=True,
            )

    output_index = pl.DataFrame(output_rows).with_columns(pl.col("trade_date").cast(pl.Date))
    duplicates = output_index.height - output_index["trade_date"].n_unique()
    checks = (
        _check(
            "exact_target_count",
            output_index.height == len(targets),
            output_index.height,
            str(len(targets)),
        ),
        _check("unique_trade_date", duplicates == 0, duplicates, "0"),
    )
    snapshot, path = persist_snapshot(
        output_index,
        root=args.data_root,
        source=RVOL_INDEX_SOURCE,
        schema_version="historical_rvol_index.v1",
        checks=checks,
        parent_snapshot_ids=(
            index_snapshot.dataset_id,
            *(str(row["rvol_snapshot_id"]) for row in output_rows),
        ),
    )
    snapshot.assert_usable()
    print(
        json.dumps(
            {
                "status": "complete",
                "targets": len(targets),
                "raw_sessions": len(raw_frames),
                "cache_hits": cache_hits,
                "rvol_pass": int(output_index["rvol_pass"].sum()),
                "dataset_id": snapshot.dataset_id,
                "path": str(path),
            }
        )
    )


if __name__ == "__main__":
    main()
