"""Register immutable adaptive-plan baselines in the restart-safe local store."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from operations.adaptive_plan_config import load_adaptive_plan_config
from operations.adaptive_plan_store import AdaptivePlanStore

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--state-db",
        type=Path,
        default=ROOT / "runs" / "adaptive-plans.sqlite3",
    )
    args = parser.parse_args()
    config = load_adaptive_plan_config(args.config)
    store = AdaptivePlanStore(args.state_db)
    for plan in config.plans:
        store.register(plan)
    print(
        json.dumps(
            {
                "status": "registered",
                "plan_count": len(config.plans),
                "symbols": [plan.symbol for plan in config.plans],
                "orders_authorized": False,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
