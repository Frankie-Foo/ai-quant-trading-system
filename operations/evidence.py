"""Derive objective maturity metrics without changing human attestations."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl

from data_plane.contracts import DatasetSnapshot
from execution.session_ledger import PaperSessionLedger, PaperSessionStatus
from operations.readiness import MaturityEvidence

PIT_SOURCE = "research.history.pit_selection_index"
CENSUS_SOURCE = "research.history.trade_replay_census"
LABEL_SOURCE = "research.history.net_labels"
OOS_SOURCE = "research.validation.purged_oos_folds"


def _snapshot(path: Path) -> DatasetSnapshot:
    return DatasetSnapshot.model_validate_json(
        (path.parent / "manifest.json").read_text(encoding="utf-8")
    )


def _latest(data_root: Path, source: str) -> tuple[DatasetSnapshot, Path] | None:
    matches: list[tuple[datetime, DatasetSnapshot, Path]] = []
    for path in (data_root / "accepted").glob(f"{source}-*/data.parquet"):
        snapshot = _snapshot(path)
        if snapshot.source == source and snapshot.usable:
            matches.append((snapshot.asof_utc, snapshot, path))
    if not matches:
        return None
    _, snapshot, path = max(matches, key=lambda item: item[0])
    return snapshot, path


def _quote_cost_coverage(data_root: Path) -> float:
    labels = _latest(data_root, LABEL_SOURCE)
    if labels is None:
        return 0.0
    label_snapshot, label_path = labels
    label_count = pl.read_parquet(label_path, columns=["symbol"]).height
    census_id = next(
        (
            item
            for item in label_snapshot.parent_snapshot_ids
            if item.startswith(f"{CENSUS_SOURCE}-")
        ),
        None,
    )
    if census_id is None:
        return 0.0
    census_path = data_root / "accepted" / census_id / "data.parquet"
    if not census_path.is_file():
        return 0.0
    denominator = (
        pl.scan_parquet(census_path)
        .filter(pl.col("status").is_in(["labeled", "quote_unavailable"]))
        .select(pl.len())
        .collect()
        .item()
    )
    return label_count / denominator if denominator else 0.0


def _objective_snapshot_metrics(data_root: Path) -> tuple[int, int, int, float]:
    pit = _latest(data_root, PIT_SOURCE)
    labels = _latest(data_root, LABEL_SOURCE)
    folds = _latest(data_root, OOS_SOURCE)
    pit_sessions = (
        0
        if pit is None
        else pl.read_parquet(pit[1], columns=["trade_date"])["trade_date"].n_unique()
    )
    label_count = 0 if labels is None else labels[0].row_count
    fold_count = 0 if folds is None else folds[0].row_count
    return int(pit_sessions), label_count, fold_count, _quote_cost_coverage(data_root)


def _paper_metrics(order_db: Path) -> tuple[int, float, int]:
    if not order_db.is_file():
        return 0, 0.0, 0
    ledger = PaperSessionLedger(order_db)
    completed = tuple(
        record
        for record in ledger.records()
        if record.status is PaperSessionStatus.COMPLETED
    )
    reconciliation = (
        min(record.reconciliation_match_rate for record in completed)
        if completed
        else 0.0
    )
    with sqlite3.connect(order_db) as connection:
        has_orders = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'orders'"
        ).fetchone()
        duplicate_rows = (
            None
            if has_orders is None
            else connection.execute(
                """
                SELECT COALESCE(SUM(duplicate_count - 1), 0)
                FROM (
                    SELECT COUNT(*) AS duplicate_count
                    FROM orders
                    WHERE broker_order_id IS NOT NULL
                    GROUP BY broker_order_id
                    HAVING COUNT(*) > 1
                )
                """
            ).fetchone()
        )
    duplicate_count = 0 if duplicate_rows is None else int(duplicate_rows[0])
    return len(completed), reconciliation, duplicate_count


def refresh_maturity_evidence(
    *,
    data_root: Path,
    order_db: Path,
    existing: MaturityEvidence | None = None,
    asof_utc: datetime | None = None,
) -> MaturityEvidence:
    """Recompute program-owned fields and retain every external attestation."""
    pit_sessions, labels, folds, quote_coverage = _objective_snapshot_metrics(data_root)
    paper_sessions, reconciliation, duplicates = _paper_metrics(order_db)
    base = existing or MaturityEvidence(asof_utc=datetime.now(UTC))
    payload: dict[str, Any] = base.model_dump(mode="python")
    payload.update(
        {
            "asof_utc": asof_utc or datetime.now(UTC),
            "point_in_time_history_sessions": pit_sessions,
            "net_labeled_trade_count": labels,
            "purged_oos_fold_count": folds,
            "quote_cost_coverage": quote_coverage,
            "paper_trading_sessions": paper_sessions,
            "reconciliation_match_rate": reconciliation,
            "duplicate_order_count": duplicates,
        }
    )
    return MaturityEvidence.model_validate(payload)


def load_existing_evidence(path: Path) -> MaturityEvidence | None:
    if not path.is_file():
        return None
    return MaturityEvidence.model_validate_json(path.read_text(encoding="utf-8"))


def write_evidence_atomic(evidence: MaturityEvidence, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(evidence.model_dump(mode="json"), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)
