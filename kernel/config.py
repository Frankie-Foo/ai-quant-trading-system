from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

HH_MM_PATTERN = r"^(?:[01]\d|2[0-3]):[0-5]\d$"


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TierConfig(FrozenModel):
    min_cap: float = Field(ge=0)
    weight: float = Field(gt=0, le=1)


class TiersConfig(FrozenModel):
    mega: TierConfig
    large: TierConfig
    mid: TierConfig
    small: TierConfig


class ExitConfig(FrozenModel):
    k_tp: float = Field(gt=0)
    k_sl: float = Field(gt=0)
    time_stop_et: str = Field(pattern=HH_MM_PATTERN)


class UniverseConfig(FrozenModel):
    min_price: float = Field(gt=0)
    min_rvol: float = Field(gt=0)
    min_premarket_return: float = Field(ge=0)
    min_premarket_gap_return: float = Field(ge=0)
    min_premarket_close_location: float = Field(ge=0, le=1)
    min_beta: float = Field(gt=0)
    min_atr_pct: float = Field(gt=0)
    max_bearish_open_to_close_return: float = Field(lt=0)
    min_distribution_volume_ratio: float = Field(gt=1)
    max_distribution_close_location: float = Field(ge=0, le=1)
    luld_low_float_shares: int = Field(gt=0)


class CostConfig(FrozenModel):
    commission_per_share: float = Field(ge=0)
    commission_min: float = Field(ge=0)
    sec_fee_per_million_sold: float = Field(ge=0)
    finra_taf_per_share_sold: float = Field(ge=0)
    spread_capture: float = Field(ge=1)
    impact_k: float = Field(ge=0)
    stop_slippage_atr: float = Field(ge=0)


class MarketDataConfig(FrozenModel):
    max_quote_age_seconds: float = Field(gt=0, le=120)
    paper_start_lead_minutes: int = Field(ge=0, le=60)
    postmarket_data_grace_minutes: int = Field(ge=0, le=60)
    sip_event_stale_seconds: float = Field(gt=0, le=300)


class GuardrailConfig(FrozenModel):
    daily_loss_limit: float = Field(gt=0, lt=1)
    lock_time_beijing: str = Field(pattern=HH_MM_PATTERN)
    selection_time_beijing: str = Field(pattern=HH_MM_PATTERN)
    noise_cutoff_beijing: str = Field(pattern=HH_MM_PATTERN)


class SchedulerConfig(FrozenModel):
    premarket_retry_minutes: int = Field(gt=0, le=120)
    premarket_max_attempts: int = Field(gt=0, le=100)
    postmarket_retry_minutes: int = Field(gt=0, le=120)
    postmarket_max_attempts: int = Field(gt=0, le=20)


class Config(FrozenModel):
    capital: float = Field(gt=0)
    risk_per_trade: float = Field(ge=0.003, le=0.005)
    max_concurrent: int = Field(gt=0)
    max_gross_exposure: float = Field(gt=0, le=1)
    long_only: bool
    tiers: TiersConfig
    exits: ExitConfig
    participation_cap: float = Field(gt=0, le=1)
    universe: UniverseConfig
    costs: CostConfig
    market_data: MarketDataConfig
    guardrails: GuardrailConfig
    scheduler: SchedulerConfig

    @field_validator("long_only")
    @classmethod
    def long_only_is_permanent(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("long_only is a permanent system invariant")
        return value


def load_config(path: str | Path) -> Config:
    config_path = Path(path)
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("config root must be a mapping")
    return Config.model_validate(payload)
