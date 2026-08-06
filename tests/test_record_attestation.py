from __future__ import annotations

from pathlib import Path

import pytest

from operations.evidence import load_existing_evidence
from scripts.record_attestation import record_attestation


def test_attestation_requires_reference_and_can_be_revoked(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.json"
    with pytest.raises(ValueError, match="reference"):
        record_attestation(
            evidence_path=evidence,
            data_root=tmp_path / "data",
            order_db=tmp_path / "orders.sqlite3",
            field="historical_data_license",
            evidence_ref=None,
            revoke=False,
        )
    record_attestation(
        evidence_path=evidence,
        data_root=tmp_path / "data",
        order_db=tmp_path / "orders.sqlite3",
        field="historical_data_license",
        evidence_ref="contract:alpaca-plus-2026-07-21",
        revoke=False,
    )
    current = load_existing_evidence(evidence)
    assert current is not None
    assert current.historical_data_license.passed is True

    record_attestation(
        evidence_path=evidence,
        data_root=tmp_path / "data",
        order_db=tmp_path / "orders.sqlite3",
        field="historical_data_license",
        evidence_ref=None,
        revoke=True,
    )
    revoked = load_existing_evidence(evidence)
    assert revoked is not None
    assert revoked.historical_data_license.passed is False

