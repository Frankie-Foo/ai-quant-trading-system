from __future__ import annotations

import json
from pathlib import Path

from operations.paper_acceptance_drills import run_paper_acceptance_drills

ROOT = Path(__file__).parents[1]


def test_acceptance_drills_are_local_tamper_evident_and_pass(tmp_path: Path) -> None:
    output = tmp_path / "receipt.json"

    receipt = run_paper_acceptance_drills(root=ROOT, output_path=output)

    assert receipt.passed
    assert receipt.broker_calls == 0
    assert receipt.external_writes == 0
    assert len(receipt.receipt_sha256) == 64
    assert json.loads(output.read_text(encoding="utf-8"))["receipt_sha256"] == (
        receipt.receipt_sha256
    )
