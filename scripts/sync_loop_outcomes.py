from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from operations.local_env import load_project_env
from operations.loop_integration.client import LoopClient
from operations.loop_integration.contracts import LoopOutcomeEnvelope
from operations.loop_integration.outbox import LoopOutbox
from operations.loop_integration.review_builder import envelope_sha256

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    load_project_env(ROOT)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", required=True, type=Path)
    parser.add_argument("--outbox", type=Path, default=ROOT / "runs/loop-integration.sqlite3")
    parser.add_argument("--stage-only", action="store_true")
    args = parser.parse_args()
    raw = json.loads(args.file.read_text(encoding="utf-8"))
    rows = raw if isinstance(raw, list) else [raw]
    outcomes = tuple(LoopOutcomeEnvelope.model_validate(row) for row in rows)
    outbox = LoopOutbox(args.outbox)
    client = None if args.stage_only else LoopClient(
        base_url=os.environ.get("LOOP_BASE_URL", ""),
        api_key=os.environ.get("LOOP_RUNTIME_API_KEY", ""),
    )
    delivered = 0
    for outcome in outcomes:
        payload = outcome.model_dump(mode="json")
        outbox.stage(
            event_id=outcome.id,
            event_type="outcome",
            payload=payload,
            payload_sha256=envelope_sha256(payload),
        )
        if client is None:
            continue
        try:
            client.submit_outcome(outcome)
        except Exception as exc:
            outbox.mark_failed(outcome.id, error_code=type(exc).__name__)
            raise
        outbox.mark_delivered(outcome.id)
        delivered += 1
    print(json.dumps({"staged": len(outcomes), "delivered": delivered}))


if __name__ == "__main__":
    main()
