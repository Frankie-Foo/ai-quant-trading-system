from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_retained_autonomous_compose_is_read_only_without_executor() -> None:
    config = yaml.safe_load(
        (ROOT / "compose.autonomous-paper.yaml").read_text(encoding="utf-8")
    )

    refresher = config["services"]["sip-refresher"]["command"]

    assert "/app/runs/autonomous-sip.sqlite3" in refresher
    assert "/app/runs/sip-stream.sqlite3" not in refresher
    assert set(config["services"]) == {"sip-refresher", "runtime-agents"}
