from __future__ import annotations

import argparse
import json
import os
from datetime import date
from pathlib import Path

from kernel.config import load_config
from kernel.strategy_policy import load_strategy_policy
from operations.local_env import load_project_env, project_data_root
from operations.loop_integration.client import LoopClient
from operations.loop_integration.contracts import LoopBinding
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
    return parser


def main() -> None:
    load_project_env(ROOT)
    args = _parser().parse_args()
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
    )
    outbox = LoopOutbox(args.outbox)
    outbox.stage(
        event_id=envelope.event_id,
        event_type="daily_review",
        payload=envelope.model_dump(mode="json"),
        payload_sha256=envelope.payload_sha256,
    )
    if args.stage_only:
        print(json.dumps({"status": "staged", "event_id": envelope.event_id}))
        return
    client = LoopClient(
        base_url=os.environ.get("LOOP_BASE_URL", ""),
        api_key=os.environ.get("LOOP_RUNTIME_API_KEY", ""),
    )
    try:
        task_id, run_id = client.submit_review(envelope, binding)
    except Exception as exc:
        outbox.mark_failed(envelope.event_id, error_code=type(exc).__name__)
        raise
    outbox.mark_delivered(envelope.event_id, remote_task_id=task_id, remote_run_id=run_id)
    print(json.dumps({"status": "delivered", "task_id": task_id, "run_id": run_id}))


if __name__ == "__main__":
    main()
