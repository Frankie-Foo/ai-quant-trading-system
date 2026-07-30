from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl
from dotenv import load_dotenv

from data_plane.calendar import build_xnys_schedule
from data_plane.contracts import DataQualityCheck, DatasetSnapshot, QualitySeverity
from data_plane.providers.massive import fetch_free_float, fetch_ticker_details
from data_plane.providers.nasdaq_events import (
    fetch_earnings_calendar,
    fetch_trade_halts,
)
from data_plane.storage import persist_snapshot
from kernel.config import load_config
from kernel.universe import apply_selection_gates

ROOT = Path(__file__).resolve().parents[1]


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


def _load_target_snapshot(
    data_root: Path,
    *,
    pattern: str,
    date_column: str,
    target_date: date,
) -> tuple[pl.DataFrame, DatasetSnapshot] | None:
    matches: list[tuple[datetime, Path, DatasetSnapshot]] = []
    for path in (data_root / "accepted").glob(pattern):
        frame = pl.read_parquet(path, columns=[date_column])
        values = frame.get_column(date_column).unique().to_list()
        if values != [target_date]:
            continue
        snapshot = DatasetSnapshot.model_validate(_manifest(path.parent / "manifest.json"))
        matches.append((snapshot.asof_utc, path, snapshot))
    if not matches:
        return None
    _, path, snapshot = max(matches)
    return pl.read_parquet(path), snapshot


def _load_locked(
    data_root: Path, target_date: date
) -> tuple[pl.DataFrame, DatasetSnapshot]:
    loaded = _load_target_snapshot(
        data_root,
        pattern="kernel.catalysts.overnight_candidates-*/data.parquet",
        date_column="session_date",
        target_date=target_date,
    )
    if loaded is None:
        raise FileNotFoundError(f"no catalyst lock for {target_date}")
    return loaded


def _load_daily(
    data_root: Path, previous_session: date
) -> tuple[pl.DataFrame, DatasetSnapshot]:
    loaded = _load_target_snapshot(
        data_root,
        pattern="kernel.universe.daily_precheck-*/data.parquet",
        date_column="asof_date",
        target_date=previous_session,
    )
    if loaded is None:
        raise FileNotFoundError(f"no daily universe for {previous_session}")
    return loaded


def _load_rvol(
    data_root: Path, target_date: date
) -> tuple[pl.DataFrame, DatasetSnapshot] | None:
    return _load_target_snapshot(
        data_root,
        pattern="kernel.premarket.rvol_candidates-*/data.parquet",
        date_column="session_date",
        target_date=target_date,
    )


def _check(
    name: str,
    severity: QualitySeverity,
    passed: bool,
    observed: object,
    expected: str,
    provenance: str,
) -> DataQualityCheck:
    return DataQualityCheck(
        name=name,
        severity=severity,
        passed=passed,
        observed=str(observed),
        expected=expected,
        provenance=provenance,
    )


def _store_reference(
    frame: pl.DataFrame,
    *,
    data_root: Path,
    source: str,
    schema_version: str,
    symbols: tuple[str, ...] | None = None,
    target_date: date | None = None,
    date_column: str | None = None,
    key_columns: tuple[str, ...] = ("symbol",),
    parent_ids: tuple[str, ...] = (),
    allow_empty: bool = False,
) -> DatasetSnapshot:
    provenance = f"{source}@{datetime.now(UTC).isoformat()}"
    duplicate_keys = (
        frame.height - frame.select(pl.struct(*key_columns).n_unique()).item()
        if frame.height
        else frame.height - frame.get_column("symbol").n_unique()
    )
    checks = [
        _check(
            "non_empty",
            QualitySeverity.CRITICAL,
            allow_empty or frame.height > 0,
            frame.height,
            "empty is a valid provider result" if allow_empty else "row_count > 0",
            provenance,
        ),
        _check(
            "unique_keys",
            QualitySeverity.CRITICAL,
            duplicate_keys == 0,
            duplicate_keys,
            "0 duplicate provider keys",
            provenance,
        ),
    ]
    if symbols is not None:
        actual = set(frame.get_column("symbol").to_list())
        missing = sorted(set(symbols) - actual)
        checks.append(
            _check(
                "requested_symbol_coverage",
                QualitySeverity.WARNING,
                not missing,
                len(missing),
                f"all {len(symbols)} requested symbols have provider records",
                provenance,
            )
        )
    if target_date is not None and date_column is not None:
        wrong = frame.filter(pl.col(date_column) != target_date).height
        checks.append(
            _check(
                "point_in_time_date",
                QualitySeverity.CRITICAL,
                wrong == 0,
                wrong,
                target_date.isoformat(),
                provenance,
            )
        )
    snapshot, _ = persist_snapshot(
        frame,
        root=data_root,
        source=source,
        schema_version=schema_version,
        checks=tuple(checks),
        parent_snapshot_ids=parent_ids,
    )
    snapshot.assert_usable()
    return snapshot


def _latest_source(
    data_root: Path,
    *,
    source: str,
    predicate: Callable[[pl.DataFrame], bool],
) -> tuple[pl.DataFrame, DatasetSnapshot] | None:
    matches: list[tuple[datetime, Path, DatasetSnapshot]] = []
    for path in (data_root / "accepted").glob(f"{source}-*/data.parquet"):
        frame = pl.read_parquet(path)
        if not predicate(frame):
            continue
        snapshot = DatasetSnapshot.model_validate(_manifest(path.parent / "manifest.json"))
        matches.append((snapshot.asof_utc, path, snapshot))
    if not matches:
        return None
    _, path, snapshot = max(matches)
    return pl.read_parquet(path), snapshot


def _counts(frame: pl.DataFrame, column: str) -> dict[str, int]:
    return {
        str(row[column]): int(row["len"])
        for row in frame.group_by(column).len().sort(column).iter_rows(named=True)
    }


def main() -> None:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser()
    parser.add_argument("--trade-date", type=_parse_date, required=True)
    parser.add_argument("--data-root", type=Path, default=ROOT / "data")
    parser.add_argument("--massive-pace-seconds", type=float, default=12.5)
    parser.add_argument("--refresh-events", action="store_true")
    args = parser.parse_args()

    schedule = build_xnys_schedule(args.trade_date - timedelta(days=15), args.trade_date)
    prior_dates = schedule.filter(pl.col("trade_date") < args.trade_date).get_column(
        "trade_date"
    ).tail(5).to_list()
    if len(prior_dates) != 5:
        raise ValueError("five prior XNYS sessions are required")
    previous_session = prior_dates[-1]
    candidates, locked_snapshot = _load_locked(args.data_root, args.trade_date)
    daily, daily_snapshot = _load_daily(args.data_root, previous_session)
    symbols = tuple(candidates.get_column("symbol").sort().to_list())
    symbol_set = set(symbols)

    earnings_cached = None if args.refresh_events else _latest_source(
        args.data_root,
        source="nasdaq.earnings_calendar",
        predicate=lambda frame: frame.get_column("trade_date").unique().to_list()
        == [args.trade_date],
    )
    if earnings_cached is None:
        earnings = fetch_earnings_calendar(args.trade_date)
        earnings_snapshot = _store_reference(
            earnings,
            data_root=args.data_root,
            source="nasdaq.earnings_calendar",
            schema_version="earnings_calendar.v1",
            target_date=args.trade_date,
            date_column="trade_date",
            allow_empty=True,
        )
    else:
        earnings, earnings_snapshot = earnings_cached

    requested_halt_dates = prior_dates + [args.trade_date]
    halts_cached = None if args.refresh_events else _latest_source(
        args.data_root,
        source="nasdaqtrader.trade_halts",
        predicate=lambda frame: set(frame.get_column("halt_date").unique().to_list())
        .intersection(requested_halt_dates)
        == set(requested_halt_dates),
    )
    if halts_cached is None:
        halts = fetch_trade_halts(requested_halt_dates)
        halt_snapshot = _store_reference(
            halts,
            data_root=args.data_root,
            source="nasdaqtrader.trade_halts",
            schema_version="trade_halts.v1",
            key_columns=("symbol", "halt_ts_utc"),
            allow_empty=True,
        )
    else:
        halts, halt_snapshot = halts_cached

    market_cached = _latest_source(
        args.data_root,
        source="massive.ticker_details",
        predicate=lambda frame: set(frame.get_column("symbol").to_list()) == symbol_set
        and frame.get_column("asof_date").unique().to_list() == [previous_session],
    )
    if market_cached is None:
        market = fetch_ticker_details(
            symbols,
            previous_session,
            pace_seconds=args.massive_pace_seconds,
        )
        market_snapshot = _store_reference(
            market,
            data_root=args.data_root,
            source="massive.ticker_details",
            schema_version="ticker_details.v1",
            symbols=symbols,
            target_date=previous_session,
            date_column="asof_date",
            parent_ids=(locked_snapshot.dataset_id,),
        )
    else:
        market, market_snapshot = market_cached

    # This endpoint returns only matched records, so a prior subset cannot prove that
    # it was requested for the same locked pool. It is a paginated table query rather
    # than one request per symbol; refresh it for every final gate build.
    floats = fetch_free_float(symbols)
    float_snapshot = _store_reference(
        floats,
        data_root=args.data_root,
        source="massive.free_float",
        schema_version="free_float.v1",
        symbols=symbols,
        parent_ids=(locked_snapshot.dataset_id,),
        allow_empty=True,
    )

    rvol_loaded = _load_rvol(args.data_root, args.trade_date)
    if rvol_loaded is None:
        result = {
            "trade_date": args.trade_date.isoformat(),
            "status": "reference_gates_ready_rvol_pending",
            "locked_symbols": len(symbols),
            "earnings_rows": earnings.height,
            "locked_earnings_symbols": len(symbol_set.intersection(earnings["symbol"])),
            "halt_rows": halts.height,
            "locked_market_caps": market.filter(pl.col("market_cap").is_not_null()).height,
            "locked_free_float": floats.height,
            "selection_snapshot": None,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    rvol_frame, rvol_snapshot = rvol_loaded
    cfg = load_config(ROOT / "config.yaml")
    gate_asof_utc = datetime.now(UTC)
    output = apply_selection_gates(
        daily,
        candidates,
        rvol_frame,
        market,
        earnings,
        halts,
        floats,
        trade_date=args.trade_date,
        asof_utc=gate_asof_utc,
        recent_session_dates=prior_dates,
        cfg=cfg,
        low_float_shares=cfg.universe.luld_low_float_shares,
    )
    actual = set(output.get_column("symbol").to_list())
    duplicate_count = output.height - output.get_column("symbol").n_unique()
    invalid_pass = output.filter(
        pl.col("pass_gate")
        & (
            (pl.col("rvol") <= cfg.universe.min_rvol)
            | ~pl.col("directional_volume_confirmed")
            | pl.col("earnings_day")
            | pl.col("current_halt")
            | pl.col("luld_risk")
            | pl.col("market_cap").is_null()
        )
    ).height
    checks = (
        _check(
            "exact_locked_pool",
            QualitySeverity.CRITICAL,
            actual == symbol_set,
            len(actual),
            f"exactly {len(symbols)} symbols",
            "kernel.universe.apply_selection_gates",
        ),
        _check(
            "unique_symbol",
            QualitySeverity.CRITICAL,
            duplicate_count == 0,
            duplicate_count,
            "0 duplicate symbols",
            "kernel.universe.apply_selection_gates",
        ),
        _check(
            "hard_gates_enforced",
            QualitySeverity.CRITICAL,
            invalid_pass == 0,
            invalid_pass,
            "0 passing rows violating a hard gate",
            "kernel.universe.apply_selection_gates",
        ),
    )
    parent_ids = (
        locked_snapshot.dataset_id,
        daily_snapshot.dataset_id,
        rvol_snapshot.dataset_id,
        earnings_snapshot.dataset_id,
        halt_snapshot.dataset_id,
        market_snapshot.dataset_id,
        float_snapshot.dataset_id,
    )
    snapshot, path = persist_snapshot(
        output,
        root=args.data_root,
        source="kernel.universe.selection_gates",
        schema_version="selection_gates.v2",
        checks=checks,
        parent_snapshot_ids=parent_ids,
    )
    snapshot.assert_usable()
    result = {
        "trade_date": args.trade_date.isoformat(),
        "status": "complete",
        "locked_symbols": len(symbols),
        "passes": output.filter(pl.col("pass_gate")).height,
        "rejections": _counts(
            output.filter(~pl.col("pass_gate")), "reject_reason"
        ),
        "dataset_id": snapshot.dataset_id,
        "path": str(path),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
