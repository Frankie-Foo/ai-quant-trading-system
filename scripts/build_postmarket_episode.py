from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import polars as pl

from data_plane.calendar import build_xnys_schedule
from data_plane.contracts import DataQualityCheck, DatasetSnapshot, QualitySeverity
from data_plane.storage import persist_snapshot
from kernel.config import load_config
from research.postmortem import EPISODE_SCHEMA_VERSION, build_trading_episode

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


def _dated_snapshots(
    data_root: Path, pattern: str, trade_date: date
) -> list[tuple[DatasetSnapshot, Path]]:
    matches: list[tuple[DatasetSnapshot, Path]] = []
    for path in (data_root / "accepted").glob(pattern):
        frame = pl.read_parquet(path, columns=["session_date"])
        if frame.get_column("session_date").unique().to_list() != [trade_date]:
            continue
        snapshot = DatasetSnapshot.model_validate(_manifest(path.parent / "manifest.json"))
        matches.append((snapshot, path))
    return matches


def _latest_dated(
    data_root: Path, pattern: str, trade_date: date
) -> tuple[pl.DataFrame, DatasetSnapshot, Path]:
    matches = _dated_snapshots(data_root, pattern, trade_date)
    if not matches:
        raise FileNotFoundError(f"no accepted {pattern} snapshot for {trade_date}")
    snapshot, path = max(matches, key=lambda item: item[0].asof_utc)
    return pl.read_parquet(path), snapshot, path


def _load_full_session_signals(
    data_root: Path, trade_date: date, market_close_utc: datetime
) -> tuple[pl.DataFrame, DatasetSnapshot, Path]:
    complete: list[tuple[DatasetSnapshot, Path]] = []
    for snapshot, path in _dated_snapshots(
        data_root, "kernel.signals.orb5_shadow-*/data.parquet", trade_date
    ):
        frame = pl.read_parquet(path, columns=["data_cutoff_utc"])
        cutoff = frame.get_column("data_cutoff_utc").min()
        if isinstance(cutoff, datetime) and cutoff >= market_close_utc:
            complete.append((snapshot, path))
    if not complete:
        raise FileNotFoundError(f"no full-session ORB-5 snapshot for {trade_date}")
    snapshot, path = max(complete, key=lambda item: item[0].asof_utc)
    return pl.read_parquet(path), snapshot, path


def _load_score(
    data_root: Path, trade_date: date
) -> tuple[pl.DataFrame, DatasetSnapshot | None]:
    matches = _dated_snapshots(
        data_root,
        "research.catalysts.deepseek_v4_pro_shadow-*/data.parquet",
        trade_date,
    )
    if not matches:
        return (
            pl.DataFrame(
                schema={
                    "symbol": pl.String,
                    "raw_probability": pl.Float64,
                    "calibration_status": pl.String,
                    "approved_for_kernel": pl.Boolean,
                    "model_id": pl.String,
                    "prompt_sha256": pl.String,
                }
            ),
            None,
        )
    snapshot, path = max(matches, key=lambda item: item[0].asof_utc)
    return pl.read_parquet(path), snapshot


def _check(
    name: str,
    severity: QualitySeverity,
    passed: bool,
    observed: object,
    expected: str,
) -> DataQualityCheck:
    return DataQualityCheck(
        name=name,
        severity=severity,
        passed=passed,
        observed=str(observed),
        expected=expected,
        provenance="research.postmortem.build_trading_episode",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trade-date", type=_parse_date, required=True)
    parser.add_argument("--data-root", type=Path, default=ROOT / "data")
    args = parser.parse_args()

    schedule = build_xnys_schedule(args.trade_date, args.trade_date)
    if schedule.height != 1:
        raise ValueError("target XNYS session is unavailable")
    session = schedule.row(0, named=True)
    market_open = session["market_open_utc"]
    market_close = session["market_close_utc"]
    if not isinstance(market_open, datetime) or not isinstance(market_close, datetime):
        raise ValueError("calendar timestamps are invalid")
    is_half_day = bool(session.get("is_half_day", False))

    selection, selection_snapshot, _ = _latest_dated(
        args.data_root,
        "kernel.universe.selection_gates-*/data.parquet",
        args.trade_date,
    )
    signals, signal_snapshot, _ = _load_full_session_signals(
        args.data_root, args.trade_date, market_close
    )
    raw_ids = [
        value
        for value in signal_snapshot.parent_snapshot_ids
        if value.startswith("alpaca.sip.orb5_1m-")
    ]
    if len(raw_ids) != 1:
        raise ValueError("ORB-5 snapshot must identify exactly one raw bar parent")
    raw_path = args.data_root / "accepted" / raw_ids[0] / "data.parquet"
    if not raw_path.exists():
        raise FileNotFoundError("ORB-5 raw bar parent is unavailable")
    bars = pl.read_parquet(raw_path)
    scores, score_snapshot = _load_score(args.data_root, args.trade_date)

    episode = build_trading_episode(
        selection=selection,
        signals=signals,
        bars=bars,
        catalyst_scores=scores,
        trade_date=args.trade_date,
        session_open_utc=market_open,
        session_close_utc=market_close,
        is_half_day=is_half_day,
        cfg=load_config(ROOT / "config.yaml"),
    )
    survivor_symbols = set(
        selection.filter(pl.col("pass_gate")).get_column("symbol").to_list()
    )
    actual_symbols = set(episode.get_column("symbol").to_list())
    duplicates = episode.height - episode.get_column("symbol").n_unique()
    unavailable_triggered = episode.filter(
        pl.col("signal_triggered") & (pl.col("outcome_label") == "unavailable")
    ).height
    approved_scores = episode.filter(pl.col("model_score_approved")).height
    checks = (
        _check(
            "exact_gate_survivors",
            QualitySeverity.CRITICAL,
            actual_symbols == survivor_symbols,
            len(actual_symbols),
            f"exactly {len(survivor_symbols)} gate survivors",
        ),
        _check(
            "unique_symbol",
            QualitySeverity.CRITICAL,
            duplicates == 0,
            duplicates,
            "0 duplicate symbols",
        ),
        _check(
            "complete_triggered_outcomes",
            QualitySeverity.WARNING,
            unavailable_triggered == 0,
            unavailable_triggered,
            "0 triggered signals with censored outcome labels; missing bars stay missing",
        ),
        _check(
            "shadow_scores_remain_unapproved",
            QualitySeverity.CRITICAL,
            approved_scores == 0,
            approved_scores,
            "0 raw catalyst scores approved for the kernel",
        ),
        _check(
            "net_costs_not_invented",
            QualitySeverity.INFO,
            episode.get_column("net_return").null_count() == episode.height,
            episode.get_column("net_return").null_count(),
            "all net returns remain unavailable until quote spread is captured",
        ),
    )
    parent_ids = [selection_snapshot.dataset_id, signal_snapshot.dataset_id, raw_ids[0]]
    if score_snapshot is not None:
        parent_ids.append(score_snapshot.dataset_id)
    snapshot, path = persist_snapshot(
        episode,
        root=args.data_root,
        source="research.trading_episodes",
        schema_version=EPISODE_SCHEMA_VERSION,
        checks=checks,
        parent_snapshot_ids=tuple(parent_ids),
    )
    snapshot.assert_usable()
    counts = episode.group_by("outcome_label").len().sort("outcome_label").to_dicts()
    print(
        json.dumps(
            {
                "trade_date": args.trade_date.isoformat(),
                "status": "complete_research_only",
                "rows": episode.height,
                "outcomes": counts,
                "net_return_status": "unavailable_missing_quote_spread",
                "dataset_id": snapshot.dataset_id,
                "path": str(path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
