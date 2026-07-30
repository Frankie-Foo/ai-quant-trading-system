"""Versioned immutable contracts for observations, scores, and advice."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class FrozenModel(BaseModel):
    """Base model that rejects unknown fields and mutation."""

    model_config = ConfigDict(extra="forbid", frozen=True)


ComponentName: TypeAlias = Literal[
    "price_trend",
    "price_oi",
    "funding",
    "signed_flow",
    "liquidation",
    "basis",
]


def require_utc(value: datetime, *, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")
    return value.astimezone(UTC)


class Regime(StrEnum):
    RISK_ON = "risk_on"
    NEUTRAL = "neutral"
    RISK_OFF = "risk_off"
    CONFLICTED = "conflicted"
    UNAVAILABLE = "unavailable"


class Scope(StrEnum):
    MARKET = "market"
    SECTOR = "sector"
    CUSTOM = "custom"


class ProviderStatus(FrozenModel):
    venue: str = Field(min_length=1)
    status: Literal["ok", "partial", "unavailable"]
    observation_count: int = Field(ge=0)
    warnings: tuple[str, ...] = ()


class PerpObservation(FrozenModel):
    venue: str = Field(min_length=1)
    market: str = Field(min_length=1)
    instrument: str = Field(min_length=1)
    observed_at_utc: datetime
    mark_price: float = Field(gt=0)
    oracle_price: float | None = Field(default=None, gt=0)
    reference_price: float | None = Field(default=None, gt=0)
    open_interest: float | None = Field(default=None, ge=0)
    funding_rate: float | None = None
    notional_volume_24h: float | None = Field(default=None, ge=0)
    bid_price: float | None = Field(default=None, gt=0)
    ask_price: float | None = Field(default=None, gt=0)
    aggressor_imbalance: float | None = Field(default=None, ge=-1, le=1)
    aggressor_trade_count: int | None = Field(default=None, ge=0)
    long_liquidation_usd: float | None = Field(default=None, ge=0)
    short_liquidation_usd: float | None = Field(default=None, ge=0)
    liquidation_event_count: int | None = Field(default=None, ge=0)
    active: bool = True
    provenance: str = Field(min_length=1)

    @field_validator("venue", "market", mode="after")
    @classmethod
    def normalize_lower(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("instrument", mode="after")
    @classmethod
    def normalize_instrument(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("observed_at_utc")
    @classmethod
    def validate_observed_at(cls, value: datetime) -> datetime:
        return require_utc(value, name="observed_at_utc")

    @model_validator(mode="after")
    def validate_book(self) -> PerpObservation:
        if (self.bid_price is None) is not (self.ask_price is None):
            raise ValueError("bid_price and ask_price must be supplied together")
        if (
            self.bid_price is not None
            and self.ask_price is not None
            and self.ask_price <= self.bid_price
        ):
            raise ValueError("ask_price must exceed bid_price")
        return self

    @property
    def key(self) -> tuple[str, str, str]:
        return self.venue, self.market, self.instrument


class ComponentEvidence(FrozenModel):
    name: ComponentName
    weight: float = Field(gt=0, le=1)
    score: float | None = Field(default=None, ge=-100, le=100)
    provenance: str


class SourceAssessment(FrozenModel):
    source_id: str
    target_id: str
    scope: Scope
    venue: str
    market: str
    instrument: str
    availability: Literal[
        "available",
        "missing_observation",
        "quality_rejected",
        "insufficient_evidence",
    ]
    score: float | None = Field(default=None, ge=-100, le=100)
    confidence: float = Field(ge=0, le=1)
    price_oi_regime: str
    components: tuple[ComponentEvidence, ...]
    quality_reasons: tuple[str, ...] = ()
    evidence_warnings: tuple[str, ...] = ()
    observed_at_utc: datetime | None = None
    previous_observed_at_utc: datetime | None = None
    provenance: str

    @field_validator("observed_at_utc", "previous_observed_at_utc")
    @classmethod
    def validate_optional_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else require_utc(value, name="observation time")


class TargetAssessment(FrozenModel):
    target_id: str
    scope: Scope
    regime: Regime
    score: float | None = Field(default=None, ge=-100, le=100)
    confidence: float = Field(ge=0, le=1)
    coverage: float = Field(ge=0, le=1)
    liquidation_coverage: float = Field(ge=0, le=1)
    disagreement: float | None = Field(default=None, ge=0, le=1)
    available_sources: int = Field(ge=0)
    configured_sources: int = Field(ge=0)
    available_venues: int = Field(ge=0)
    venue_conflict: bool = False
    boost_eligible: bool = False
    candidate_multiplier: float = Field(ge=0, le=1.2)
    effective_multiplier: float = Field(ge=0, le=1.2)
    pending_windows: int = Field(ge=0)
    confirmation_windows: int = Field(gt=0)
    reasons: tuple[str, ...] = ()
    sources: tuple[SourceAssessment, ...]
    asof_utc: datetime

    @field_validator("asof_utc")
    @classmethod
    def validate_asof(cls, value: datetime) -> datetime:
        return require_utc(value, name="asof_utc")


class RiskSnapshot(FrozenModel):
    schema_version: Literal["perp_risk_snapshot.v1"] = "perp_risk_snapshot.v1"
    skill_version: str
    snapshot_id: str
    asof_utc: datetime
    data_cutoff_utc: datetime
    config_hash: str
    actionable: bool
    session_state: Literal["actionable", "research_only", "market_closed"]
    provider_status: tuple[ProviderStatus, ...]
    targets: tuple[TargetAssessment, ...]
    warnings: tuple[str, ...] = ()
    production_eligible: Literal[False]
    execution_eligible: Literal[False]
    orders_submitted: Literal[0]

    @field_validator("asof_utc", "data_cutoff_utc")
    @classmethod
    def validate_snapshot_time(cls, value: datetime) -> datetime:
        return require_utc(value, name="snapshot time")

    @model_validator(mode="after")
    def enforce_read_only(self) -> RiskSnapshot:
        if self.production_eligible or self.execution_eligible or self.orders_submitted:
            raise ValueError("perpetual risk skill is read-only")
        return self


class PositionRecommendation(FrozenModel):
    schema_version: Literal["perp_risk_position_recommendation.v1"] = (
        "perp_risk_position_recommendation.v1"
    )
    skill_version: str
    recommendation_id: str
    snapshot_id: str
    asof_utc: datetime
    relevant_targets: tuple[str, ...]
    position_multiplier: float = Field(ge=0, le=1.2)
    action: Literal["cash", "reduce", "hold", "increase", "research_only"]
    actionable: bool
    base_target_position_pct: float | None = Field(default=None, ge=0, le=100)
    adjusted_target_position_pct: float | None = Field(default=None, ge=0, le=100)
    reasons: tuple[str, ...]
    production_eligible: Literal[False]
    execution_eligible: Literal[False]
    orders_submitted: Literal[0]

    @field_validator("asof_utc")
    @classmethod
    def validate_recommendation_time(cls, value: datetime) -> datetime:
        return require_utc(value, name="asof_utc")

    @model_validator(mode="after")
    def enforce_read_only(self) -> PositionRecommendation:
        if self.production_eligible or self.execution_eligible or self.orders_submitted:
            raise ValueError("recommendation cannot authorize execution")
        if (self.base_target_position_pct is None) is not (
            self.adjusted_target_position_pct is None
        ):
            raise ValueError("base and adjusted position must be supplied together")
        return self


class OutcomeRecord(FrozenModel):
    schema_version: Literal["perp_risk_outcome.v1"] = "perp_risk_outcome.v1"
    snapshot_id: str
    target_id: str
    kind: Literal["benchmark", "trade"]
    observed_at_utc: datetime
    horizon_minutes: int = Field(gt=0, le=10_080)
    return_pct: float
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("observed_at_utc")
    @classmethod
    def validate_outcome_time(cls, value: datetime) -> datetime:
        return require_utc(value, name="observed_at_utc")


class ReviewReport(FrozenModel):
    schema_version: Literal["perp_risk_review.v1"] = "perp_risk_review.v1"
    generated_at_utc: datetime
    outcome_count: int = Field(ge=0)
    benchmark_count: int = Field(ge=0)
    trade_count: int = Field(ge=0)
    directional_samples: int = Field(ge=0)
    directional_accuracy: float | None = Field(default=None, ge=0, le=1)
    average_overlay_contribution_pct: float | None = None
    average_trade_return_pct: float | None = None
    warnings: tuple[str, ...] = ()

    @field_validator("generated_at_utc")
    @classmethod
    def validate_generated_at(cls, value: datetime) -> datetime:
        return require_utc(value, name="generated_at_utc")
