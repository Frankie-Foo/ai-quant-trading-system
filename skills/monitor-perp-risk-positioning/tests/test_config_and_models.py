from __future__ import annotations

from datetime import UTC, datetime

import pytest
from perp_risk.config import AppConfig, load_config
from perp_risk.models import PerpObservation, RiskSnapshot
from pydantic import ValidationError


def test_default_config_contains_representative_targets() -> None:
    config = load_config()

    assert config.collection.interval_seconds == 60
    assert config.policy.boost_multiplier == 1.2
    assert config.policy.require_liquidation_for_boost is True
    assert {(item.target_id, item.market, item.instrument) for item in config.bindings} >= {
        ("energy-risk", "xyz", "CL"),
        ("semiconductor-risk", "xyz", "SMH"),
    }
    volatility = next(item for item in config.targets if item.target_id == "volatility-risk")
    assert volatility.enabled is False
    assert volatility.unavailable_reason == "no_active_vix_source"


def test_config_hash_is_deterministic() -> None:
    first = load_config()
    second = AppConfig.model_validate(first.model_dump(mode="json"))

    assert first.config_hash == second.config_hash


def test_observation_rejects_crossed_book() -> None:
    with pytest.raises(ValidationError, match="ask_price must exceed"):
        PerpObservation(
            venue="hyperliquid",
            market="main",
            instrument="BTC",
            observed_at_utc=datetime(2026, 7, 30, 14, tzinfo=UTC),
            mark_price=100,
            bid_price=101,
            ask_price=100,
            provenance="test",
        )


def test_sparse_trade_count_is_preserved_without_fake_imbalance() -> None:
    observation = PerpObservation(
        venue="hyperliquid",
        market="xyz",
        instrument="SMH",
        observed_at_utc=datetime(2026, 7, 30, 14, tzinfo=UTC),
        mark_price=500,
        bid_price=499,
        ask_price=501,
        aggressor_trade_count=2,
        aggressor_imbalance=None,
        provenance="test",
    )

    assert observation.aggressor_trade_count == 2
    assert observation.aggressor_imbalance is None


def test_snapshot_cannot_authorize_execution() -> None:
    with pytest.raises(ValidationError, match="False"):
        RiskSnapshot(
            skill_version="0.1.0",
            snapshot_id="test",
            asof_utc=datetime(2026, 7, 30, 14, tzinfo=UTC),
            data_cutoff_utc=datetime(2026, 7, 30, 14, tzinfo=UTC),
            config_hash="abc",
            actionable=True,
            session_state="actionable",
            provider_status=(),
            targets=(),
            production_eligible=False,
            execution_eligible=True,  # type: ignore[arg-type]
            orders_submitted=0,
        )
