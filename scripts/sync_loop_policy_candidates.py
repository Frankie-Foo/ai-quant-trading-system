from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from operations.local_env import load_project_env
from operations.loop_integration.client import LoopClient
from operations.loop_integration.policy_consumer import install_shadow_candidate

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    load_project_env(ROOT)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market-scope", default="US-equity")
    parser.add_argument("--artifact-id")
    parser.add_argument("--active-policy", type=Path, default=ROOT / "runs/strategy/active.json")
    parser.add_argument(
        "--challenger-policy",
        type=Path,
        default=ROOT / "runs/strategy/challenger.json",
    )
    args = parser.parse_args()
    client = LoopClient(
        base_url=os.environ.get("LOOP_BASE_URL", ""),
        api_key=os.environ.get("LOOP_RUNTIME_API_KEY", ""),
    )
    candidates = client.list_policy_candidates(market_scope=args.market_scope)
    selected = [item for item in candidates if args.artifact_id in {None, item.id}]
    if len(selected) != 1:
        raise RuntimeError("select exactly one Loop policy artifact with --artifact-id")
    policy = install_shadow_candidate(
        selected[0], active_path=args.active_policy, challenger_path=args.challenger_policy
    )
    print(
        json.dumps(
            {
                "status": "shadow_installed",
                "version": policy.version,
                "policy_hash": policy.policy_hash,
            }
        )
    )


if __name__ == "__main__":
    main()
