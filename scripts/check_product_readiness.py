from __future__ import annotations

import argparse
import json
from pathlib import Path

from operations.readiness import MaturityEvidence, ProductStage, assess_product_readiness


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument(
        "--target",
        choices=("paper", "live"),
        default="live",
    )
    args = parser.parse_args()
    evidence = MaturityEvidence.model_validate_json(
        args.evidence.read_text(encoding="utf-8")
    )
    report = assess_product_readiness(evidence)
    print(json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2))
    passed = (
        report.paper_eligible
        if args.target == "paper"
        else report.stage is ProductStage.LIVE_ELIGIBLE
    )
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
