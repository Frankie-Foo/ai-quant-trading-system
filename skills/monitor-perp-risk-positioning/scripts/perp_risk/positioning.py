"""Asymmetric hysteresis and multi-target position overlay."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal

from . import __version__
from .config import PolicyConfig
from .engine import TargetSignal
from .models import (
    PositionRecommendation,
    Regime,
    RiskSnapshot,
    TargetAssessment,
)


@dataclass(frozen=True)
class PositionState:
    target_id: str
    effective_multiplier: float
    pending_multiplier: float
    pending_windows: int
    last_window_id: int


class PositionResolver:
    def __init__(self, policy: PolicyConfig, *, window_seconds: int):
        self._policy = policy
        self._window_seconds = window_seconds

    def resolve(
        self,
        signal: TargetSignal,
        prior: PositionState | None,
    ) -> tuple[TargetAssessment, PositionState]:
        window_id = int(signal.asof_utc.timestamp()) // self._window_seconds
        candidate = signal.candidate_multiplier
        effective, pending, count = self._transition(
            signal=signal,
            prior=prior,
            candidate=candidate,
            window_id=window_id,
        )
        state = PositionState(
            target_id=signal.target_id,
            effective_multiplier=effective,
            pending_multiplier=pending,
            pending_windows=count,
            last_window_id=window_id,
        )
        reasons = list(signal.reasons)
        if count > 0 and effective != candidate:
            reasons.append(
                f"confirmation_pending:{count}/{self._policy.confirmation_windows}"
            )
        return (
            TargetAssessment(
                target_id=signal.target_id,
                scope=signal.scope,
                regime=signal.regime,
                score=signal.score,
                confidence=signal.confidence,
                coverage=signal.coverage,
                liquidation_coverage=signal.liquidation_coverage,
                disagreement=signal.disagreement,
                available_sources=signal.available_sources,
                configured_sources=signal.configured_sources,
                available_venues=signal.available_venues,
                venue_conflict=signal.venue_conflict,
                boost_eligible=signal.boost_eligible,
                candidate_multiplier=candidate,
                effective_multiplier=effective,
                pending_windows=count,
                confirmation_windows=self._policy.confirmation_windows,
                reasons=tuple(reasons),
                sources=signal.sources,
                asof_utc=signal.asof_utc,
            ),
            state,
        )

    def _transition(
        self,
        *,
        signal: TargetSignal,
        prior: PositionState | None,
        candidate: float,
        window_id: int,
    ) -> tuple[float, float, int]:
        immediate = candidate == 0 or signal.regime in {Regime.UNAVAILABLE, Regime.CONFLICTED}
        if prior is None:
            if immediate:
                return candidate, candidate, 0
            if candidate in {
                self._policy.risk_off_multiplier,
                self._policy.boost_multiplier,
            }:
                return (
                    self._policy.neutral_multiplier,
                    candidate,
                    1,
                )
            return candidate, candidate, 0
        if candidate == prior.effective_multiplier:
            return candidate, candidate, 0
        if immediate:
            return candidate, candidate, 0
        same_window = window_id == prior.last_window_id
        if candidate == prior.pending_multiplier:
            count = prior.pending_windows + (0 if same_window else 1)
        else:
            count = 1
        if count >= self._policy.confirmation_windows:
            return candidate, candidate, 0
        return prior.effective_multiplier, candidate, count


def recommend_position(
    snapshot: RiskSnapshot,
    *,
    relevant_targets: tuple[str, ...],
    base_target_position_pct: float | None = None,
) -> PositionRecommendation:
    requested = tuple(dict.fromkeys(item.strip().lower() for item in relevant_targets))
    if not requested:
        raise ValueError("at least one relevant target is required")
    by_target = {item.target_id: item for item in snapshot.targets}
    missing = [item for item in requested if item not in by_target]
    if missing:
        raise ValueError(f"unknown targets: {','.join(missing)}")
    selected = [by_target[item] for item in requested]
    defensive = [item.effective_multiplier for item in selected if item.effective_multiplier < 1]
    if defensive:
        multiplier = min(defensive)
    elif any(item.effective_multiplier > 1 for item in selected):
        multiplier = max(item.effective_multiplier for item in selected)
    else:
        multiplier = 1.0
    adjusted = (
        None
        if base_target_position_pct is None
        else min(base_target_position_pct * multiplier, 100.0)
    )
    action = _action(multiplier, actionable=snapshot.actionable)
    reasons = tuple(
        dict.fromkeys(
            [f"{item.target_id}:{item.regime.value}" for item in selected]
            + [f"{item.target_id}:{reason}" for item in selected for reason in item.reasons]
        )
    )
    return PositionRecommendation(
        skill_version=__version__,
        recommendation_id=f"rec_{uuid.uuid4().hex}",
        snapshot_id=snapshot.snapshot_id,
        asof_utc=snapshot.asof_utc,
        relevant_targets=requested,
        position_multiplier=multiplier,
        action=action,
        actionable=snapshot.actionable,
        base_target_position_pct=base_target_position_pct,
        adjusted_target_position_pct=adjusted,
        reasons=reasons,
        production_eligible=False,
        execution_eligible=False,
        orders_submitted=0,
    )


def _action(
    multiplier: float,
    *,
    actionable: bool,
) -> Literal["cash", "reduce", "hold", "increase", "research_only"]:
    if not actionable:
        return "research_only"
    if multiplier == 0:
        return "cash"
    if multiplier < 1:
        return "reduce"
    if multiplier > 1:
        return "increase"
    return "hold"
