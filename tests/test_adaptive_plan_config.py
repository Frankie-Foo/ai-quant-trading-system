from __future__ import annotations

import json
from pathlib import Path

import pytest

from kernel.adaptive_trade_plan import PlanMode
from operations.adaptive_plan_config import load_adaptive_plan_config


def _payload() -> dict[str, object]:
    return {
        "schema_version": "adaptive_plan_config.v1",
        "poll_seconds": 15,
        "plans": [
            {
                "baseline": {
                    "plan_id": "plan-20260728-XYZ",
                    "symbol": "XYZ",
                    "trade_date": "2026-07-28",
                    "mode": "catalyst",
                    "entry_window_end_utc": "2026-07-28T15:30:00+00:00",
                    "force_exit_utc": "2026-07-28T19:55:00+00:00",
                    "hard_stop": 99.0,
                    "max_risk_dollars": 300.0,
                    "max_notional": 20000.0,
                    "probe_fraction": 0.25,
                    "max_spread_ratio": 0.0025,
                    "soft_cooldown_seconds": 180,
                    "max_soft_revisions": 3,
                },
                "evidence": {
                    "benchmark_symbol": "SPY",
                    "sector_symbol": "XLK",
                    "catalyst_score": 0.82,
                    "provenance": "accepted.selection@test",
                },
            }
        ],
    }


def test_config_loads_baseline_and_evidence_without_secrets(tmp_path: Path) -> None:
    path = tmp_path / "plans.json"
    path.write_text(json.dumps(_payload()), encoding="utf-8")

    loaded = load_adaptive_plan_config(path)

    assert loaded.poll_seconds == 15
    assert loaded.plans[0].mode is PlanMode.CATALYST
    assert loaded.plans[0].probe_fraction == 0.25
    assert loaded.evidence[loaded.plans[0].plan_id].sector_symbol == "XLK"


def test_config_rejects_unknown_schema_and_fast_polling(tmp_path: Path) -> None:
    payload = _payload()
    payload["schema_version"] = "unknown"
    payload["poll_seconds"] = 1
    path = tmp_path / "plans.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError):
        load_adaptive_plan_config(path)
