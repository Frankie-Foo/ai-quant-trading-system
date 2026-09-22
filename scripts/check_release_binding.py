"""Read-only guard against resetting runtime state or strategy during deployment."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from kernel.strategy_policy import load_strategy_policy


def validate_release_binding(
    release_root: Path, state_root: Path, active_policy: Path,
) -> dict[str, str]:
    release = release_root.resolve(strict=True)
    state = state_root.resolve(strict=True)
    if state.is_relative_to(release):
        raise ValueError("persistent state must be outside the immutable release")
    runs = release / "runs"
    if not state.is_dir() or not runs.is_dir() or not runs.samefile(state):
        raise ValueError("release runs must use the verified persistent state binding")
    policy = load_strategy_policy(active_policy, required_status="active")
    return {
        "state_root": str(state),
        "policy_version": policy.version,
        "policy_file_sha256": hashlib.sha256(active_policy.read_bytes()).hexdigest(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-root", type=Path, required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--active-policy", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(validate_release_binding(
        args.release_root, args.state_root, args.active_policy,
    ), sort_keys=True))


if __name__ == "__main__":
    main()
