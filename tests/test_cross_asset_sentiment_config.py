from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from operations.cross_asset_sentiment_config import (
    CrossAssetSentimentConfig,
    load_cross_asset_sentiment_config,
)

ROOT = Path(__file__).resolve().parents[1]


def test_repository_cross_asset_config_is_shadow_only_and_multi_venue() -> None:
    config = load_cross_asset_sentiment_config(
        ROOT / "config" / "cross_asset_sentiment.yaml"
    )

    assert config.shadow_only is True
    assert {binding.venue for binding in config.bindings} == {
        "hyperliquid",
        "aevo",
    }
    assert {binding.target_id for binding in config.bindings} == {
        "global-risk"
    }
    assert config.collection_interval_seconds == 60


def test_cross_asset_config_cannot_enable_production() -> None:
    with pytest.raises(ValidationError, match="shadow"):
        CrossAssetSentimentConfig.model_validate(
            {
                "schema_version": "cross_asset_sentiment.v1",
                "shadow_only": False,
                "collection_interval_seconds": 60,
                "policy": {},
                "bindings": [
                    {
                        "target_id": "global-risk",
                        "scope": "market",
                        "venue": "hyperliquid",
                        "market": "main",
                        "instrument": "BTC",
                        "weight": 1.0,
                    }
                ],
            }
        )
