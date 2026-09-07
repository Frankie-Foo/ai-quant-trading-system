"""Generate and submit all matured Loop 1d/5d/20d Outcome assignments."""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, date, datetime
from pathlib import Path

from operations.local_env import load_project_env, project_data_root
from operations.loop_integration.client import LoopClient
from operations.loop_integration.contracts import OutcomeReporterConfig
from operations.loop_integration.outbox import LoopOutbox
from operations.loop_integration.outcome_reporter import sync_due_outcomes

ROOT = Path(__file__).resolve().parents[1]


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


def main() -> None:
    load_project_env(ROOT)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of-trade-date", required=True, type=_date)
    parser.add_argument("--data-root", type=Path, default=project_data_root(ROOT))
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument(
        "--outbox",
        type=Path,
        default=ROOT / "runs/loop-integration.sqlite3",
    )
    parser.add_argument("--stage-only", action="store_true")
    args = parser.parse_args()
    config = OutcomeReporterConfig.model_validate_json(
        args.config.read_text(encoding="utf-8")
    )
    client = LoopClient(
        base_url=os.environ.get("LOOP_BASE_URL", ""),
        api_key=os.environ.get("LOOP_RUNTIME_API_KEY", ""),
    )
    summary = sync_due_outcomes(
        client=client,
        outbox=LoopOutbox(args.outbox),
        data_root=args.data_root,
        as_of_date=args.as_of_trade_date,
        observed_before=datetime.now(UTC),
        config=config,
        stage_only=args.stage_only,
    )
    print(json.dumps(summary.to_dict(), ensure_ascii=False))


if __name__ == "__main__":
    main()
