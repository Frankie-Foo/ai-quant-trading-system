"""Deterministic admission gate for agent-generated research proposals."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from research.postmortem_agents import CriticReview, CriticVerdict, ResearchReview


class EvolutionStatus(StrEnum):
    NO_ACTIONABLE_HYPOTHESIS = "no_actionable_hypothesis"
    REJECTED_BY_CRITIC = "rejected_by_critic"
    REVISION_REQUIRED = "revision_required"
    WAITING_FOR_MINIMUM_EPISODES = "waiting_for_minimum_episodes"
    ELIGIBLE_FOR_SANDBOX_EXPERIMENT = "eligible_for_sandbox_experiment"


class EvolutionDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: EvolutionStatus
    episode_count: int
    minimum_episode_count: int
    labeled_trade_count: int
    minimum_labeled_trade_count: int
    hypothesis_count: int
    approved_for_production: bool = False
    reasons: tuple[str, ...]


def evaluate_proposal(
    review: ResearchReview,
    critic: CriticReview,
    *,
    episode_count: int,
    labeled_trade_count: int,
    min_episodes: int = 20,
    min_labeled_trades: int = 20,
) -> EvolutionDecision:
    """Permit only a sandbox experiment; agents can never promote themselves."""
    if (
        episode_count < 0
        or labeled_trade_count < 0
        or min_episodes <= 0
        or min_labeled_trades <= 0
    ):
        raise ValueError("episode counts are invalid")
    def decision(
        status: EvolutionStatus, reasons: tuple[str, ...]
    ) -> EvolutionDecision:
        return EvolutionDecision(
            status=status,
            episode_count=episode_count,
            minimum_episode_count=min_episodes,
            labeled_trade_count=labeled_trade_count,
            minimum_labeled_trade_count=min_labeled_trades,
            hypothesis_count=len(review.hypotheses),
            approved_for_production=False,
            reasons=reasons,
        )

    if not review.hypotheses:
        return decision(
            EvolutionStatus.NO_ACTIONABLE_HYPOTHESIS,
            ("research agent found no falsifiable hypothesis",),
        )
    if critic.verdict is CriticVerdict.REJECT:
        return decision(
            EvolutionStatus.REJECTED_BY_CRITIC,
            (critic.rationale,),
        )
    if critic.verdict is CriticVerdict.REVISE:
        return decision(
            EvolutionStatus.REVISION_REQUIRED,
            (critic.rationale, *critic.required_evidence),
        )
    if episode_count < min_episodes or labeled_trade_count < min_labeled_trades:
        return decision(
            EvolutionStatus.WAITING_FOR_MINIMUM_EPISODES,
            (
                f"requires at least {min_episodes} completed trading episodes and "
                f"{min_labeled_trades} uncensored trade labels",
            ),
        )
    return decision(
        EvolutionStatus.ELIGIBLE_FOR_SANDBOX_EXPERIMENT,
        ("critic admitted proposal for deterministic OOS testing only",),
    )
