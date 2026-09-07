from __future__ import annotations

import argparse
import json
import os
from datetime import date
from pathlib import Path

from kernel.config import load_config
from kernel.strategy_policy import load_strategy_policy
from operations.local_env import load_project_env, project_data_root
from operations.loop_integration.client import (
    AuditOnlyBackfillRequired,
    LoopClient,
    LoopPreconditionError,
    LoopRunFailedError,
)
from operations.loop_integration.contracts import LoopBinding
from operations.loop_integration.execution_summary import load_execution_index
from operations.loop_integration.outbox import LoopOutbox
from operations.loop_integration.review_builder import build_review_envelope, load_accepted_snapshot

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "research.intraday_selection_postmortem"


def _latest(data_root: Path, trade_date: date) -> Path:
    matches: list[Path] = []
    for path in (data_root / "accepted").glob(f"{SOURCE}-*/data.parquet"):
        snapshot, frame = load_accepted_snapshot(path)
        del snapshot
        if frame.get_column("session_date").unique().to_list() == [trade_date]:
            matches.append(path)
    if not matches:
        raise FileNotFoundError(f"no accepted selection postmortem for {trade_date}")
    return max(matches, key=lambda value: value.parent.stat().st_mtime_ns)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True, type=date.fromisoformat)
    parser.add_argument("--binding", required=True, type=Path)
    parser.add_argument("--data-root", type=Path, default=project_data_root(ROOT))
    parser.add_argument("--active-policy", type=Path, default=ROOT / "runs/strategy/active.json")
    parser.add_argument("--outbox", type=Path, default=ROOT / "runs/loop-integration.sqlite3")
    parser.add_argument("--artifact-id", action="append", default=[])
    parser.add_argument("--stage-only", action="store_true")
    parser.add_argument("--effective-plan", type=Path)
    parser.add_argument("--effective-plan-sha256")
    parser.add_argument("--fill-evidence", type=Path)
    parser.add_argument("--fill-evidence-sha256")
    parser.add_argument("--review-context", type=Path)
    parser.add_argument("--review-context-sha256")
    parser.add_argument("--execution-index", type=Path)
    parser.add_argument("--execution-index-sha256")
    return parser


def main() -> None:
    args = _parser().parse_args()
    if not args.stage_only:
        load_project_env(ROOT)
    entries = load_execution_index(args.execution_index, args.execution_index_sha256)
    entry = None
    if args.execution_index is not None:
        if any((args.effective_plan, args.effective_plan_sha256, args.fill_evidence,
                args.fill_evidence_sha256, args.review_context, args.review_context_sha256)):
            raise ValueError("use execution index or direct evidence flags, not both")
        matches = [row for row in entries if row.trade_date == args.trade_date]
        if len(matches) != 1:
            raise ValueError("review requires exactly one execution index entry for date")
        entry = matches[0]
        args.effective_plan, args.effective_plan_sha256 = entry.plan_path, entry.plan_sha256
        args.fill_evidence, args.fill_evidence_sha256 = entry.fills_path, entry.fills_sha256
        args.review_context = entry.review_context_path
        args.review_context_sha256 = entry.review_context_sha256
    binding = LoopBinding.model_validate_json(args.binding.read_text(encoding="utf-8"))
    path = _latest(args.data_root, args.trade_date)
    snapshot, _ = load_accepted_snapshot(path)
    active = load_strategy_policy(args.active_policy, required_status="active")
    envelope = build_review_envelope(
        project_root=ROOT,
        trade_date=args.trade_date,
        opportunity_path=path,
        opportunity_snapshot=snapshot,
        artifact_ids=tuple(args.artifact_id),
        cfg=load_config(ROOT / "config.yaml"),
        active_policy=active,
        market_scope=binding.market_scope,
        effective_plan_path=args.effective_plan,
        effective_plan_sha256=args.effective_plan_sha256,
        fill_evidence_path=args.fill_evidence,
        fill_evidence_sha256=args.fill_evidence_sha256,
        review_context_path=args.review_context,
        review_context_sha256=args.review_context_sha256,
    )
    if entry is not None and envelope.risk_policy.get("status") == "available":
        if envelope.risk_policy["evidence"]["strategy_sha256"] != entry.strategy_sha256:
            raise ValueError("execution index strategy mismatch")
    outbox = LoopOutbox(args.outbox)
    staged = outbox.stage(
        event_id=envelope.event_id,
        event_type="daily_review",
        payload=envelope.model_dump(mode="json"),
        payload_sha256=envelope.payload_sha256,
    )
    if args.stage_only:
        print(json.dumps({
            "status": "staged", "event_id": envelope.event_id,
            "risk_status": envelope.risk_policy.get("status"),
            "submitted": False,
        }))
        return
    if staged.status == "delivered":
        print(json.dumps({"status": "delivered", "event_id": envelope.event_id,
                          "task_id": staged.remote_task_id, "run_id": staged.remote_run_id}))
        return
    if envelope.risk_policy.get("status") != "available":
        outbox.mark_blocked_precondition(
            envelope.event_id, error_code="EFFECTIVE_MODERN_PLAN_UNAVAILABLE"
        )
        print(json.dumps({"status": "blocked_precondition", "event_id": envelope.event_id}))
        return
    client = LoopClient(
        base_url=os.environ.get("LOOP_BASE_URL", ""),
        api_key=os.environ.get("LOOP_RUNTIME_API_KEY", ""),
    )
    try:
        task_id, run_id = client.submit_review(envelope, binding)
    except AuditOnlyBackfillRequired as exc:
        outbox.mark_audit_only_backfill(envelope.event_id, error_code=exc.code)
        print(json.dumps({"status": "audit_only_backfill", "event_id": envelope.event_id}))
        return
    except LoopPreconditionError as exc:
        outbox.mark_blocked_precondition(envelope.event_id, error_code=exc.code)
        print(json.dumps({"status": "blocked_precondition", "event_id": envelope.event_id}))
        return
    except LoopRunFailedError as exc:
        outbox.mark_failed(
            envelope.event_id,
            error_code=exc.error_code,
            remote_task_id=exc.task_id,
            remote_run_id=exc.run_id,
            failed_node=exc.failed_node,
        )
        raise
    except Exception as exc:
        outbox.mark_failed(envelope.event_id, error_code=type(exc).__name__)
        raise
    outbox.mark_delivered(envelope.event_id, remote_task_id=task_id, remote_run_id=run_id)
    print(json.dumps({"status": "delivered", "task_id": task_id, "run_id": run_id}))


if __name__ == "__main__":
    main()
