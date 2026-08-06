from __future__ import annotations

import argparse
import json
from pathlib import Path

from operations.drills import run_local_safety_drills
from operations.evidence import (
    load_existing_evidence,
    refresh_maturity_evidence,
    write_evidence_atomic,
)
from operations.readiness import Attestation

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=ROOT / "data")
    parser.add_argument("--state-root", type=Path, default=ROOT / "runs")
    parser.add_argument("--backup-dir", type=Path, default=ROOT / "runs/drill-backups")
    parser.add_argument("--receipt-dir", type=Path, default=ROOT / "runs/drills")
    parser.add_argument("--evidence", type=Path, default=ROOT / "runs/maturity-evidence.json")
    args = parser.parse_args()
    receipt, receipt_path = run_local_safety_drills(
        root=ROOT,
        data_root=args.data_root,
        state_root=args.state_root,
        backup_dir=args.backup_dir,
        receipt_dir=args.receipt_dir,
    )
    evidence = refresh_maturity_evidence(
        data_root=args.data_root,
        order_db=args.state_root / "paper-orders.sqlite3",
        existing=load_existing_evidence(args.evidence),
    )
    reference = f"local-receipt:{receipt_path}"
    evidence = evidence.model_copy(
        update={
            "kill_switch_drill": Attestation(
                passed=receipt.kill_switch_passed, evidence_refs=(reference,)
            ),
            "backup_restore_drill": Attestation(
                passed=receipt.backup_restore_passed, evidence_refs=(reference,)
            ),
        }
    )
    write_evidence_atomic(evidence, args.evidence)
    print(
        json.dumps(
            {
                "status": "complete",
                "receipt": str(receipt_path),
                "kill_switch_passed": receipt.kill_switch_passed,
                "broker_calls": receipt.broker_calls_during_kill_switch,
                "backup_restore_passed": receipt.backup_restore_passed,
                "restored_files": receipt.restored_file_count,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()

