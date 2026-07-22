from __future__ import annotations

import argparse
import json
from pathlib import Path

from operations.evidence import (
    load_existing_evidence,
    refresh_maturity_evidence,
    write_evidence_atomic,
)
from operations.readiness import assess_product_readiness

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Refresh objective maturity metrics; preserve manual attestations."
    )
    parser.add_argument("--data-root", type=Path, default=ROOT / "data")
    parser.add_argument("--order-db", type=Path, default=ROOT / "runs/paper-orders.sqlite3")
    parser.add_argument(
        "--evidence",
        type=Path,
        default=ROOT / "runs/maturity-evidence.json",
    )
    args = parser.parse_args()
    evidence = refresh_maturity_evidence(
        data_root=args.data_root,
        order_db=args.order_db,
        existing=load_existing_evidence(args.evidence),
    )
    write_evidence_atomic(evidence, args.evidence)
    report = assess_product_readiness(evidence)
    print(
        json.dumps(
            {
                "status": "complete",
                "evidence": str(args.evidence),
                "stage": report.stage.value,
                "paper_eligible": report.paper_eligible,
                "live_eligible": report.live_eligible,
                "approved_for_live": report.approved_for_live,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()

