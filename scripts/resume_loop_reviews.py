"""Poll due frozen Loop reviews; never build a new daily task."""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from operations.local_env import load_project_env
from operations.loop_integration.client import LoopClient
from operations.loop_integration.outbox import LoopOutbox
from scripts.sync_loop_daily_review import resume_submitted_review


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    load_project_env(root)
    if os.environ.get("AI_QUANT_LOOP_SYNC_ENABLED", "").lower() not in {"true", "1", "yes"}:
        return 0
    outbox = LoopOutbox(root / "runs/loop-integration.sqlite3")
    now = datetime.now(UTC)
    due = outbox.recoverable_reviews(now=now)
    if not due:
        return 0
    client = LoopClient(base_url=os.environ.get("LOOP_BASE_URL", ""),
                        api_key=os.environ.get("LOOP_RUNTIME_API_KEY", ""))
    receipts = [resume_submitted_review(outbox, item, client, now=now) for item in due]
    print(json.dumps({"loop_recovery": receipts}))
    # Slow-loop availability never blocks or fails the fast trading scheduler.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
