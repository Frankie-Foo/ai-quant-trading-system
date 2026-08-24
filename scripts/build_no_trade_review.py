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
from execution.autonomous_paper_session import PaperSessionLedger
from operations.autonomous_paper_config import load_autonomous_paper_config
from research.no_trade_review import (
    NO_TRADE_REVIEW_SCHEMA_VERSION,
    build_no_trade_review,
)

ROOT = Path(__file__).resolve().parents[1]


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


def _latest_episode(data_root: Path, trade_date: date) -> tuple[pl.DataFrame, DatasetSnapshot]:
    matches: list[tuple[DatasetSnapshot, Path]] = []
    for path in (data_root / "accepted").glob("research.trading_episodes-*/data.parquet"):
        frame = pl.read_parquet(path, columns=["session_date"])
        if frame.get_column("session_date").unique().to_list() != [trade_date]:
            continue
        manifest = DatasetSnapshot.model_validate_json(
            (path.parent / "manifest.json").read_text(encoding="utf-8")
        )
        manifest.assert_usable()
        matches.append((manifest, path))
    if not matches:
        raise FileNotFoundError("accepted trading episode is unavailable")
    manifest, path = max(matches, key=lambda item: item[0].asof_utc)
    return pl.read_parquet(path), manifest


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
        provenance="research.no_trade_review",
    )


def _evaluation_frame(ledger: PaperSessionLedger, trade_date: date) -> pl.DataFrame:
    rows = [
        {
            "plan_id": item.plan_id,
            "evaluation_count": item.evaluation_count,
            "observe_count": item.observe_count,
            "data_blocked_count": item.data_blocked_count,
            "runtime_failure_count": item.runtime_failure_count,
            "submitted_order_count": item.submitted_order_count,
            "first_observed_at_utc": item.first_observed_at_utc,
            "last_observed_at_utc": item.last_observed_at_utc,
        }
        for item in ledger.plan_evaluation_summaries(trade_date)
    ]
    if rows:
        return pl.DataFrame(rows)
    return pl.DataFrame(
        schema={
            "plan_id": pl.String,
            "evaluation_count": pl.Int64,
            "observe_count": pl.Int64,
            "data_blocked_count": pl.Int64,
            "runtime_failure_count": pl.Int64,
            "submitted_order_count": pl.Int64,
            "first_observed_at_utc": pl.Datetime(time_zone="UTC"),
            "last_observed_at_utc": pl.Datetime(time_zone="UTC"),
        }
    )


def _evaluation_frame_from_log(path: Path, *, plan_ids: frozenset[str]) -> pl.DataFrame:
    """Backfill pre-summary runtimes from their existing structured JSON log."""

    if not path.exists():
        return pl.DataFrame()
    summaries: dict[str, dict[str, object]] = {}

    def increment(row: dict[str, object], key: str, amount: int = 1) -> None:
        current = row[key]
        if isinstance(current, bool) or not isinstance(current, int):
            raise ValueError("invalid runtime log counter")
        row[key] = current + amount

    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("plan_id") not in plan_ids:
            continue
        plan_id = str(event["plan_id"])
        event_name = str(event.get("event") or "")
        if event_name not in {
            "autonomous_paper_plan_evaluated",
            "autonomous_paper_runtime_failed_closed",
        }:
            continue
        observed_at = datetime.fromisoformat(str(event["ts_utc"]))
        row = summaries.setdefault(
            plan_id,
            {
                "plan_id": plan_id,
                "evaluation_count": 0,
                "observe_count": 0,
                "data_blocked_count": 0,
                "runtime_failure_count": 0,
                "submitted_order_count": 0,
                "first_observed_at_utc": observed_at,
                "last_observed_at_utc": observed_at,
            },
        )
        row["last_observed_at_utc"] = observed_at
        if event_name == "autonomous_paper_runtime_failed_closed":
            increment(row, "runtime_failure_count")
            continue
        action = str(event.get("action") or "")
        increment(row, "evaluation_count")
        increment(row, "observe_count", int(action == "observe"))
        increment(row, "data_blocked_count", int(action == "data_blocked"))
        order_ids = event.get("submitted_order_ids")
        if isinstance(order_ids, list):
            increment(row, "submitted_order_count", len(order_ids))
    return pl.DataFrame(list(summaries.values())) if summaries else pl.DataFrame()


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trade-date", type=_parse_date, required=True)
    parser.add_argument("--data-root", type=Path, default=ROOT / "data")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--state-db", type=Path)
    parser.add_argument(
        "--executor-log", type=Path, default=ROOT / "runs" / "autonomous-executor.out.log"
    )
    args = parser.parse_args(argv)
    run_root = ROOT / "runs" / "autonomous" / args.trade_date.isoformat()
    config_path = args.config or run_root / "autonomous_paper.json"
    state_db = args.state_db or run_root / "paper.sqlite3"

    config = load_autonomous_paper_config(config_path)
    if {bundle.plan.trade_date for bundle in config.plans} != {args.trade_date}:
        raise ValueError("Paper config trade date mismatch")
    plans = pl.DataFrame(
        {
            "plan_id": [bundle.plan.plan_id for bundle in config.plans],
            "symbol": [bundle.plan.symbol for bundle in config.plans],
            "trade_date": [bundle.plan.trade_date for bundle in config.plans],
        }
    )
    episodes, episode_snapshot = _latest_episode(args.data_root, args.trade_date)
    session = build_xnys_schedule(args.trade_date, args.trade_date).row(0, named=True)
    evaluations = _evaluation_frame(PaperSessionLedger(state_db), args.trade_date)
    if evaluations.is_empty():
        evaluations = _evaluation_frame_from_log(
            args.executor_log,
            plan_ids=frozenset(bundle.plan.plan_id for bundle in config.plans),
        )
    review = build_no_trade_review(
        plans=plans,
        episodes=episodes,
        evaluations=evaluations,
        session_open_utc=session["market_open_utc"],
        session_close_utc=session["market_close_utc"],
    )
    evidence_missing = review.filter(
        pl.col("execution_root_cause") == "execution_evidence_missing"
    ).height
    snapshot, path = persist_snapshot(
        review,
        root=args.data_root,
        source="research.paper_no_trade_review",
        schema_version=NO_TRADE_REVIEW_SCHEMA_VERSION,
        checks=(
            _check(
                "exact_paper_plans",
                QualitySeverity.CRITICAL,
                review.height == plans.height,
                review.height,
                f"exactly {plans.height} Paper plans",
            ),
            _check(
                "execution_evidence_present",
                QualitySeverity.WARNING,
                evidence_missing == 0,
                evidence_missing,
                "zero plans without durable execution evidence",
            ),
            _check(
                "production_changes_forbidden",
                QualitySeverity.CRITICAL,
                not review.get_column("production_change_allowed").any(),
                review.get_column("production_change_allowed").sum(),
                "zero production changes",
            ),
        ),
        parent_snapshot_ids=(episode_snapshot.dataset_id,),
    )
    snapshot.assert_usable()
    result: dict[str, Any] = {
        "trade_date": args.trade_date.isoformat(),
        "dataset_id": snapshot.dataset_id,
        "path": str(path),
        "plans": review.height,
        "orders_submitted": int(review.get_column("submitted_order_count").sum()),
        "root_causes": review.group_by("execution_root_cause")
        .len()
        .sort("execution_root_cause")
        .to_dicts(),
        "production_changes": 0,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
