from __future__ import annotations

import json
from pathlib import Path

from perp_risk.cli import main


def test_init_doctor_and_schema_are_offline(
    tmp_path: Path,
    capsys: object,
) -> None:
    config = tmp_path / "config.yaml"
    assert main(["init", "--output", str(config)]) == 0
    assert main(["--config", str(config), "doctor"]) == 0
    schema_dir = tmp_path / "schemas"
    assert main(["schema", "--output-dir", str(schema_dir)]) == 0

    assert (schema_dir / "risk-snapshot-v1.schema.json").is_file()
    for filename in (
        "risk-snapshot-v1.schema.json",
        "position-recommendation-v1.schema.json",
    ):
        payload = json.loads((schema_dir / filename).read_text(encoding="utf-8"))
        assert payload["properties"]["production_eligible"]["const"] is False
        assert payload["properties"]["execution_eligible"]["const"] is False
        assert payload["properties"]["orders_submitted"]["const"] == 0
        assert {
            "production_eligible",
            "execution_eligible",
            "orders_submitted",
        } <= set(payload["required"])
