from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl
from dotenv import load_dotenv

from data_plane.calendar import build_xnys_schedule
from data_plane.contracts import DataQualityCheck, DatasetSnapshot, QualitySeverity
from data_plane.providers.alpaca import fetch_bars, stock_data_policy_from_env
from data_plane.quality import BAR_SCHEMA_VERSION, audit_minute_bars
from data_plane.storage import persist_snapshot
from kernel.config import load_config
from kernel.signals import orb5

ROOT = Path(__file__).resolve().parents[1]


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("asof must include a timezone")
    return parsed.astimezone(UTC)


def _manifest(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"manifest is not an object: {path}")
    return value


def _load_selection(
    data_root: Path, trade_date: date
) -> tuple[pl.DataFrame, DatasetSnapshot]:
    matches: list[tuple[datetime, Path, DatasetSnapshot]] = []
    for path in (data_root / "accepted").glob(
        "kernel.universe.selection_gates-*/data.parquet"
    ):
        frame = pl.read_parquet(path, columns=["session_date"])
        if frame.get_column("session_date").unique().to_list() != [trade_date]:
            continue
        snapshot = DatasetSnapshot.model_validate(_manifest(path.parent / "manifest.json"))
        matches.append((snapshot.asof_utc, path, snapshot))
    if not matches:
        raise FileNotFoundError(f"no completed selection gates for {trade_date}")
    _, path, snapshot = max(matches)
    return pl.read_parquet(path), snapshot


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


def _snapshot_status(
    signals: pl.DataFrame,
    *,
    query_end: datetime,
    market_close: datetime,
) -> str:
    """Never turn an intraday point-in-time snapshot into an all-day conclusion."""

    if query_end >= market_close:
        return "complete_session"
    if signals.filter(pl.col("reason") == "next_bar_unavailable_at_asof").height:
        return "in_progress_pending_confirmation"
    if signals.filter(pl.col("triggered")).height:
        return "in_progress_with_triggers"
    return "in_progress_no_trigger_yet"


def main() -> None:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser()
    parser.add_argument("--trade-date", type=_parse_date, required=True)
    parser.add_argument("--asof", type=_parse_utc)
    parser.add_argument("--data-root", type=Path, default=ROOT / "data")
    args = parser.parse_args()

    policy = stock_data_policy_from_env()
    now_floor = datetime.now(UTC).replace(second=0, microsecond=0)
    actual_asof_utc: datetime = args.asof or datetime.now(UTC).replace(
        second=0, microsecond=0
    )
    if actual_asof_utc > now_floor:
        raise ValueError("asof cannot be in the future")
    data_cutoff_utc = actual_asof_utc - timedelta(minutes=policy.delay_minutes)
    schedule = build_xnys_schedule(args.trade_date, args.trade_date)
    if schedule.height != 1:
        raise ValueError("target XNYS session is unavailable")
    session = schedule.row(0, named=True)
    market_open = session["market_open_utc"]
    market_close = session["market_close_utc"]
    if not isinstance(market_open, datetime) or not isinstance(market_close, datetime):
        raise ValueError("calendar timestamps are invalid")
    query_end = min(data_cutoff_utc, market_close)
    if query_end < market_open + timedelta(minutes=7):
        raise RuntimeError(
            "available SIP data does not yet contain ORB-5, breakout, and next fill bar"
        )

    selection, selection_snapshot = _load_selection(args.data_root, args.trade_date)
    survivors = selection.filter(pl.col("pass_gate")).sort("selection_rank")
    symbols = tuple(survivors.get_column("symbol").to_list())
    if not symbols:
        print(
            json.dumps(
                {
                    "trade_date": args.trade_date.isoformat(),
                    "status": "complete_no_gate_survivors",
                    "signals": 0,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    bars = fetch_bars(symbols, market_open, query_end, feed=policy.feed)
    raw_provenance = (
        f"alpaca.{policy.feed}.orb5@{market_open.isoformat()}..{query_end.isoformat()}|"
        f"delay={policy.delay_minutes}m"
    )
    raw_checks = audit_minute_bars(
        bars,
        provenance=raw_provenance,
        expected_symbols=(),
        research_approved=True,
    )
    raw_snapshot, _ = persist_snapshot(
        bars,
        root=args.data_root,
        source="alpaca.sip.orb5_1m",
        schema_version=BAR_SCHEMA_VERSION,
        checks=raw_checks,
        parent_snapshot_ids=(selection_snapshot.dataset_id,),
    )
    raw_snapshot.assert_usable()

    cfg = load_config(ROOT / "config.yaml")
    rows: list[dict[str, object]] = []
    for candidate in survivors.iter_rows(named=True):
        symbol = str(candidate["symbol"])
        signal = orb5(
            bars.filter(pl.col("symbol") == symbol),
            session_open_utc=market_open,
            asof_utc=query_end,
            rvol=float(candidate["rvol"]),
            min_rvol=cfg.universe.min_rvol,
        )
        row = asdict(signal)
        row.update(
            {
                "session_date": args.trade_date,
                "selection_rank": candidate["selection_rank"],
                "rvol": candidate["rvol"],
                "actual_asof_utc": actual_asof_utc,
                "data_cutoff_utc": data_cutoff_utc,
                "provider_delay_minutes": policy.delay_minutes,
                "market_data_feed": policy.feed,
                "market_data_realtime": policy.is_realtime,
                "actionability": "research_snapshot_only",
            }
        )
        rows.append(row)
    signals = pl.DataFrame(rows).sort("selection_rank", "symbol")
    triggered = signals.filter(pl.col("triggered"))
    duplicate_count = signals.height - signals.get_column("symbol").n_unique()
    future_entries = triggered.filter(pl.col("entry_ts_utc") >= query_end).height
    checks = (
        _check(
            "exact_gate_survivors",
            QualitySeverity.CRITICAL,
            set(signals.get_column("symbol")) == set(symbols),
            signals.height,
            f"exactly {len(symbols)} gate survivors",
            "kernel.signals.orb5",
        ),
        _check(
            "unique_symbol",
            QualitySeverity.CRITICAL,
            duplicate_count == 0,
            duplicate_count,
            "0 duplicate symbols",
            "kernel.signals.orb5",
        ),
        _check(
            "no_unfinished_entry_bar",
            QualitySeverity.CRITICAL,
            future_entries == 0,
            future_entries,
            "all entry bars start before the selected feed's point-in-time cutoff",
            "kernel.signals.orb5",
        ),
        _check(
            "research_snapshot_not_execution",
            QualitySeverity.INFO,
            False,
            f"feed={policy.feed}; realtime={policy.is_realtime}",
            "historical ORB output is research-only; Modern H15 is the sole Paper runtime",
            "scripts.monitor_modern_momentum_paper",
        ),
    )
    status = _snapshot_status(signals, query_end=query_end, market_close=market_close)
    pending = signals.filter(pl.col("reason") == "next_bar_unavailable_at_asof")
    snapshot, path = persist_snapshot(
        signals,
        root=args.data_root,
        source="kernel.signals.orb5_shadow",
        schema_version="orb5_signals.v2",
        checks=checks,
        parent_snapshot_ids=(selection_snapshot.dataset_id, raw_snapshot.dataset_id),
    )
    snapshot.assert_usable()
    print(
        json.dumps(
            {
                "trade_date": args.trade_date.isoformat(),
                "status": status,
                "gate_survivors": len(symbols),
                "triggered": triggered.height,
                "triggered_symbols": triggered.get_column("symbol").to_list(),
                "pending_confirmation": pending.height,
                "pending_symbols": pending.get_column("symbol").to_list(),
                "session_complete": query_end >= market_close,
                "market_data_feed": policy.feed,
                "market_data_realtime": policy.is_realtime,
                "data_cutoff_utc": data_cutoff_utc.isoformat(),
                "dataset_id": snapshot.dataset_id,
                "path": str(path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
