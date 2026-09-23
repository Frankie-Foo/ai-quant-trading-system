from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import UTC, date, datetime
from pathlib import Path

from kernel.config import load_config
from kernel.strategy_policy import load_strategy_policy
from operations.local_env import load_project_env, project_data_root
from operations.loop_integration.client import (
    AuditOnlyBackfillRequired,
    LoopClient,
    LoopPreconditionError,
    LoopRemoteRejectedError,
    LoopRunFailedError,
    LoopRunIncompleteError,
)
from operations.loop_integration.contracts import LoopBinding, QuantReviewEnvelope
from operations.loop_integration.execution_summary import load_execution_index
from operations.loop_integration.outbox import LoopOutbox, OutboxItem
from operations.loop_integration.review_builder import build_review_envelope, load_accepted_snapshot

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "research.intraday_selection_postmortem"
_WAVE_FILES = (
    ("08:30_top20", "first_wave_pool.json"),
    ("09:00_top20", "second_wave_pool.json"),
    ("09:30_top10", "final_wave_pool.json"),
)
_WAVE_CANDIDATE_FIELDS = (
    "wave_rank",
    "forward_rank",
    "repeat_count",
    "base_score",
    "weighted_score",
    "rvol",
    "premarket_return",
    "catalyst_categories",
    "reasons",
)


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


def _intraday_waves(state_root: Path, trade_date: date) -> dict[str, object]:
    """Load only frozen intraday wave facts; missing waves remain explicit."""

    day_root = state_root / trade_date.isoformat()
    waves: list[dict[str, object]] = []
    missing: list[str] = []
    for stage, filename in _WAVE_FILES:
        path = day_root / filename
        if not path.is_file():
            missing.append(stage)
            continue
        raw = path.read_bytes()
        payload = json.loads(raw)
        if not isinstance(payload, dict) or payload.get("trade_date") != trade_date.isoformat():
            raise ValueError(f"intraday wave is invalid: {filename}")
        rows = payload.get("candidates")
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise ValueError(f"intraday wave candidates are invalid: {filename}")
        candidates: list[dict[str, object]] = []
        for row in rows:
            symbol = str(row.get("symbol") or "").strip().upper()
            if not symbol:
                raise ValueError(f"intraday wave symbol is invalid: {filename}")
            candidates.append({
                "symbol": symbol,
                **{field: row[field] for field in _WAVE_CANDIDATE_FIELDS if field in row},
            })
        waves.append({
            "stage": stage,
            "artifact": filename,
            "content_sha256": hashlib.sha256(raw).hexdigest(),
            "generated_at_utc": payload.get("generated_at_utc"),
            "source_snapshot_id": payload.get("source_snapshot_id"),
            "candidate_count": len(candidates),
            "candidates": candidates,
        })
    status = "unavailable"
    if waves:
        status = "available" if len(waves) == len(_WAVE_FILES) else "partial"
    return {
        "status": status,
        "missing_stages": missing,
        "waves": waves,
        "semantics": "frozen_intraday_wave_snapshots_not_post_close_winners",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True, type=date.fromisoformat)
    parser.add_argument("--binding", required=True, type=Path)
    parser.add_argument("--data-root", type=Path, default=project_data_root(ROOT))
    parser.add_argument("--active-policy", type=Path, default=ROOT / "runs/strategy/active.json")
    parser.add_argument("--outbox", type=Path, default=ROOT / "runs/loop-integration.sqlite3")
    parser.add_argument("--state-root", type=Path, default=ROOT / "runs" / "autonomous")
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


def resume_submitted_review(
    outbox: LoopOutbox, item: OutboxItem, client: LoopClient, *, now: datetime,
) -> dict[str, object]:
    """Resume frozen remote work without requiring today's policy or source files."""
    envelope = QuantReviewEnvelope.model_validate(item.payload)
    if envelope.payload_sha256 != item.payload_sha256:
        raise ValueError("persisted Loop review content hash mismatch")
    receipt: dict[str, object] = {"event_id": item.event_id, "task_id": item.remote_task_id,
                                 "run_id": item.remote_run_id}
    if item.status in {"delivered", "remote_completed"}:
        return {**receipt, "status": "delivered"}
    if item.status == "remote_rejected":
        return {**receipt, "status": "remote_failed"}
    if item.status in {"creating_task", "starting_run"}:
        return {**receipt, "status": "reconciliation_required"}
    if item.retry_after_utc and datetime.fromisoformat(item.retry_after_utc) > now:
        return {**receipt, "status": "retry_scheduled", "retry_after_utc": item.retry_after_utc}

    def checkpoint(status: str, task: str | None, run: str | None) -> None:
        outbox.checkpoint(item.event_id, status, task, run)

    try:
        if item.remote_task_id and item.remote_run_id:
            task, run = client.get_review_run(
                task_id=item.remote_task_id, run_id=item.remote_run_id
            )
        elif item.status == "task_created" and item.remote_task_id:
            task, run = client.start_review_run(task_id=item.remote_task_id, checkpoint=checkpoint)
        else:
            return {**receipt, "status": "reconciliation_required"}
    except LoopRunIncompleteError as exc:
        outbox.mark_remote_processing(item.event_id, remote_task_id=exc.task_id,
                                      remote_run_id=exc.run_id)
        outbox.defer_retry(item.event_id, "REMOTE_PROCESSING", now=now)
        return {**receipt, "status": "remote_processing", "run_id": exc.run_id}
    except LoopRunFailedError as exc:
        outbox.mark_failed(item.event_id, error_code=exc.error_code,
                           remote_task_id=exc.task_id, remote_run_id=exc.run_id,
                           failed_node=exc.failed_node)
        outbox.mark_remote_rejected(item.event_id, error_code=exc.error_code)
        return {**receipt, "status": "remote_failed"}
    except LoopRemoteRejectedError as exc:
        outbox.mark_remote_rejected(item.event_id, error_code=exc.error_code)
        return {**receipt, "status": "remote_rejected", "error_code": exc.error_code}
    except Exception as exc:
        outbox.defer_retry(item.event_id, type(exc).__name__, now=now)
        return {**receipt, "status": "retryable_failure", "error_type": type(exc).__name__}
    outbox.mark_delivered(item.event_id, remote_task_id=task, remote_run_id=run)
    return {**receipt, "status": "delivered", "task_id": task, "run_id": run}


def main() -> None:
    args = _parser().parse_args()
    if not args.stage_only:
        load_project_env(ROOT)
        outbox = LoopOutbox(args.outbox)
        prior = outbox.submitted_review(args.trade_date)
        if prior is not None:
            client = LoopClient(base_url=os.environ.get("LOOP_BASE_URL", ""),
                                api_key=os.environ.get("LOOP_RUNTIME_API_KEY", ""))
            print(json.dumps(resume_submitted_review(outbox, prior, client, now=datetime.now(UTC))))
            return
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
        intraday_waves=_intraday_waves(args.state_root, args.trade_date),
    )
    if entry is not None and envelope.risk_policy.get("status") == "available":
        if envelope.risk_policy["evidence"]["strategy_sha256"] != entry.strategy_sha256:
            raise ValueError("execution index strategy mismatch")
    outbox = LoopOutbox(args.outbox)
    previous = outbox.get(envelope.event_id)
    if not args.stage_only and previous is not None and (
        previous.remote_task_id or previous.status in {"creating_task", "starting_run"}
    ):
        # Deployment/snapshot changes cannot rewrite an already submitted review.
        staged = previous
        envelope = QuantReviewEnvelope.model_validate(previous.payload)
        if envelope.payload_sha256 != previous.payload_sha256:
            raise ValueError("persisted Loop review content hash mismatch")
    else:
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
    if staged.status in {"delivered", "remote_completed"}:
        print(json.dumps({"status": "delivered", "event_id": envelope.event_id,
                          "task_id": staged.remote_task_id, "run_id": staged.remote_run_id}))
        return
    no_order_review = envelope.execution_summary.get("orders_authorized") is False
    if envelope.risk_policy.get("status") != "available" and not no_order_review:
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
        def checkpoint(status: str, task: str | None, run: str | None) -> None:
            outbox.checkpoint(envelope.event_id, status, task, run)
        if staged.remote_task_id and staged.remote_run_id:
            task_id, run_id = client.get_review_run(
                task_id=staged.remote_task_id, run_id=staged.remote_run_id
            )
        elif staged.status == "task_created" and staged.remote_task_id:
            task_id, run_id = client.start_review_run(
                task_id=staged.remote_task_id, checkpoint=checkpoint
            )
        elif staged.status in {"creating_task", "starting_run"} or staged.remote_task_id:
            raise RuntimeError("Loop submission outcome unknown; reconcile before resubmission")
        else:
            task_id, run_id = client.submit_review(envelope, binding, checkpoint=checkpoint)
    except LoopRunIncompleteError as exc:
        outbox.mark_remote_processing(
            envelope.event_id, remote_task_id=exc.task_id, remote_run_id=exc.run_id
        )
        outbox.defer_retry(envelope.event_id, "REMOTE_PROCESSING", now=datetime.now(UTC))
        print(json.dumps({"status": "remote_processing", "task_id": exc.task_id,
                          "run_id": exc.run_id}))
        return
    except AuditOnlyBackfillRequired as exc:
        outbox.mark_audit_only_backfill(envelope.event_id, error_code=exc.code)
        print(json.dumps({"status": "audit_only_backfill", "event_id": envelope.event_id}))
        return
    except LoopPreconditionError as exc:
        outbox.mark_blocked_precondition(envelope.event_id, error_code=exc.code)
        print(json.dumps({"status": "blocked_precondition", "event_id": envelope.event_id}))
        return
    except LoopRemoteRejectedError as exc:
        outbox.mark_remote_rejected(envelope.event_id, error_code=exc.error_code)
        print(json.dumps({"status": "remote_rejected", "event_id": envelope.event_id,
                          "error_code": exc.error_code}))
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
        outbox.record_error(envelope.event_id, type(exc).__name__)
        raise
    outbox.mark_delivered(envelope.event_id, remote_task_id=task_id, remote_run_id=run_id)
    print(json.dumps({"status": "delivered", "task_id": task_id, "run_id": run_id}))


if __name__ == "__main__":
    main()
