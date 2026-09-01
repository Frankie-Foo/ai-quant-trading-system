"""Backfill causal Alpaca SIP NBBO windows for H30 replay fills."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path

import polars as pl

from data_plane.contracts import DataQualityCheck, DatasetSnapshot, QualitySeverity
from data_plane.http import DownloadError
from data_plane.providers.alpaca import fetch_quotes
from data_plane.storage import persist_snapshot
from kernel.quote_costs import latest_nbbo_spread, window_nbbo_spread
from operations.local_env import load_project_env

ROOT = Path(__file__).resolve().parents[1]
LABEL_SOURCE = "research.h30_challenger.labels"
RAW_SOURCE = "research.h30_signal_nbbo.raw"
EVIDENCE_SOURCE = "research.h30_signal_nbbo.evidence"


def _manifest(path: Path) -> DatasetSnapshot:
    return DatasetSnapshot.model_validate_json(
        (path.parent / "manifest.json").read_text(encoding="utf-8")
    )


def _latest_labels(data_root: Path) -> tuple[pl.DataFrame, DatasetSnapshot]:
    matches = [
        (_manifest(path).asof_utc, path, _manifest(path))
        for path in (data_root / "accepted").glob(f"{LABEL_SOURCE}-*/data.parquet")
    ]
    if not matches:
        raise FileNotFoundError("H30 labels are missing")
    _, path, snapshot = max(matches, key=lambda item: item[0])
    frame = pl.read_parquet(path)
    return frame.unique(
        ("trade_date", "symbol", "attempt", "entry_ts_utc", "exit_ts_utc")
    ), snapshot


def _check(
    name: str,
    passed: bool,
    observed: object,
    expected: str,
    *,
    severity: QualitySeverity = QualitySeverity.CRITICAL,
) -> DataQualityCheck:
    return DataQualityCheck(
        name=name,
        severity=severity,
        passed=passed,
        observed=str(observed),
        expected=expected,
        provenance="scripts.backfill_h30_signal_nbbo.v1",
    )


def main() -> None:
    load_project_env(ROOT)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-root", type=Path, default=ROOT / "runtime" / "ai-quant" / "data"
    )
    parser.add_argument("--plan-only", action="store_true")
    args = parser.parse_args()
    labels, parent = _latest_labels(args.data_root)
    if args.plan_only:
        print(json.dumps({"signal_legs": labels.height, "windows": labels.height * 2}))
        return
    quote_parts: list[pl.DataFrame] = []
    window_rows: list[dict[str, object]] = []
    for index, row in enumerate(labels.iter_rows(named=True), start=1):
        symbol = str(row["symbol"])
        entry_ts = row["entry_ts_utc"]
        exit_ts = row["exit_ts_utc"]
        if not isinstance(entry_ts, datetime) or not isinstance(exit_ts, datetime):
            raise ValueError("H30 label timestamps are invalid")
        entry_window = (entry_ts - timedelta(seconds=30), entry_ts + timedelta(microseconds=1))
        exit_window = (exit_ts - timedelta(minutes=1), exit_ts + timedelta(microseconds=1))
        error: str | None = None
        try:
            parts = [
                fetch_quotes((symbol,), entry_window[0], entry_window[1]),
                fetch_quotes((symbol,), exit_window[0], exit_window[1]),
            ]
            quotes = (
                pl.concat(parts)
                .unique(("symbol", "ts_utc"), keep="last")
                .sort("symbol", "ts_utc")
            )
            quote_parts.append(quotes)
            entry = latest_nbbo_spread(
                quotes, symbol=symbol, at_utc=entry_ts, max_age=timedelta(seconds=30)
            )
            exit_spread = window_nbbo_spread(
                quotes,
                symbol=symbol,
                start_utc=exit_window[0],
                end_utc=exit_window[1],
                quantile=0.95,
            )
        except (DownloadError, ValueError) as exc:
            entry = None
            exit_spread = None
            error = type(exc).__name__
        window_rows.append(
            {
                "trade_date": row["trade_date"],
                "symbol": symbol,
                "attempt": row["attempt"],
                "entry_ts_utc": entry_ts,
                "exit_ts_utc": exit_ts,
                "entry_relative_spread": None if entry is None else entry.relative_spread,
                "entry_quote_age_seconds": None if entry is None else entry.age_seconds,
                "exit_relative_spread_p95": (
                    None if exit_spread is None else exit_spread.relative_spread
                ),
                "exit_quote_samples": None if exit_spread is None else exit_spread.sample_count,
                "entry_quote_provenance": None if entry is None else entry.provenance,
                "exit_quote_provenance": None if exit_spread is None else exit_spread.provenance,
                "fetch_error": error,
            }
        )
        print(json.dumps({"completed": index, "total": labels.height, "symbol": symbol}))
    if not quote_parts:
        raise RuntimeError("NBBO backfill returned no quotes")
    raw = pl.concat(quote_parts).unique(("symbol", "ts_utc"), keep="last").sort(
        "symbol", "ts_utc"
    )
    duplicates = raw.height - raw.select(pl.struct("symbol", "ts_utc").n_unique()).item()
    raw_snapshot, _ = persist_snapshot(
        raw,
        root=args.data_root,
        source=RAW_SOURCE,
        schema_version="sip_nbbo_quotes.v1",
        checks=(
            _check("non_empty", raw.height > 0, raw.height, ">0"),
            _check("unique_symbol_timestamp", duplicates == 0, duplicates, "0"),
            _check(
                "crossed_quotes_excluded_from_cost",
                raw.filter(pl.col("bid_price") > pl.col("ask_price")).height == 0,
                raw.filter(pl.col("bid_price") > pl.col("ask_price")).height,
                "0",
                severity=QualitySeverity.WARNING,
            ),
        ),
        parent_snapshot_ids=(parent.dataset_id,),
    )
    raw_snapshot.assert_usable()
    evidence = pl.DataFrame(window_rows).sort("trade_date", "symbol", "attempt")
    complete = evidence.filter(
        pl.col("entry_relative_spread").is_not_null()
        & pl.col("exit_relative_spread_p95").is_not_null()
    ).height
    evidence_snapshot, path = persist_snapshot(
        evidence,
        root=args.data_root,
        source=EVIDENCE_SOURCE,
        schema_version="h30_signal_nbbo_evidence.v1",
        checks=(
            _check("non_empty", evidence.height > 0, evidence.height, ">0"),
            _check(
                "complete_signal_windows",
                complete == evidence.height,
                complete,
                str(evidence.height),
                severity=QualitySeverity.WARNING,
            ),
        ),
        parent_snapshot_ids=(raw_snapshot.dataset_id, parent.dataset_id),
    )
    evidence_snapshot.assert_usable()
    print(
        json.dumps(
            {
                "status": "complete",
                "quote_rows": raw.height,
                "complete_windows": complete,
                "total_windows": evidence.height,
                "dataset_id": evidence_snapshot.dataset_id,
                "path": str(path),
            }
        )
    )


if __name__ == "__main__":
    main()
