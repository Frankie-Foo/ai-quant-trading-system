from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from kernel.config import load_config
from kernel.strategy_policy import (
    build_strategy_policy,
    load_strategy_policy,
    write_strategy_policy,
)

NOW = datetime(2026, 8, 27, 1, 0, tzinfo=UTC)


def test_active_policy_overrides_only_min_rvol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "active-policy.json"
    policy = build_strategy_policy(
        version="selection-2026.09.01",
        status="active",
        min_rvol=4.0,
        created_at_utc=NOW,
        approved_by="owner",
        approved_at_utc=NOW,
    )
    write_strategy_policy(path, policy)
    monkeypatch.setenv("AI_QUANT_ACTIVE_POLICY_FILE", str(path))

    config = load_config("config.yaml")

    assert config.universe.min_rvol == 4.0
    assert config.universe.min_market_cap_usd == 1_000_000_000
    assert load_strategy_policy(path, required_status="active") == policy


def test_policy_hash_detects_tampering(tmp_path: Path) -> None:
    path = tmp_path / "active-policy.json"
    policy = build_strategy_policy(
        version="selection-2026.09.01",
        status="active",
        min_rvol=4.0,
        created_at_utc=NOW,
        approved_by="owner",
        approved_at_utc=NOW,
    )
    write_strategy_policy(path, policy)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["parameter_overrides"]["universe.min_rvol"] = 2.0
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="hash"):
        load_strategy_policy(path)


def test_policy_rejects_unknown_or_unsafe_parameters() -> None:
    with pytest.raises(ValueError, match="allowlisted"):
        build_strategy_policy(
            version="selection-2026.09.01",
            status="shadow",
            min_rvol=4.0,
            created_at_utc=NOW,
            extra_overrides={"risk_per_trade": 0.02},
        )

    with pytest.raises(ValueError, match="between"):
        build_strategy_policy(
            version="selection-2026.09.01",
            status="shadow",
            min_rvol=9.0,
            created_at_utc=NOW,
        )


def test_load_config_rejects_shadow_policy_as_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "challenger.json"
    write_strategy_policy(
        path,
        build_strategy_policy(
            version="selection-shadow-2026.09",
            status="shadow",
            min_rvol=4.0,
            created_at_utc=NOW,
            source_snapshot_ids=("research-snapshot",),
        ),
    )
    monkeypatch.setenv("AI_QUANT_ACTIVE_POLICY_FILE", str(path))

    with pytest.raises(ValueError, match="active"):
        load_config("config.yaml")
