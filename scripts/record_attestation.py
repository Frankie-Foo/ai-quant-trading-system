"""Record or revoke a named external maturity attestation with an evidence reference."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Literal, get_args

from operations.evidence import (
    load_existing_evidence,
    refresh_maturity_evidence,
    write_evidence_atomic,
)
from operations.readiness import Attestation

ROOT = Path(__file__).resolve().parents[1]
AttestationField = Literal[
    "full_market_realtime_data",
    "historical_data_license",
    "paper_broker_access",
    "kill_switch_drill",
    "alert_delivery_drill",
    "backup_restore_drill",
    "broker_recovery_drill",
    "secrets_rotated",
    "owner_risk_signoff",
    "compliance_signoff",
    "live_broker_permission",
]


def record_attestation(
    *,
    evidence_path: Path,
    data_root: Path,
    order_db: Path,
    field: AttestationField,
    evidence_ref: str | None,
    revoke: bool,
) -> None:
    if not revoke and (evidence_ref is None or not evidence_ref.strip()):
        raise ValueError("a non-empty evidence reference is required")
    evidence = refresh_maturity_evidence(
        data_root=data_root,
        order_db=order_db,
        existing=load_existing_evidence(evidence_path),
    )
    value = (
        Attestation()
        if revoke
        else Attestation(passed=True, evidence_refs=(str(evidence_ref).strip(),))
    )
    write_evidence_atomic(evidence.model_copy(update={field: value}), evidence_path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--field", choices=get_args(AttestationField), required=True)
    parser.add_argument("--evidence-ref")
    parser.add_argument("--revoke", action="store_true")
    parser.add_argument("--data-root", type=Path, default=ROOT / "data")
    parser.add_argument("--order-db", type=Path, default=ROOT / "runs/paper-orders.sqlite3")
    parser.add_argument("--evidence", type=Path, default=ROOT / "runs/maturity-evidence.json")
    args = parser.parse_args()
    record_attestation(
        evidence_path=args.evidence,
        data_root=args.data_root,
        order_db=args.order_db,
        field=args.field,
        evidence_ref=args.evidence_ref,
        revoke=args.revoke,
    )
    print(
        json.dumps(
            {
                "status": "revoked" if args.revoke else "recorded",
                "field": args.field,
                "evidence": str(args.evidence),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()

