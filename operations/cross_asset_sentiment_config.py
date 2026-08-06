"""Validated deployment configuration for cross-asset sentiment collection."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from kernel.cross_asset_sentiment import (
    CrossAssetSentimentPolicy,
    ProxyBinding,
)


class CrossAssetSentimentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = Field(pattern=r"^cross_asset_sentiment\.v\d+$")
    shadow_only: bool
    collection_interval_seconds: int = Field(ge=15, le=300)
    history_snapshot_limit: int = Field(default=120, ge=1, le=10_000)
    policy: CrossAssetSentimentPolicy
    bindings: tuple[ProxyBinding, ...] = Field(min_length=1)

    @field_validator("shadow_only")
    @classmethod
    def require_shadow_only(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("cross-asset sentiment must remain shadow-only")
        return value

    @model_validator(mode="after")
    def validate_bindings(self) -> CrossAssetSentimentConfig:
        keys: set[tuple[str, str, str, str]] = set()
        for binding in self.bindings:
            if binding.venue not in {"hyperliquid", "aevo"}:
                raise ValueError("unsupported cross-asset sentiment venue")
            if binding.venue == "aevo" and binding.market != "mainnet":
                raise ValueError("Aevo binding must use mainnet")
            key = (
                binding.target_id,
                binding.venue,
                binding.market,
                binding.instrument,
            )
            if key in keys:
                raise ValueError("cross-asset sentiment bindings must be unique")
            keys.add(key)
        return self


def load_cross_asset_sentiment_config(
    path: str | Path,
) -> CrossAssetSentimentConfig:
    config_path = Path(path)
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("cross-asset sentiment config root must be a mapping")
    return CrossAssetSentimentConfig.model_validate(payload)
