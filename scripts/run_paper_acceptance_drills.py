"""Run no-network Paper safety drills and write a hashed receipt."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from operations.paper_acceptance_drills import run_paper_acceptance_drills

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=(
            ROOT
            / "runs"
            / "acceptance"
            / f"paper-acceptance-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
        ),
    )
    args = parser.parse_args()
    receipt = run_paper_acceptance_drills(root=ROOT, output_path=args.output)
    print(json.dumps(asdict(receipt), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
