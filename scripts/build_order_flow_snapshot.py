"""Build a point-in-time consolidated-tape order-flow shadow snapshot."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import polars as pl

from data_plane.contracts import DataQualityCheck, DatasetSnapshot, QualitySeverity
from data_plane.providers.alpaca import fetch_quotes, fetch_trades
from data_plane.snapshot_queries import load_latest_session_snapshot
from data_plane.storage import persist_snapshot
from kernel.config import load_config
from kernel.features.order_flow import order_flow_features

ROOT = Path(__file__).resolve().parents[1]
CATALYST_SOURCE = "kernel.universe.selection_gates"
FACTOR_SOURCE = "kernel.selection.factor_candidates_shadow"
RAW_TRADE_SOURCE = "raw.alpaca.sip_trades"
RAW_QUOTE_SOURCE = "raw.alpaca.sip_quotes"
SOURCE = "kernel.features.order_flow_shadow"
BATCH_SIZE = 100
NEW_YORK = ZoneInfo("America/New_York")


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


def _parse_asof(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("asof must be an ISO-8601 timestamp") from exc
    if result.tzinfo is None:
        raise argparse.ArgumentTypeError("asof must include a timezone")
    return result.astimezone(UTC)


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


def build_order_flow_snapshot(
    trades: pl.DataFrame,
    quotes: pl.DataFrame,
    *,
    symbols: tuple[str, ...],
    trade_snapshot: DatasetSnapshot,
    quote_snapshot: DatasetSnapshot,
    data_root: Path,
    asof_utc: datetime,
    window: timedelta,
    provenance: str,
) -> tuple[pl.DataFrame, DatasetSnapshot, Path]:
    """Persist the order-flow feature module behind an auditable shadow interface."""

    frame = order_flow_features(
        trades,
        quotes,
        symbols=symbols,
        asof_utc=asof_utc,
        window=window,
        provenance=provenance,
    ).with_columns(
        pl.lit(asof_utc.astimezone(NEW_YORK).date()).alias("session_date")
    )
    requested = set(symbols)
    actual = set(frame.get_column("symbol").to_list())
    duplicate_count = frame.height - frame.get_column("symbol").n_unique()
    future_count = frame.filter(pl.col("data_cutoff_utc") > asof_utc).height
    production_count = frame.filter(pl.col("production_eligible")).height
    check_provenance = f"{SOURCE}@{asof_utc.isoformat()}"
    checks = (
        _check(
            "exact_requested_symbols",
            actual == requested,
            sorted(actual),
            str(sorted(requested)),
            check_provenance,
        ),
        _check(
            "unique_symbol",
            duplicate_count == 0,
            duplicate_count,
            "0 duplicate symbols",
            check_provenance,
        ),
        _check(
            "point_in_time_cutoff",
            future_count == 0,
            future_count,
            "0 rows after declared asof",
            check_provenance,
        ),
        _check(
            "no_production_eligibility",
            production_count == 0,
            production_count,
            "0 production-eligible rows while order flow is shadow-only",
            check_provenance,
        ),
    )
    snapshot, path = persist_snapshot(
        frame,
        root=data_root,
        source=SOURCE,
        schema_version="order_flow_shadow.v1",
        checks=checks,
        parent_snapshot_ids=(
            trade_snapshot.dataset_id,
            quote_snapshot.dataset_id,
        ),
    )
    snapshot.assert_usable()
    return frame, snapshot, path


def _candidate_symbols(
    catalyst: pl.DataFrame,
    factor: pl.DataFrame,
) -> tuple[str, ...]:
    catalyst_symbols = catalyst.filter(pl.col("pass_gate")).get_column("symbol")
    factor_symbols = factor.filter(pl.col("factor_pass")).get_column("symbol")
    return tuple(
        sorted(
            set(str(value) for value in catalyst_symbols)
            | set(str(value) for value in factor_symbols)
        )
    )


def _fetch_in_batches(
    kind: Literal["trades", "quotes"],
    symbols: tuple[str, ...],
    start_utc: datetime,
    end_utc: datetime,
) -> pl.DataFrame:
    frames: list[pl.DataFrame] = []
    fetcher = fetch_trades if kind == "trades" else fetch_quotes
    for offset in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[offset : offset + BATCH_SIZE]
        frames.append(fetcher(batch, start_utc, end_utc, feed="sip"))
    if not frames:
        raise ValueError("order-flow collection requires at least one candidate")
    return pl.concat(frames)


def _persist_raw(
    frame: pl.DataFrame,
    *,
    data_root: Path,
    source: str,
    schema_version: str,
    kind: Literal["trades", "quotes"],
    symbols: tuple[str, ...],
    start_utc: datetime,
    asof_utc: datetime,
    parent_snapshot_ids: tuple[str, ...],
) -> tuple[DatasetSnapshot, Path]:
    out_of_window = frame.filter(
        (pl.col("ts_utc") < start_utc) | (pl.col("ts_utc") > asof_utc)
    ).height
    unexpected_symbols = set(frame.get_column("symbol").to_list()) - set(symbols)
    duplicate_count = (
        frame.height
        - frame.select(
            pl.struct(
                ["symbol", "ts_utc", "trade_id", "tape"]
                if kind == "trades"
                else [
                    "symbol",
                    "ts_utc",
                    "bid_price",
                    "ask_price",
                    "bid_size",
                    "ask_size",
                ]
            ).n_unique()
        ).item()
    )
    provenance = f"{source}@{asof_utc.isoformat()}"
    checks = (
        _check(
            "point_in_time_window",
            out_of_window == 0,
            out_of_window,
            "0 rows outside requested window",
            provenance,
        ),
        _check(
            "requested_symbols_only",
            not unexpected_symbols,
            sorted(unexpected_symbols),
            "no symbols outside the candidate union",
            provenance,
        ),
        _check(
            "unique_market_event",
            duplicate_count == 0,
            duplicate_count,
            "0 duplicate market events",
            provenance,
        ),
    )
    return persist_snapshot(
        frame,
        root=data_root,
        source=source,
        schema_version=schema_version,
        checks=checks,
        parent_snapshot_ids=parent_snapshot_ids,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trade-date", type=_parse_date, required=True)
    parser.add_argument("--asof-utc", type=_parse_asof)
    parser.add_argument("--window-minutes", type=int)
    parser.add_argument("--data-root", type=Path, default=ROOT / "data")
    args = parser.parse_args()

    asof_utc = args.asof_utc or datetime.now(UTC)
    if asof_utc.astimezone(NEW_YORK).date() != args.trade_date:
        raise ValueError("asof must fall on the requested New York trade date")
    cfg = load_config(ROOT / "config.yaml")
    window_minutes = args.window_minutes or cfg.order_flow.window_minutes
    window = timedelta(minutes=window_minutes)
    if window <= timedelta(0) or window > timedelta(days=1):
        raise ValueError("window-minutes must be in [1, 1440]")

    catalyst, catalyst_snapshot = load_latest_session_snapshot(
        args.data_root,
        source=CATALYST_SOURCE,
        session_date=args.trade_date,
    )
    factor, factor_snapshot = load_latest_session_snapshot(
        args.data_root,
        source=FACTOR_SOURCE,
        session_date=args.trade_date,
    )
    symbols = _candidate_symbols(catalyst, factor)
    if not symbols:
        raise ValueError("candidate union is empty; there is no order flow to collect")

    start_utc = asof_utc - window
    request_end_utc = asof_utc + timedelta(microseconds=1)
    trades = _fetch_in_batches("trades", symbols, start_utc, request_end_utc)
    quotes = _fetch_in_batches("quotes", symbols, start_utc, request_end_utc)
    parent_ids = (
        catalyst_snapshot.dataset_id,
        factor_snapshot.dataset_id,
    )
    trade_snapshot, trade_path = _persist_raw(
        trades,
        data_root=args.data_root,
        source=RAW_TRADE_SOURCE,
        schema_version="sip_trades.v1",
        kind="trades",
        symbols=symbols,
        start_utc=start_utc,
        asof_utc=asof_utc,
        parent_snapshot_ids=parent_ids,
    )
    quote_snapshot, quote_path = _persist_raw(
        quotes,
        data_root=args.data_root,
        source=RAW_QUOTE_SOURCE,
        schema_version="sip_quotes.v1",
        kind="quotes",
        symbols=symbols,
        start_utc=start_utc,
        asof_utc=asof_utc,
        parent_snapshot_ids=parent_ids,
    )
    trade_snapshot.assert_usable()
    quote_snapshot.assert_usable()
    frame, snapshot, path = build_order_flow_snapshot(
        trades,
        quotes,
        symbols=symbols,
        trade_snapshot=trade_snapshot,
        quote_snapshot=quote_snapshot,
        data_root=args.data_root,
        asof_utc=asof_utc,
        window=window,
        provenance="cloud.alpaca.market_data.sip",
    )
    print(
        json.dumps(
            {
                "ok": True,
                "status": "shadow_complete",
                "trade_date": args.trade_date.isoformat(),
                "asof_utc": asof_utc.isoformat(),
                "symbols": len(symbols),
                "available": frame.filter(
                    pl.col("availability") == "available"
                ).height,
                "production_eligible": False,
                "trade_dataset_id": trade_snapshot.dataset_id,
                "trade_path": str(trade_path),
                "quote_dataset_id": quote_snapshot.dataset_id,
                "quote_path": str(quote_path),
                "dataset_id": snapshot.dataset_id,
                "path": str(path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
