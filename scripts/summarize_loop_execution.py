"""Print factual Loop execution evidence from explicitly pinned local files only.

No environment loading, discovery, network, outbox, broker calls or file writes.
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path

from operations.loop_integration.execution_summary import build_factual_execution_summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--plan-sha256", required=True)
    parser.add_argument("--fills", type=Path)
    parser.add_argument("--fills-sha256")
    parser.add_argument("--review-context", type=Path)
    parser.add_argument("--review-context-sha256")
    parser.add_argument("--trade-date", required=True, type=date.fromisoformat)
    parser.add_argument("--as-of", required=True, type=datetime.fromisoformat)
    args = parser.parse_args()
    summary = build_factual_execution_summary(
        plan_path=args.plan, plan_sha256=args.plan_sha256,
        fills_path=args.fills, fills_sha256=args.fills_sha256,
        trade_date=args.trade_date, as_of=args.as_of,
        review_context_path=args.review_context, review_context_sha256=args.review_context_sha256,
    )
    print(json.dumps(summary, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
