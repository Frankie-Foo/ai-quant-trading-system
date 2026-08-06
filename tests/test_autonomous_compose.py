from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_autonomous_services_share_a_dedicated_sip_database() -> None:
    config = yaml.safe_load(
        (ROOT / "compose.autonomous-paper.yaml").read_text(encoding="utf-8")
    )

    refresher = config["services"]["sip-refresher"]["command"]
    executor = config["services"]["paper-executor"]["command"]

    assert "/app/runs/autonomous-sip.sqlite3" in refresher
    assert "/app/runs/autonomous-sip.sqlite3" in executor
    assert "/app/runs/sip-stream.sqlite3" not in refresher
    assert "/app/runs/sip-stream.sqlite3" not in executor
