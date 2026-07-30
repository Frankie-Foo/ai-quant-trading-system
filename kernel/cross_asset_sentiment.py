"""Deterministic cross-asset perpetual sentiment with explicit degradation.

The module is deliberately execution-blind. External adapters normalize venue data
into :class:`PerpObservation`; callers receive immutable, shadow-only assessments.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SentimentScope(StrEnum):
    MARKET = "market"
    SECTOR = "sector"
    THEME = "theme"


class SentimentRegime(StrEnum):
    RISK_ON = "risk_on"
    NEUTRAL = "neutral"
    RISK_OFF = "risk_off"
    UNAVAILABLE = "unavailable"


class PerpObservation(FrozenModel):
    """One causally observed, venue-normalized perpetual market state."""

    venue: str = Field(min_length=1)
    market: str = Field(min_length=1)
    instrument: str = Field(min_length=1)
    observed_at_utc: datetime
    mark_price: float = Field(gt=0, allow_inf_nan=False)
    oracle_price: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    reference_price: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    open_interest: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    funding_rate: float | None = Field(default=None, allow_inf_nan=False)
    notional_volume_24h: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    bid_price: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    ask_price: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    aggressor_imbalance: float | None = Field(
        default=None,
        ge=-1,
        le=1,
        allow_inf_nan=False,
    )
    long_liquidation_usd: float | None = Field(
        default=None,
        ge=0,
        allow_inf_nan=False,
    )
    short_liquidation_usd: float | None = Field(
        default=None,
        ge=0,
        allow_inf_nan=False,
    )
    active: bool
    provenance: str = Field(min_length=1)

    @field_validator("observed_at_utc")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("observed_at_utc must be timezone-aware UTC")
        return value

    @field_validator("venue", "market", "instrument")
    @classmethod
    def normalize_identity(cls, value: str) -> str:
        return value.strip().lower()

    @model_validator(mode="after")
    def validate_quote(self) -> PerpObservation:
        if (self.bid_price is None) != (self.ask_price is None):
            raise ValueError("bid and ask must be both available or both unavailable")
        if (
            self.bid_price is not None
            and self.ask_price is not None
            and self.ask_price <= self.bid_price
        ):
            raise ValueError("ask_price must be above bid_price")
        if (self.long_liquidation_usd is None) != (
            self.short_liquidation_usd is None
        ):
            raise ValueError(
                "long and short liquidation amounts must be jointly available"
            )
        return self

    @property
    def key(self) -> tuple[str, str, str]:
        return self.venue, self.market, self.instrument


class ProxyBinding(FrozenModel):
    """Map one venue instrument into one market, sector, or theme target."""

    target_id: str = Field(min_length=1)
    scope: SentimentScope
    venue: str = Field(min_length=1)
    market: str = Field(min_length=1)
    instrument: str = Field(min_length=1)
    weight: float = Field(gt=0, allow_inf_nan=False)
    min_notional_volume_24h: float | None = Field(
        default=None,
        ge=0,
        allow_inf_nan=False,
    )

    @field_validator("target_id", "venue", "market", "instrument")
    @classmethod
    def normalize_identity(cls, value: str) -> str:
        return value.strip().lower()

    @property
    def observation_key(self) -> tuple[str, str, str]:
        return self.venue, self.market, self.instrument


class CrossAssetSentimentPolicy(FrozenModel):
    max_age_seconds: float = Field(default=90, gt=0, le=900)
    max_spread_bps: float = Field(default=150, gt=0, le=5_000)
    max_abs_basis: float = Field(default=0.05, gt=0, lt=1)
    price_return_full_scale: float = Field(default=0.02, gt=0, lt=1)
    open_interest_change_full_scale: float = Field(default=0.02, gt=0, lt=1)
    basis_full_scale: float = Field(default=0.01, gt=0, lt=1)
    moderate_funding_abs: float = Field(default=0.0001, gt=0, lt=1)
    extreme_funding_abs: float = Field(default=0.001, gt=0, lt=1)
    minimum_component_weight: float = Field(default=0.35, gt=0, le=1)
    risk_on_threshold: float = Field(default=25, gt=0, le=100)
    risk_off_threshold: float = Field(default=-25, ge=-100, lt=0)

    @model_validator(mode="after")
    def validate_thresholds(self) -> CrossAssetSentimentPolicy:
        if self.extreme_funding_abs <= self.moderate_funding_abs:
            raise ValueError(
                "extreme_funding_abs must exceed moderate_funding_abs"
            )
        return self


class InstrumentSentimentAssessment(FrozenModel):
    target_id: str
    scope: SentimentScope
    venue: str
    market: str
    instrument: str
    availability: str
    score: float | None = Field(default=None, ge=-100, le=100)
    confidence: float = Field(ge=0, le=1)
    price_return: float | None = None
    open_interest_change: float | None = None
    price_oi_regime: str
    component_scores: dict[str, float | None]
    quality_reasons: tuple[str, ...]
    observed_at_utc: datetime | None
    provenance: str
    production_eligible: bool = False

    @field_validator("production_eligible")
    @classmethod
    def forbid_production(cls, value: bool) -> bool:
        if value:
            raise ValueError("cross-asset sentiment is shadow-only")
        return value


class TargetSentimentAssessment(FrozenModel):
    target_id: str
    scope: SentimentScope
    regime: SentimentRegime
    score: float | None = Field(default=None, ge=-100, le=100)
    confidence: float = Field(ge=0, le=1)
    available_sources: int = Field(ge=0)
    configured_sources: int = Field(gt=0)
    disagreement: float | None = Field(default=None, ge=0, le=1)
    source_scores: dict[str, float | None]
    asof_utc: datetime
    production_eligible: bool = False

    @field_validator("production_eligible")
    @classmethod
    def forbid_production(cls, value: bool) -> bool:
        if value:
            raise ValueError("cross-asset sentiment is shadow-only")
        return value


class CrossAssetSentimentResult(FrozenModel):
    asof_utc: datetime
    instrument_assessments: tuple[InstrumentSentimentAssessment, ...]
    target_assessments: tuple[TargetSentimentAssessment, ...]
    production_eligible: bool = False

    @field_validator("production_eligible")
    @classmethod
    def forbid_production(cls, value: bool) -> bool:
        if value:
            raise ValueError("cross-asset sentiment is shadow-only")
        return value


_COMPONENT_WEIGHTS = {
    "price_trend": 0.20,
    "price_oi": 0.25,
    "funding": 0.15,
    "signed_flow": 0.20,
    "liquidation": 0.10,
    "basis": 0.10,
}


class CrossAssetSentimentEngine:
    """Deep deterministic module for quality-gated, cross-venue sentiment."""

    def __init__(
        self,
        *,
        policy: CrossAssetSentimentPolicy,
        bindings: tuple[ProxyBinding, ...],
    ):
        if not bindings:
            raise ValueError("at least one cross-asset proxy binding is required")
        keys = [
            (item.target_id, *item.observation_key)
            for item in bindings
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("cross-asset proxy bindings must be unique")
        scopes: dict[str, SentimentScope] = {}
        for binding in bindings:
            prior = scopes.setdefault(binding.target_id, binding.scope)
            if prior is not binding.scope:
                raise ValueError("one target_id cannot mix sentiment scopes")
        self.policy = policy
        self.bindings = bindings

    def evaluate(
        self,
        *,
        observations: tuple[PerpObservation, ...],
        previous_observations: tuple[PerpObservation, ...] = (),
        asof_utc: datetime,
    ) -> CrossAssetSentimentResult:
        _require_utc(asof_utc, name="asof_utc")
        current = _unique_observations(observations, name="observations")
        previous = _unique_observations(
            previous_observations,
            name="previous_observations",
        )
        instrument_assessments = tuple(
            self._assess_instrument(
                binding,
                current.get(binding.observation_key),
                previous.get(binding.observation_key),
                asof_utc=asof_utc,
            )
            for binding in self.bindings
        )
        target_assessments = self._aggregate_targets(
            instrument_assessments,
            asof_utc=asof_utc,
        )
        return CrossAssetSentimentResult(
            asof_utc=asof_utc,
            instrument_assessments=instrument_assessments,
            target_assessments=target_assessments,
        )

    def _assess_instrument(
        self,
        binding: ProxyBinding,
        current: PerpObservation | None,
        previous: PerpObservation | None,
        *,
        asof_utc: datetime,
    ) -> InstrumentSentimentAssessment:
        if current is None:
            return InstrumentSentimentAssessment(
                target_id=binding.target_id,
                scope=binding.scope,
                venue=binding.venue,
                market=binding.market,
                instrument=binding.instrument,
                availability="missing_observation",
                confidence=0,
                price_oi_regime="unavailable",
                component_scores={name: None for name in _COMPONENT_WEIGHTS},
                quality_reasons=("missing_observation",),
                observed_at_utc=None,
                provenance="N/A",
            )
        quality_reasons = self._quality_reasons(
            binding,
            current,
            asof_utc=asof_utc,
        )
        if quality_reasons:
            return InstrumentSentimentAssessment(
                target_id=binding.target_id,
                scope=binding.scope,
                venue=binding.venue,
                market=binding.market,
                instrument=binding.instrument,
                availability="quality_rejected",
                confidence=0,
                price_oi_regime="unavailable",
                component_scores={name: None for name in _COMPONENT_WEIGHTS},
                quality_reasons=quality_reasons,
                observed_at_utc=current.observed_at_utc,
                provenance=current.provenance,
            )

        reference_price = (
            previous.mark_price
            if previous is not None
            else current.reference_price
        )
        price_return = (
            None
            if reference_price is None
            else current.mark_price / reference_price - 1
        )
        oi_change = (
            None
            if previous is None
            or previous.open_interest is None
            or current.open_interest is None
            or previous.open_interest <= 0
            else current.open_interest / previous.open_interest - 1
        )
        price_oi_regime, price_oi_score = _price_oi(
            price_return,
            oi_change,
            price_full_scale=self.policy.price_return_full_scale,
            open_interest_full_scale=(
                self.policy.open_interest_change_full_scale
            ),
        )
        basis = (
            None
            if current.oracle_price is None
            else current.mark_price / current.oracle_price - 1
        )
        components: dict[str, float | None] = {
            "price_trend": _scaled_score(
                price_return,
                self.policy.price_return_full_scale,
            ),
            "price_oi": price_oi_score,
            "funding": _funding_score(
                current.funding_rate,
                moderate=self.policy.moderate_funding_abs,
                extreme=self.policy.extreme_funding_abs,
            ),
            "signed_flow": (
                None
                if current.aggressor_imbalance is None
                else 100 * current.aggressor_imbalance
            ),
            "liquidation": _liquidation_score(
                current.long_liquidation_usd,
                current.short_liquidation_usd,
            ),
            "basis": _scaled_score(basis, self.policy.basis_full_scale),
        }
        available_weight = sum(
            _COMPONENT_WEIGHTS[name]
            for name, value in components.items()
            if value is not None
        )
        score = (
            None
            if available_weight < self.policy.minimum_component_weight
            else sum(
                _COMPONENT_WEIGHTS[name] * value
                for name, value in components.items()
                if value is not None
            )
            / available_weight
        )
        return InstrumentSentimentAssessment(
            target_id=binding.target_id,
            scope=binding.scope,
            venue=binding.venue,
            market=binding.market,
            instrument=binding.instrument,
            availability="available" if score is not None else "insufficient_evidence",
            score=score,
            confidence=available_weight if score is not None else 0,
            price_return=price_return,
            open_interest_change=oi_change,
            price_oi_regime=price_oi_regime,
            component_scores=components,
            quality_reasons=(),
            observed_at_utc=current.observed_at_utc,
            provenance=current.provenance,
        )

    def _quality_reasons(
        self,
        binding: ProxyBinding,
        observation: PerpObservation,
        *,
        asof_utc: datetime,
    ) -> tuple[str, ...]:
        reasons: list[str] = []
        age = (asof_utc - observation.observed_at_utc).total_seconds()
        if age < 0:
            reasons.append("future_observation")
        elif age > self.policy.max_age_seconds:
            reasons.append("stale_observation")
        if not observation.active:
            reasons.append("inactive_instrument")
        if (
            binding.min_notional_volume_24h is not None
            and (
                observation.notional_volume_24h is None
                or observation.notional_volume_24h
                < binding.min_notional_volume_24h
            )
        ):
            reasons.append("insufficient_liquidity")
        if observation.oracle_price is not None:
            basis = observation.mark_price / observation.oracle_price - 1
            if abs(basis) > self.policy.max_abs_basis:
                reasons.append("oracle_basis_outlier")
        if observation.bid_price is not None and observation.ask_price is not None:
            midpoint = (observation.bid_price + observation.ask_price) / 2
            spread_bps = (
                observation.ask_price - observation.bid_price
            ) / midpoint * 10_000
            if spread_bps > self.policy.max_spread_bps:
                reasons.append("spread_too_wide")
        return tuple(reasons)

    def _aggregate_targets(
        self,
        instruments: tuple[InstrumentSentimentAssessment, ...],
        *,
        asof_utc: datetime,
    ) -> tuple[TargetSentimentAssessment, ...]:
        grouped: dict[str, list[tuple[ProxyBinding, InstrumentSentimentAssessment]]]
        grouped = defaultdict(list)
        by_identity = {
            (
                item.target_id,
                item.venue,
                item.market,
                item.instrument,
            ): item
            for item in instruments
        }
        for binding in self.bindings:
            grouped[binding.target_id].append(
                (
                    binding,
                    by_identity[
                        (
                            binding.target_id,
                            binding.venue,
                            binding.market,
                            binding.instrument,
                        )
                    ],
                )
            )

        results: list[TargetSentimentAssessment] = []
        for target_id in sorted(grouped):
            pairs = grouped[target_id]
            available = [
                (binding, item)
                for binding, item in pairs
                if item.score is not None
            ]
            configured_weight = sum(binding.weight for binding, _ in pairs)
            source_scores = {
                _source_name(binding): item.score
                for binding, item in pairs
            }
            if not available:
                results.append(
                    TargetSentimentAssessment(
                        target_id=target_id,
                        scope=pairs[0][0].scope,
                        regime=SentimentRegime.UNAVAILABLE,
                        confidence=0,
                        available_sources=0,
                        configured_sources=len(pairs),
                        source_scores=source_scores,
                        asof_utc=asof_utc,
                    )
                )
                continue
            weighted = [
                (
                    float(item.score),
                    binding.weight * item.confidence,
                )
                for binding, item in available
                if item.score is not None
            ]
            score = _weighted_median(weighted)
            total_available_weight = sum(weight for _, weight in weighted)
            disagreement = (
                sum(weight * abs(value - score) for value, weight in weighted)
                / total_available_weight
                / 100
                if total_available_weight > 0
                else 1.0
            )
            agreement = max(0.0, 1.0 - min(disagreement, 1.0))
            coverage = (
                sum(binding.weight * item.confidence for binding, item in available)
                / configured_weight
            )
            confidence = min(max(coverage * agreement, 0.0), 1.0)
            results.append(
                TargetSentimentAssessment(
                    target_id=target_id,
                    scope=pairs[0][0].scope,
                    regime=_regime(score, self.policy),
                    score=score,
                    confidence=confidence,
                    available_sources=len(available),
                    configured_sources=len(pairs),
                    disagreement=min(max(disagreement, 0.0), 1.0),
                    source_scores=source_scores,
                    asof_utc=asof_utc,
                )
            )
        return tuple(results)


def _require_utc(value: datetime, *, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")


def _unique_observations(
    values: tuple[PerpObservation, ...],
    *,
    name: str,
) -> dict[tuple[str, str, str], PerpObservation]:
    result: dict[tuple[str, str, str], PerpObservation] = {}
    for item in values:
        if item.key in result:
            raise ValueError(f"{name} contains duplicate venue instruments")
        result[item.key] = item
    return result


def _scaled_score(value: float | None, full_scale: float) -> float | None:
    if value is None:
        return None
    return min(max(value / full_scale * 100, -100), 100)


def _price_oi(
    price_return: float | None,
    oi_change: float | None,
    *,
    price_full_scale: float,
    open_interest_full_scale: float,
) -> tuple[str, float | None]:
    if price_return is None or oi_change is None:
        return "unavailable", None
    if math.isclose(price_return, 0, abs_tol=1e-12) or math.isclose(
        oi_change,
        0,
        abs_tol=1e-12,
    ):
        return "neutral", 0
    magnitude = math.sqrt(
        min(abs(price_return) / price_full_scale, 1.0)
        * min(abs(oi_change) / open_interest_full_scale, 1.0)
    )
    if price_return > 0 and oi_change > 0:
        return "fresh_longs", 100 * magnitude
    if price_return > 0:
        return "short_covering", 40 * magnitude
    if oi_change > 0:
        return "fresh_shorts", -100 * magnitude
    return "long_deleveraging", -60 * magnitude


def _funding_score(
    value: float | None,
    *,
    moderate: float,
    extreme: float,
) -> float | None:
    if value is None:
        return None
    magnitude = abs(value)
    sign = 1.0 if value >= 0 else -1.0
    if magnitude <= moderate:
        return sign * magnitude / moderate * 100
    if magnitude < extreme:
        remaining = 1 - (magnitude - moderate) / (extreme - moderate)
        return sign * remaining * 100
    return -sign * 50


def _liquidation_score(
    long_liquidation_usd: float | None,
    short_liquidation_usd: float | None,
) -> float | None:
    if long_liquidation_usd is None or short_liquidation_usd is None:
        return None
    total = long_liquidation_usd + short_liquidation_usd
    if total <= 0:
        return 0
    return (
        short_liquidation_usd - long_liquidation_usd
    ) / total * 100


def _weighted_median(values: list[tuple[float, float]]) -> float:
    positive = sorted(
        ((value, weight) for value, weight in values if weight > 0),
        key=lambda item: item[0],
    )
    if not positive:
        raise ValueError("weighted median requires positive weight")
    threshold = sum(weight for _, weight in positive) / 2
    cumulative = 0.0
    for value, weight in positive:
        cumulative += weight
        if cumulative >= threshold:
            return value
    return positive[-1][0]


def _regime(
    score: float,
    policy: CrossAssetSentimentPolicy,
) -> SentimentRegime:
    if score >= policy.risk_on_threshold:
        return SentimentRegime.RISK_ON
    if score <= policy.risk_off_threshold:
        return SentimentRegime.RISK_OFF
    return SentimentRegime.NEUTRAL


def _source_name(binding: ProxyBinding) -> str:
    return f"{binding.venue}:{binding.market}:{binding.instrument}"
