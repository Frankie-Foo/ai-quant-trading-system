"""Deterministic quality-gated sentiment scoring."""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import cast

from .config import AppConfig, BindingConfig
from .models import (
    ComponentEvidence,
    ComponentName,
    PerpObservation,
    Regime,
    Scope,
    SourceAssessment,
    require_utc,
)


@dataclass(frozen=True)
class TargetSignal:
    target_id: str
    scope: Scope
    regime: Regime
    score: float | None
    confidence: float
    coverage: float
    liquidation_coverage: float
    disagreement: float | None
    available_sources: int
    configured_sources: int
    available_venues: int
    venue_conflict: bool
    boost_eligible: bool
    candidate_multiplier: float
    reasons: tuple[str, ...]
    sources: tuple[SourceAssessment, ...]
    asof_utc: datetime


class SentimentEngine:
    def __init__(self, config: AppConfig):
        self._config = config

    def evaluate(
        self,
        *,
        observations: tuple[PerpObservation, ...],
        previous_observations: tuple[PerpObservation, ...],
        asof_utc: datetime,
    ) -> tuple[TargetSignal, ...]:
        require_utc(asof_utc, name="asof_utc")
        current = _unique_observations(observations, "observations")
        previous = _unique_observations(
            previous_observations,
            "previous_observations",
        )
        source_pairs = tuple(
            (
                binding,
                self._assess_source(
                    binding,
                    current.get(binding.observation_key),
                    previous.get(binding.observation_key),
                    asof_utc=asof_utc,
                ),
            )
            for binding in self._config.bindings
        )
        grouped: dict[
            str,
            list[tuple[BindingConfig, SourceAssessment]],
        ] = defaultdict(list)
        for binding, assessment in source_pairs:
            grouped[binding.target_id].append((binding, assessment))
        results: list[TargetSignal] = []
        for target in self._config.targets:
            if not target.enabled:
                results.append(
                    TargetSignal(
                        target_id=target.target_id,
                        scope=Scope.CUSTOM,
                        regime=Regime.UNAVAILABLE,
                        score=None,
                        confidence=0,
                        coverage=0,
                        liquidation_coverage=0,
                        disagreement=None,
                        available_sources=0,
                        configured_sources=0,
                        available_venues=0,
                        venue_conflict=False,
                        boost_eligible=False,
                        candidate_multiplier=(self._config.policy.unavailable_multiplier),
                        reasons=(target.unavailable_reason or "target_disabled",),
                        sources=(),
                        asof_utc=asof_utc,
                    )
                )
                continue
            results.append(
                self._aggregate_target(
                    target.target_id,
                    grouped.get(target.target_id, []),
                    asof_utc=asof_utc,
                )
            )
        return tuple(results)

    def _assess_source(
        self,
        binding: BindingConfig,
        current: PerpObservation | None,
        previous: PerpObservation | None,
        *,
        asof_utc: datetime,
    ) -> SourceAssessment:
        weights = self._config.policy.component_weights.model_dump()
        empty_components = tuple(
            ComponentEvidence(
                name=cast(ComponentName, name),
                weight=weight,
                score=None,
                provenance="N/A",
            )
            for name, weight in weights.items()
        )
        source_id = _source_id(binding)
        if current is None:
            return SourceAssessment(
                source_id=source_id,
                target_id=binding.target_id,
                scope=binding.scope,
                venue=binding.venue,
                market=binding.market,
                instrument=binding.instrument,
                availability="missing_observation",
                confidence=0,
                price_oi_regime="unavailable",
                components=empty_components,
                quality_reasons=("missing_observation",),
                provenance="N/A",
            )
        quality_reasons = self._quality_reasons(
            binding,
            current,
            asof_utc=asof_utc,
        )
        if quality_reasons:
            return SourceAssessment(
                source_id=source_id,
                target_id=binding.target_id,
                scope=binding.scope,
                venue=binding.venue,
                market=binding.market,
                instrument=binding.instrument,
                availability="quality_rejected",
                confidence=0,
                price_oi_regime="unavailable",
                components=tuple(
                    item.model_copy(update={"provenance": f"current:{current.provenance}"})
                    for item in empty_components
                ),
                quality_reasons=quality_reasons,
                observed_at_utc=current.observed_at_utc,
                provenance=current.provenance,
            )
        usable_previous, evidence_warnings = self._causal_previous(
            current,
            previous,
        )
        price_return = (
            None if usable_previous is None else current.mark_price / usable_previous.mark_price - 1
        )
        oi_change = (
            None
            if usable_previous is None
            or usable_previous.open_interest is None
            or current.open_interest is None
            or usable_previous.open_interest <= 0
            else current.open_interest / usable_previous.open_interest - 1
        )
        price_oi_regime, price_oi_score = _price_oi(
            price_return,
            oi_change,
            price_scale=self._config.policy.price_return_full_scale,
            oi_scale=self._config.policy.open_interest_change_full_scale,
        )
        basis = (
            None if current.oracle_price is None else current.mark_price / current.oracle_price - 1
        )
        raw_scores: dict[str, float | None] = {
            "price_trend": _scaled_score(
                price_return,
                self._config.policy.price_return_full_scale,
            ),
            "price_oi": price_oi_score,
            "funding": _funding_score(
                current.funding_rate,
                moderate=self._config.policy.moderate_funding_abs,
                extreme=self._config.policy.extreme_funding_abs,
            ),
            "signed_flow": (
                None if current.aggressor_imbalance is None else 100 * current.aggressor_imbalance
            ),
            "liquidation": _liquidation_score(
                current.long_liquidation_usd,
                current.short_liquidation_usd,
            ),
            "basis": _scaled_score(
                basis,
                self._config.policy.basis_full_scale,
            ),
        }
        scores = {
            name: (None if score is None else score * binding.polarity)
            for name, score in raw_scores.items()
        }
        current_source = f"current:{current.provenance}"
        prior_source = (
            "previous:unavailable"
            if usable_previous is None
            else f"previous:{usable_previous.provenance}"
        )
        provenance = {
            "price_trend": f"{current_source}|{prior_source}",
            "price_oi": f"{current_source}|{prior_source}",
            "funding": current_source,
            "signed_flow": current_source,
            "liquidation": current_source,
            "basis": current_source,
        }
        components = tuple(
            ComponentEvidence(
                name=cast(ComponentName, name),
                weight=weight,
                score=scores[name],
                provenance=provenance[name],
            )
            for name, weight in weights.items()
        )
        available_weight = sum(item.weight for item in components if item.score is not None)
        score = (
            None
            if available_weight < self._config.policy.minimum_component_weight
            else sum(item.weight * item.score for item in components if item.score is not None)
            / available_weight
        )
        return SourceAssessment(
            source_id=source_id,
            target_id=binding.target_id,
            scope=binding.scope,
            venue=binding.venue,
            market=binding.market,
            instrument=binding.instrument,
            availability=("available" if score is not None else "insufficient_evidence"),
            score=score,
            confidence=available_weight if score is not None else 0,
            price_oi_regime=price_oi_regime,
            components=components,
            evidence_warnings=evidence_warnings,
            observed_at_utc=current.observed_at_utc,
            previous_observed_at_utc=(
                None if usable_previous is None else usable_previous.observed_at_utc
            ),
            provenance=current.provenance,
        )

    def _quality_reasons(
        self,
        binding: BindingConfig,
        observation: PerpObservation,
        *,
        asof_utc: datetime,
    ) -> tuple[str, ...]:
        reasons: list[str] = []
        age = (asof_utc - observation.observed_at_utc).total_seconds()
        if age < 0:
            reasons.append("future_observation")
        elif age > self._config.collection.max_age_seconds:
            reasons.append("stale_observation")
        if not observation.active:
            reasons.append("inactive_instrument")
        if binding.min_notional_volume_24h is not None and (
            observation.notional_volume_24h is None
            or observation.notional_volume_24h < binding.min_notional_volume_24h
        ):
            reasons.append("insufficient_liquidity")
        if observation.bid_price is None or observation.ask_price is None:
            reasons.append("missing_bbo")
        else:
            midpoint = (observation.bid_price + observation.ask_price) / 2
            spread_bps = (observation.ask_price - observation.bid_price) / midpoint * 10_000
            maximum = binding.max_spread_bps or self._config.policy.default_max_spread_bps
            if spread_bps > maximum:
                reasons.append("spread_too_wide")
        if observation.oracle_price is not None:
            basis = observation.mark_price / observation.oracle_price - 1
            if abs(basis) > self._config.policy.max_abs_basis:
                reasons.append("oracle_basis_outlier")
        return tuple(reasons)

    def _causal_previous(
        self,
        current: PerpObservation,
        previous: PerpObservation | None,
    ) -> tuple[PerpObservation | None, tuple[str, ...]]:
        if previous is None:
            return None, ()
        if previous.observed_at_utc >= current.observed_at_utc:
            return None, ("non_causal_previous_observation",)
        gap = (current.observed_at_utc - previous.observed_at_utc).total_seconds()
        if gap > self._config.collection.max_previous_gap_seconds:
            return None, ("stale_previous_observation",)
        return previous, ()

    def _aggregate_target(
        self,
        target_id: str,
        pairs: list[tuple[BindingConfig, SourceAssessment]],
        *,
        asof_utc: datetime,
    ) -> TargetSignal:
        if not pairs:
            return TargetSignal(
                target_id=target_id,
                scope=Scope.CUSTOM,
                regime=Regime.UNAVAILABLE,
                score=None,
                confidence=0,
                coverage=0,
                liquidation_coverage=0,
                disagreement=None,
                available_sources=0,
                configured_sources=0,
                available_venues=0,
                venue_conflict=False,
                boost_eligible=False,
                candidate_multiplier=self._config.policy.unavailable_multiplier,
                reasons=("missing_bindings",),
                sources=(),
                asof_utc=asof_utc,
            )
        available = [(binding, item) for binding, item in pairs if item.score is not None]
        configured_weight = sum(binding.weight for binding, _ in pairs)
        scope = pairs[0][0].scope
        if not available:
            return TargetSignal(
                target_id=target_id,
                scope=scope,
                regime=Regime.UNAVAILABLE,
                score=None,
                confidence=0,
                coverage=0,
                liquidation_coverage=0,
                disagreement=None,
                available_sources=0,
                configured_sources=len(pairs),
                available_venues=0,
                venue_conflict=False,
                boost_eligible=False,
                candidate_multiplier=self._config.policy.unavailable_multiplier,
                reasons=("no_available_sources",),
                sources=tuple(item for _, item in pairs),
                asof_utc=asof_utc,
            )
        weighted = [
            (float(item.score), binding.weight * item.confidence)
            for binding, item in available
            if item.score is not None
        ]
        score = _weighted_median(weighted)
        total_effective = sum(weight for _, weight in weighted)
        disagreement = (
            1.0
            if total_effective <= 0
            else min(
                sum(weight * abs(value - score) for value, weight in weighted)
                / total_effective
                / 100,
                1.0,
            )
        )
        coverage = min(
            sum(binding.weight * item.confidence for binding, item in available)
            / configured_weight,
            1.0,
        )
        liquidation_coverage = min(
            sum(
                binding.weight
                for binding, item in available
                if _component_score(item, "liquidation") is not None
            )
            / configured_weight,
            1.0,
        )
        venue_scores = _venue_scores(available)
        venue_conflict = any(
            value >= self._config.policy.risk_on_threshold for value in venue_scores.values()
        ) and any(
            value <= self._config.policy.risk_off_threshold for value in venue_scores.values()
        )
        confidence = min(max(coverage * (1 - disagreement), 0.0), 1.0)
        reasons: list[str] = []
        regime: Regime
        candidate: float
        if coverage < self._config.policy.minimum_target_coverage:
            regime = Regime.UNAVAILABLE
            candidate = self._config.policy.unavailable_multiplier
            reasons.append("coverage_below_minimum")
        elif disagreement >= self._config.policy.conflict_disagreement_threshold:
            regime = Regime.CONFLICTED
            candidate = self._config.policy.unavailable_multiplier
            reasons.append("high_source_disagreement")
        elif score <= self._config.policy.strong_risk_off_threshold:
            regime = Regime.RISK_OFF
            candidate = 0.0
            reasons.append("strong_risk_off")
        elif score <= self._config.policy.risk_off_threshold:
            regime = Regime.RISK_OFF
            candidate = self._config.policy.risk_off_multiplier
            reasons.append("risk_off")
        elif score >= self._config.policy.risk_on_threshold:
            regime = Regime.RISK_ON
            candidate = self._config.policy.neutral_multiplier
        else:
            regime = Regime.NEUTRAL
            candidate = self._config.policy.neutral_multiplier
        boost_eligible = False
        if score >= self._config.policy.strong_risk_on_threshold:
            boost_reasons = self._boost_blockers(
                pairs=pairs,
                coverage=coverage,
                liquidation_coverage=liquidation_coverage,
                venue_scores=venue_scores,
                venue_conflict=venue_conflict,
            )
            if not boost_reasons and regime is Regime.RISK_ON:
                boost_eligible = True
                candidate = self._config.policy.boost_multiplier
                reasons.append("strong_risk_on_boost_candidate")
            else:
                reasons.extend(boost_reasons)
        if venue_conflict:
            reasons.append("venue_direction_conflict")
            boost_eligible = False
            candidate = min(candidate, self._config.policy.neutral_multiplier)
        return TargetSignal(
            target_id=target_id,
            scope=scope,
            regime=regime,
            score=score,
            confidence=confidence,
            coverage=coverage,
            liquidation_coverage=liquidation_coverage,
            disagreement=disagreement,
            available_sources=len(available),
            configured_sources=len(pairs),
            available_venues=len(venue_scores),
            venue_conflict=venue_conflict,
            boost_eligible=boost_eligible,
            candidate_multiplier=candidate,
            reasons=tuple(dict.fromkeys(reasons)),
            sources=tuple(item for _, item in pairs),
            asof_utc=asof_utc,
        )

    def _boost_blockers(
        self,
        *,
        pairs: list[tuple[BindingConfig, SourceAssessment]],
        coverage: float,
        liquidation_coverage: float,
        venue_scores: dict[str, float],
        venue_conflict: bool,
    ) -> tuple[str, ...]:
        reasons: list[str] = []
        if coverage < self._config.policy.boost_minimum_coverage:
            reasons.append("boost_blocked_low_coverage")
        if venue_conflict:
            reasons.append("boost_blocked_venue_conflict")
        if self._config.policy.require_two_venues_for_boost and len(venue_scores) < 2:
            reasons.append("boost_blocked_single_venue")
        if not all(binding.boost_eligible for binding, _ in pairs):
            reasons.append("boost_blocked_binding_policy")
        if (
            self._config.policy.require_liquidation_for_boost
            and liquidation_coverage < self._config.policy.minimum_liquidation_coverage_for_boost
        ):
            reasons.append("boost_blocked_missing_liquidation")
        return tuple(reasons)


def _unique_observations(
    values: tuple[PerpObservation, ...],
    name: str,
) -> dict[tuple[str, str, str], PerpObservation]:
    result: dict[tuple[str, str, str], PerpObservation] = {}
    for value in values:
        if value.key in result:
            raise ValueError(f"{name} contains duplicate observations")
        result[value.key] = value
    return result


def _source_id(binding: BindingConfig) -> str:
    return f"{binding.venue}:{binding.market}:{binding.instrument}"


def _scaled_score(value: float | None, full_scale: float) -> float | None:
    if value is None:
        return None
    return min(max(value / full_scale * 100, -100), 100)


def _price_oi(
    price_return: float | None,
    oi_change: float | None,
    *,
    price_scale: float,
    oi_scale: float,
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
        min(abs(price_return) / price_scale, 1) * min(abs(oi_change) / oi_scale, 1)
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
    long_liquidation: float | None,
    short_liquidation: float | None,
) -> float | None:
    if long_liquidation is None or short_liquidation is None:
        return None
    total = long_liquidation + short_liquidation
    if total <= 0:
        return 0
    return (short_liquidation - long_liquidation) / total * 100


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


def _component_score(
    source: SourceAssessment,
    name: str,
) -> float | None:
    for component in source.components:
        if component.name == name:
            return component.score
    raise ValueError(f"unknown component: {name}")


def _venue_scores(
    available: list[tuple[BindingConfig, SourceAssessment]],
) -> dict[str, float]:
    grouped: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for binding, source in available:
        if source.score is not None:
            grouped[binding.venue].append((source.score, binding.weight * source.confidence))
    return {venue: _weighted_median(weighted) for venue, weighted in grouped.items()}
