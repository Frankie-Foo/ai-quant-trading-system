"""Deterministic postmarket diagnostics and sandbox experiment admission."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Literal, cast

import polars as pl
from pydantic import BaseModel, ConfigDict, Field

PROGRAM_REVIEW_VERSION: Literal["postmarket_program_review.v1"] = (
    "postmarket_program_review.v1"
)
LABELED_OUTCOMES = ("tp", "sl", "time")


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProgramReviewStatus(StrEnum):
    BLOCKED_DATA_QUALITY = "blocked_data_quality"
    WAITING_FOR_SAMPLES = "waiting_for_samples"
    ELIGIBLE_FOR_SANDBOX_EXPERIMENT = "eligible_for_sandbox_experiment"


class FindingSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    BLOCKER = "blocker"


class FindingCode(StrEnum):
    INCOMPLETE_TRIGGER_PATHS = "incomplete_trigger_paths"
    NET_COSTS_UNAVAILABLE = "net_costs_unavailable"
    INSUFFICIENT_EPISODES = "insufficient_episodes"
    INSUFFICIENT_LABELED_TRADES = "insufficient_labeled_trades"
    INSUFFICIENT_NET_LABELED_TRADES = "insufficient_net_labeled_trades"
    NO_SIGNAL_TRIGGERS = "no_signal_triggers"


class ReviewPolicy(FrozenModel):
    minimum_episodes: int = Field(default=20, gt=0)
    minimum_labeled_trades: int = Field(default=20, gt=0)
    minimum_net_labeled_trades: int = Field(default=20, gt=0)


class ProgramFinding(FrozenModel):
    code: FindingCode
    severity: FindingSeverity
    observed: str = Field(min_length=1, max_length=500)
    expected: str = Field(min_length=1, max_length=500)
    action: str = Field(min_length=1, max_length=800)
    affected_symbols: tuple[str, ...] = Field(default=(), max_length=100)


class ReviewMetrics(FrozenModel):
    episode_count: int = Field(ge=0)
    candidate_count: int = Field(ge=0)
    current_candidate_count: int = Field(ge=0)
    current_trigger_count: int = Field(ge=0)
    triggered_trade_count: int = Field(ge=0)
    labeled_trade_count: int = Field(ge=0)
    net_labeled_trade_count: int = Field(ge=0)
    censored_trigger_count: int = Field(ge=0)
    tp_count: int = Field(ge=0)
    sl_count: int = Field(ge=0)
    time_count: int = Field(ge=0)
    positive_gross_rate: float | None = Field(default=None, ge=0, le=1)
    mean_gross_return: float | None = None
    mean_net_return: float | None = None


class SandboxExperiment(FrozenModel):
    experiment_id: Literal["rvol_threshold_sensitivity.v1"]
    target_component: Literal["rvol_gate"]
    parameter_name: Literal["min_rvol"]
    baseline: float
    challengers: tuple[float, ...]
    evaluation: Literal["purged_walk_forward"]
    objective: Literal["mean_net_return_after_costs"]
    attempted_configurations: int = Field(ge=1)
    production_eligible: Literal[False] = False


class ProgramReview(FrozenModel):
    review_version: Literal["postmarket_program_review.v1"]
    status: ProgramReviewStatus
    policy: ReviewPolicy
    metrics: ReviewMetrics
    findings: tuple[ProgramFinding, ...]
    sandbox_experiments: tuple[SandboxExperiment, ...]
    llm_research_allowed: bool
    approved_for_production: Literal[False] = False
    provenance: str = Field(min_length=1)

    def content_sha256(self) -> str:
        payload = self.model_dump(mode="json")
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


def _validate_episode(frame: pl.DataFrame, *, name: str) -> None:
    required = {
        "symbol",
        "session_date",
        "signal_triggered",
        "outcome_label",
        "outcome_status",
        "gross_return",
        "net_return",
        "net_return_status",
    }
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"{name} missing required columns: {sorted(missing)}")
    if frame.is_empty():
        raise ValueError(f"{name} cannot be empty")


def _float_mean(values: pl.Series) -> float | None:
    clean = values.drop_nulls()
    if clean.is_empty():
        return None
    numeric = cast(list[float], clean.cast(pl.Float64).to_list())
    return sum(numeric) / len(numeric)


def build_program_review(
    *,
    current_episode: pl.DataFrame,
    episode_history: pl.DataFrame,
    policy: ReviewPolicy | None = None,
) -> ProgramReview:
    """Create an auditable review without any model or external service call."""
    policy = policy or ReviewPolicy()
    _validate_episode(current_episode, name="current_episode")
    _validate_episode(episode_history, name="episode_history")
    current_dates = current_episode.get_column("session_date").unique().to_list()
    if len(current_dates) != 1:
        raise ValueError("current_episode must contain exactly one session date")
    history_dates = set(episode_history.get_column("session_date").unique().to_list())
    if current_dates[0] not in history_dates:
        raise ValueError("current_episode is not represented in episode_history")
    if any(value > current_dates[0] for value in history_dates):
        raise ValueError("episode_history contains a future session")

    labeled = episode_history.filter(pl.col("outcome_label").is_in(LABELED_OUTCOMES))
    net_labeled = labeled.filter(pl.col("net_return").is_not_null())
    triggered = episode_history.filter(pl.col("signal_triggered"))
    censored = triggered.filter(~pl.col("outcome_label").is_in(LABELED_OUTCOMES))
    current_triggered = current_episode.filter(pl.col("signal_triggered"))
    current_censored = current_triggered.filter(
        ~pl.col("outcome_label").is_in(LABELED_OUTCOMES)
    )
    gross = labeled.get_column("gross_return").drop_nulls()
    positive_gross_rate = (
        float((gross > 0).sum() / gross.len()) if not gross.is_empty() else None
    )
    metrics = ReviewMetrics(
        episode_count=len(history_dates),
        candidate_count=episode_history.height,
        current_candidate_count=current_episode.height,
        current_trigger_count=current_triggered.height,
        triggered_trade_count=triggered.height,
        labeled_trade_count=labeled.height,
        net_labeled_trade_count=net_labeled.height,
        censored_trigger_count=censored.height,
        tp_count=labeled.filter(pl.col("outcome_label") == "tp").height,
        sl_count=labeled.filter(pl.col("outcome_label") == "sl").height,
        time_count=labeled.filter(pl.col("outcome_label") == "time").height,
        positive_gross_rate=positive_gross_rate,
        mean_gross_return=_float_mean(labeled.get_column("gross_return")),
        mean_net_return=_float_mean(net_labeled.get_column("net_return")),
    )

    findings: list[ProgramFinding] = []
    current_censored_symbols = tuple(
        sorted(str(value) for value in current_censored.get_column("symbol").unique())
    )
    if current_censored_symbols:
        findings.append(
            ProgramFinding(
                code=FindingCode.INCOMPLETE_TRIGGER_PATHS,
                severity=FindingSeverity.BLOCKER,
                observed=(
                    f"{len(current_censored_symbols)} triggered symbols have censored "
                    "outcomes in the current Episode"
                ),
                expected="every triggered symbol has a continuous path to an exit barrier",
                action="repair upstream minute-bar coverage; do not interpolate missing bars",
                affected_symbols=current_censored_symbols,
            )
        )
    if metrics.labeled_trade_count > metrics.net_labeled_trade_count:
        missing_net = labeled.filter(pl.col("net_return").is_null())
        affected = tuple(
            sorted(str(value) for value in missing_net.get_column("symbol").unique())[:100]
        )
        findings.append(
            ProgramFinding(
                code=FindingCode.NET_COSTS_UNAVAILABLE,
                severity=FindingSeverity.BLOCKER,
                observed=(
                    f"{metrics.labeled_trade_count - metrics.net_labeled_trade_count} "
                    "labeled trades lack net returns"
                ),
                expected="all experiment labels include conservative point-in-time costs",
                action="capture quote spread and compute net returns before strategy comparison",
                affected_symbols=affected,
            )
        )
    if metrics.episode_count < policy.minimum_episodes:
        findings.append(
            ProgramFinding(
                code=FindingCode.INSUFFICIENT_EPISODES,
                severity=FindingSeverity.WARNING,
                observed=f"{metrics.episode_count} distinct Episodes",
                expected=f"at least {policy.minimum_episodes} distinct Episodes",
                action="continue immutable daily Episode capture",
            )
        )
    if metrics.labeled_trade_count < policy.minimum_labeled_trades:
        findings.append(
            ProgramFinding(
                code=FindingCode.INSUFFICIENT_LABELED_TRADES,
                severity=FindingSeverity.WARNING,
                observed=f"{metrics.labeled_trade_count} uncensored trade labels",
                expected=f"at least {policy.minimum_labeled_trades} labels",
                action="continue chronological collection; do not synthesize outcomes",
            )
        )
    if metrics.net_labeled_trade_count < policy.minimum_net_labeled_trades:
        findings.append(
            ProgramFinding(
                code=FindingCode.INSUFFICIENT_NET_LABELED_TRADES,
                severity=FindingSeverity.WARNING,
                observed=f"{metrics.net_labeled_trade_count} net trade labels",
                expected=f"at least {policy.minimum_net_labeled_trades} net labels",
                action="collect cost-complete labels before any performance experiment",
            )
        )
    if current_triggered.is_empty():
        findings.append(
            ProgramFinding(
                code=FindingCode.NO_SIGNAL_TRIGGERS,
                severity=FindingSeverity.INFO,
                observed="0 triggered signals in the current Episode",
                expected="informational; zero is a valid deterministic outcome",
                action="no action",
            )
        )

    blocked = any(item.severity is FindingSeverity.BLOCKER for item in findings)
    enough_samples = (
        metrics.episode_count >= policy.minimum_episodes
        and metrics.labeled_trade_count >= policy.minimum_labeled_trades
        and metrics.net_labeled_trade_count >= policy.minimum_net_labeled_trades
    )
    if blocked:
        status = ProgramReviewStatus.BLOCKED_DATA_QUALITY
    elif not enough_samples:
        status = ProgramReviewStatus.WAITING_FOR_SAMPLES
    else:
        status = ProgramReviewStatus.ELIGIBLE_FOR_SANDBOX_EXPERIMENT

    experiments: tuple[SandboxExperiment, ...] = ()
    if status is ProgramReviewStatus.ELIGIBLE_FOR_SANDBOX_EXPERIMENT:
        experiments = (
            SandboxExperiment(
                experiment_id="rvol_threshold_sensitivity.v1",
                target_component="rvol_gate",
                parameter_name="min_rvol",
                baseline=3.0,
                challengers=(3.5, 4.0, 5.0),
                evaluation="purged_walk_forward",
                objective="mean_net_return_after_costs",
                attempted_configurations=3,
                production_eligible=False,
            ),
        )

    return ProgramReview(
        review_version=PROGRAM_REVIEW_VERSION,
        status=status,
        policy=policy,
        metrics=metrics,
        findings=tuple(findings),
        sandbox_experiments=experiments,
        llm_research_allowed=(
            status is ProgramReviewStatus.ELIGIBLE_FOR_SANDBOX_EXPERIMENT
        ),
        approved_for_production=False,
        provenance=f"research.program_review:{PROGRAM_REVIEW_VERSION}",
    )
