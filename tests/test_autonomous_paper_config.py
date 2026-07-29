from __future__ import annotations

import json
from pathlib import Path

import pytest

from kernel.intraday_policy import EntryRoute
from operations.autonomous_paper_config import load_autonomous_paper_config

ROOT = Path(__file__).resolve().parents[1]


def _payload() -> dict[str, object]:
    return {
        "schema_version": "autonomous_paper_config.v1",
        "poll_seconds": 15,
        "plans": [
            {
                "plan": {
                    "plan_id": "auto-20260729-XYZ",
                    "symbol": "XYZ",
                    "trade_date": "2026-07-29",
                    "reference_price": "102.01",
                    "hard_stop": "98.00",
                    "max_notional_fraction": "0.20",
                    "full_risk_fraction": "0.0035",
                    "max_spread_ratio": "0.0025",
                    "source_snapshot_ids": ["selection-20260729"],
                    "provenance": "accepted.selection:auto-plan",
                },
                "policy_evidence": {
                    "route": "catalyst",
                    "catalyst": {
                        "value": 88.0,
                        "asof_utc": "2026-07-29T13:20:00+00:00",
                        "provenance": "accepted.selection:catalyst",
                    },
                    "factor": {
                        "value": None,
                        "asof_utc": "2026-07-29T13:20:00+00:00",
                        "provenance": "accepted.selection:factor-unavailable",
                    },
                    "right_tail": {
                        "value": 76.0,
                        "asof_utc": "2026-07-29T13:20:00+00:00",
                        "provenance": "accepted.selection:right-tail",
                    },
                    "first_target_reward_r": 2.5,
                    "weighted_expected_reward_r": 3.2,
                    "reward_risk_provenance": "owner-plan:r-multiple.v1",
                    "a_plus_plus_approved": False,
                },
                "market_context": {
                    "benchmark_symbol": "SPY",
                    "sector_symbol": "XLK",
                    "provenance": "owner-plan:market-context.v1",
                },
                "safety_envelope": "../runs/safety/XYZ.json",
            }
        ],
    }


def test_autonomous_config_loads_secret_free_plan_and_resolves_paths(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config" / "autonomous.json"
    config_path.parent.mkdir()
    config_path.write_text(json.dumps(_payload()), encoding="utf-8")

    config = load_autonomous_paper_config(config_path)

    assert config.poll_seconds == 15
    assert len(config.plans) == 1
    bundle = config.plans[0]
    assert bundle.plan.symbol == "XYZ"
    assert bundle.plan.hard_stop.is_finite()
    assert bundle.evidence.route is EntryRoute.CATALYST
    assert bundle.evidence.catalyst.value == 88.0
    assert bundle.benchmark_symbol == "SPY"
    assert bundle.sector_symbol == "XLK"
    assert bundle.safety_envelope_path == (
        tmp_path / "runs" / "safety" / "XYZ.json"
    ).resolve()
    raw = config_path.read_text(encoding="utf-8").lower()
    assert "secret" not in raw
    assert "api_key" not in raw


def test_autonomous_config_rejects_long_plan_with_stop_above_reference(
    tmp_path: Path,
) -> None:
    payload = _payload()
    plans = payload["plans"]
    assert isinstance(plans, list)
    item = plans[0]
    assert isinstance(item, dict)
    plan = item["plan"]
    assert isinstance(plan, dict)
    plan["hard_stop"] = "103.00"
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="hard_stop"):
        load_autonomous_paper_config(path)


def test_autonomous_config_rejects_unknown_fields(tmp_path: Path) -> None:
    payload = _payload()
    payload["broker_secret"] = "must-not-be-here"
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="unexpected fields"):
        load_autonomous_paper_config(path)


def test_repository_autonomous_example_is_loadable_and_secret_free() -> None:
    path = ROOT / "config" / "autonomous_paper.example.json"

    config = load_autonomous_paper_config(path)

    assert len(config.plans) == 1
    assert "secret" not in path.read_text(encoding="utf-8").lower()
