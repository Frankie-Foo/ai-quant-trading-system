from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl
from dotenv import load_dotenv

from data_plane.calendar import build_xnys_schedule
from data_plane.contracts import DataQualityCheck, DatasetSnapshot, QualitySeverity
from data_plane.providers.massive import (
    empty_free_float_frame,
    empty_ticker_details_frame,
    fetch_ticker_details,
)
from data_plane.providers.nasdaq_events import (
    fetch_earnings_calendar,
    fetch_trade_halts,
)
from data_plane.storage import persist_snapshot
from kernel.config import load_config
from kernel.universe import apply_selection_gates
from research.history import HISTORICAL_SELECTION_PROFILE, premarket_decision_asof_utc
from schedule.runtime import ProcessLock

ROOT = Path(__file__).resolve().parents[1]
PIT_INDEX_SOURCE = "research.history.pit_selection_index"
RVOL_INDEX_SOURCE = "research.history.rvol_index"
GATE_INDEX_SOURCE = "research.history.selection_gates_index"
GATE_SOURCE = "kernel.universe.selection_gates"


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


def _dataset_paths(data_root: Path) -> dict[str, Path]:
    return {
        _snapshot(path).dataset_id: path
        for path in (data_root / "accepted").glob("*/data.parquet")
    }


def _latest_index(
    data_root: Path, source: str, end_date: date
) -> tuple[pl.DataFrame, DatasetSnapshot]:
    matches: list[tuple[datetime, Path, DatasetSnapshot]] = []
    for path in (data_root / "accepted").glob(f"{source}-*/data.parquet"):
        frame = pl.read_parquet(path, columns=["trade_date"])
        if frame["trade_date"].max() != end_date:
            continue
        snapshot = _snapshot(path)
        matches.append((snapshot.asof_utc, path, snapshot))
    if not matches:
        raise FileNotFoundError(f"no {source} index ending {end_date}")
    _, path, snapshot = max(matches)
    return pl.read_parquet(path), snapshot


def _provenance_cache(
    data_root: Path, source: str
) -> dict[str, tuple[Path, DatasetSnapshot]]:
    result: dict[str, tuple[Path, DatasetSnapshot]] = {}
    for path in (data_root / "accepted").glob(f"{source}-*/data.parquet"):
        snapshot = _snapshot(path)
        for check in snapshot.checks:
            current = result.get(check.provenance)
            if current is None or current[1].asof_utc < snapshot.asof_utc:
                result[check.provenance] = (path, snapshot)
    return result


def _check(
    name: str,
    passed: bool,
    observed: object,
    expected: str,
    provenance: str,
    *,
    severity: QualitySeverity = QualitySeverity.CRITICAL,
) -> DataQualityCheck:
    return DataQualityCheck(
        name=name,
        severity=severity,
        passed=passed,
        observed=str(observed),
        expected=expected,
        provenance=provenance,
    )


def _event_checks(
    frame: pl.DataFrame,
    *,
    requested_date: date,
    date_column: str,
    provenance: str,
) -> tuple[DataQualityCheck, ...]:
    wrong = frame.filter(pl.col(date_column) != requested_date).height
    return (
        _check("provider_query_completed", True, True, "complete", provenance),
        _check("requested_date", wrong == 0, wrong, "0", provenance),
    )


def _market_provenance(target: date, asof_date: date, symbols: tuple[str, ...]) -> str:
    digest = hashlib.sha256(",".join(symbols).encode()).hexdigest()[:16]
    return (
        f"massive.ticker_details.history@{target.isoformat()}|"
        f"asof={asof_date.isoformat()}|symbols_sha256={digest}"
    )


def _market_checks(
    frame: pl.DataFrame,
    *,
    symbols: tuple[str, ...],
    asof_date: date,
    provenance: str,
) -> tuple[DataQualityCheck, ...]:
    actual = set(frame["symbol"].to_list())
    missing = set(symbols) - actual
    wrong_dates = frame.filter(pl.col("asof_date") != asof_date).height
    duplicates = frame.height - frame["symbol"].n_unique()
    return (
        _check("provider_query_completed", True, True, "complete", provenance),
        _check("unique_symbol", duplicates == 0, duplicates, "0", provenance),
        _check("point_in_time_date", wrong_dates == 0, wrong_dates, "0", provenance),
        _check(
            "requested_symbol_coverage",
            not missing,
            len(missing),
            "all requested symbols returned",
            provenance,
            severity=QualitySeverity.WARNING,
        ),
    )


def _gate_checks(
    frame: pl.DataFrame, *, target: date, locked_symbols: tuple[str, ...]
) -> tuple[DataQualityCheck, ...]:
    provenance = f"{HISTORICAL_SELECTION_PROFILE}.gates@{target.isoformat()}"
    duplicates = frame.height - frame["symbol"].n_unique()
    actual = set(frame["symbol"].to_list())
    invalid_pass = frame.filter(
        pl.col("pass_gate")
        & (
            (pl.col("rvol") <= 3.0)
            | pl.col("earnings_day")
            | pl.col("current_halt")
            | pl.col("luld_risk")
            | pl.col("market_cap").is_null()
        )
    ).height
    future = frame.filter(pl.col("gate_asof_utc") > premarket_decision_asof_utc(target)).height
    return (
        _check(
            "exact_locked_pool",
            actual == set(locked_symbols),
            len(actual),
            str(len(locked_symbols)),
            provenance,
        ),
        _check("unique_symbol", duplicates == 0, duplicates, "0", provenance),
        _check("hard_gates_enforced", invalid_pass == 0, invalid_pass, "0", provenance),
        _check("no_future_gate_asof", future == 0, future, "0", provenance),
    )


def main() -> None:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser()
    parser.add_argument("--end", type=_parse_date, required=True)
    parser.add_argument("--data-root", type=Path, default=ROOT / "data")
    parser.add_argument("--massive-pace-seconds", type=float, default=12.5)
    args = parser.parse_args()

    pit_index, pit_snapshot = _latest_index(args.data_root, PIT_INDEX_SOURCE, args.end)
    rvol_index, rvol_index_snapshot = _latest_index(
        args.data_root, RVOL_INDEX_SOURCE, args.end
    )
    if pit_index["trade_date"].to_list() != rvol_index["trade_date"].to_list():
        raise ValueError("PIT and RVOL index dates do not match")
    paths = _dataset_paths(args.data_root)
    targets = pit_index["trade_date"].to_list()
    schedule = build_xnys_schedule(targets[0] - timedelta(days=15), targets[-1])
    cfg = load_config(ROOT / "config.yaml")
    earnings_cache = _provenance_cache(args.data_root, "nasdaq.earnings_calendar")
    halts_cache = _provenance_cache(args.data_root, "nasdaqtrader.trade_halts")
    market_cache = _provenance_cache(args.data_root, "massive.ticker_details")
    event_frames: dict[date, tuple[pl.DataFrame, DatasetSnapshot]] = {}
    halt_frames: dict[date, tuple[pl.DataFrame, DatasetSnapshot]] = {}
    rows: list[dict[str, object]] = []

    with ProcessLock(ROOT / "runs" / "historical-selection-gates.lock"):
        required_halt_dates = schedule["trade_date"].to_list()
        for index_number, event_date in enumerate(required_halt_dates, start=1):
            halt_provenance = f"nasdaqtrader.trade_halts@{event_date.isoformat()}"
            cached = halts_cache.get(halt_provenance)
            if cached is None:
                frame = fetch_trade_halts([event_date])
                snapshot, _ = persist_snapshot(
                    frame,
                    root=args.data_root,
                    source="nasdaqtrader.trade_halts",
                    schema_version="trade_halts.v1",
                    checks=_event_checks(
                        frame,
                        requested_date=event_date,
                        date_column="halt_date",
                        provenance=halt_provenance,
                    ),
                )
                snapshot.assert_usable()
            else:
                path, snapshot = cached
                frame = pl.read_parquet(path)
            halt_frames[event_date] = (frame, snapshot)
            print(
                json.dumps(
                    {
                        "event": "halt_date_complete",
                        "completed": index_number,
                        "total": len(required_halt_dates),
                        "date": event_date.isoformat(),
                        "rows": frame.height,
                        "cached": cached is not None,
                    }
                ),
                flush=True,
            )

        for index_number, target in enumerate(targets, start=1):
            earnings_provenance = f"nasdaq.earnings_calendar@{target.isoformat()}"
            cached_earnings = earnings_cache.get(earnings_provenance)
            if cached_earnings is None:
                earnings = fetch_earnings_calendar(target)
                earnings_snapshot, _ = persist_snapshot(
                    earnings,
                    root=args.data_root,
                    source="nasdaq.earnings_calendar",
                    schema_version="earnings_calendar.v1",
                    checks=_event_checks(
                        earnings,
                        requested_date=target,
                        date_column="trade_date",
                        provenance=earnings_provenance,
                    ),
                )
                earnings_snapshot.assert_usable()
            else:
                path, earnings_snapshot = cached_earnings
                earnings = pl.read_parquet(path)
            event_frames[target] = (earnings, earnings_snapshot)

            pit_row = pit_index.filter(pl.col("trade_date") == target).row(0, named=True)
            rvol_row = rvol_index.filter(pl.col("trade_date") == target).row(0, named=True)
            candidate_path = paths[str(pit_row["candidate_snapshot_id"])]
            daily_path = paths[str(pit_row["daily_snapshot_id"])]
            rvol_path = paths[str(rvol_row["rvol_snapshot_id"])]
            candidates = pl.read_parquet(candidate_path).sort("symbol")
            daily = pl.read_parquet(daily_path)
            rvol_frame = pl.read_parquet(rvol_path)
            candidate_snapshot = _snapshot(candidate_path)
            daily_snapshot = _snapshot(daily_path)
            rvol_snapshot = _snapshot(rvol_path)
            locked_symbols = tuple(candidates["symbol"].to_list())
            previous_session = pit_row["previous_session"]
            if not isinstance(previous_session, date):
                raise ValueError("PIT index previous_session is invalid")

            potential = (
                rvol_frame.join(
                    daily.select("symbol", "price"),
                    on="symbol",
                    how="left",
                    validate="1:1",
                )
                .filter(
                    (pl.col("availability") == "available")
                    & (pl.col("rvol") > cfg.universe.min_rvol)
                    & pl.col("premarket_price_confirmation")
                    & (
                        (pl.col("premarket_close") / pl.col("price") - 1)
                        > cfg.universe.min_premarket_gap_return
                    )
                )["symbol"]
                .sort()
                .to_list()
            )
            market_symbols = tuple(str(value) for value in potential)
            market_provenance = _market_provenance(
                target, previous_session, market_symbols
            )
            cached_market = market_cache.get(market_provenance)
            if cached_market is None:
                market = (
                    fetch_ticker_details(
                        market_symbols,
                        previous_session,
                        pace_seconds=args.massive_pace_seconds,
                    )
                    if market_symbols
                    else empty_ticker_details_frame()
                )
                market_snapshot, _ = persist_snapshot(
                    market,
                    root=args.data_root,
                    source="massive.ticker_details",
                    schema_version="ticker_details.v1",
                    checks=_market_checks(
                        market,
                        symbols=market_symbols,
                        asof_date=previous_session,
                        provenance=market_provenance,
                    ),
                    parent_snapshot_ids=(candidate_snapshot.dataset_id,),
                )
                market_snapshot.assert_usable()
            else:
                path, market_snapshot = cached_market
                market = pl.read_parquet(path)

            prior_dates = (
                schedule.filter(pl.col("trade_date") < target)["trade_date"]
                .tail(5)
                .to_list()
            )
            if len(prior_dates) != 5:
                raise ValueError(f"five prior sessions unavailable for {target}")
            halt_parts = [halt_frames[value][0] for value in (*prior_dates, target)]
            halts = pl.concat(halt_parts).unique(
                subset=["symbol", "halt_ts_utc"], keep="last"
            )
            floats = empty_free_float_frame()
            output = apply_selection_gates(
                daily,
                candidates,
                rvol_frame,
                market,
                earnings,
                halts,
                floats,
                trade_date=target,
                asof_utc=premarket_decision_asof_utc(target),
                recent_session_dates=prior_dates,
                cfg=cfg,
                low_float_shares=cfg.universe.luld_low_float_shares,
            )
            checks = _gate_checks(
                output, target=target, locked_symbols=locked_symbols
            )
            halt_parent_ids = tuple(
                halt_frames[value][1].dataset_id for value in (*prior_dates, target)
            )
            gate_snapshot, _ = persist_snapshot(
                output,
                root=args.data_root,
                source=GATE_SOURCE,
                schema_version="selection_gates.v2",
                checks=checks,
                parent_snapshot_ids=(
                    candidate_snapshot.dataset_id,
                    daily_snapshot.dataset_id,
                    rvol_snapshot.dataset_id,
                    earnings_snapshot.dataset_id,
                    *halt_parent_ids,
                    market_snapshot.dataset_id,
                ),
            )
            gate_snapshot.assert_usable()
            rows.append(
                {
                    "trade_date": target,
                    "selection_profile": HISTORICAL_SELECTION_PROFILE,
                    "locked_symbols": len(locked_symbols),
                    "rvol_pass": len(market_symbols),
                    "final_pass": output.filter(pl.col("pass_gate")).height,
                    "unknown_float_policy": "fail_recent_luld_only",
                    "selection_gate_snapshot_id": gate_snapshot.dataset_id,
                }
            )
            print(
                json.dumps(
                    {
                        "event": "selection_gate_complete",
                        "completed": index_number,
                        "total": len(targets),
                        "trade_date": target.isoformat(),
                        "rvol_pass": len(market_symbols),
                        "final_pass": rows[-1]["final_pass"],
                    }
                ),
                flush=True,
            )

    output_index = pl.DataFrame(rows).with_columns(pl.col("trade_date").cast(pl.Date))
    duplicates = output_index.height - output_index["trade_date"].n_unique()
    checks = (
        _check(
            "exact_target_count",
            output_index.height == len(targets),
            output_index.height,
            str(len(targets)),
            GATE_INDEX_SOURCE,
        ),
        _check(
            "unique_trade_date",
            duplicates == 0,
            duplicates,
            "0",
            GATE_INDEX_SOURCE,
        ),
    )
    snapshot, path = persist_snapshot(
        output_index,
        root=args.data_root,
        source=GATE_INDEX_SOURCE,
        schema_version="historical_selection_gates_index.v1",
        checks=checks,
        parent_snapshot_ids=(
            pit_snapshot.dataset_id,
            rvol_index_snapshot.dataset_id,
            *(str(row["selection_gate_snapshot_id"]) for row in rows),
        ),
    )
    snapshot.assert_usable()
    print(
        json.dumps(
            {
                "status": "complete",
                "sessions": len(targets),
                "final_pass": int(output_index["final_pass"].sum()),
                "dataset_id": snapshot.dataset_id,
                "path": str(path),
            }
        )
    )


if __name__ == "__main__":
    main()
