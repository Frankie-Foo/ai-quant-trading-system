from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Literal, Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SHA256_PATTERN = r"^[0-9a-f]{64}$"


class RunStatus(StrEnum):
    REGISTERED = "registered"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ResearchSplit(BaseModel):
    """Chronological train/validation/test split with no overlapping windows."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    train_start: date
    train_end: date
    validation_start: date
    validation_end: date
    test_start: date
    test_end: date

    @model_validator(mode="after")
    def enforce_chronology(self) -> Self:
        if not (
            self.train_start <= self.train_end
            < self.validation_start
            <= self.validation_end
            < self.test_start
            <= self.test_end
        ):
            raise ValueError("research windows must be ordered and non-overlapping")
        return self


class ResearchRun(BaseModel):
    """Reproducible registry record for one research or model experiment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID = Field(default_factory=uuid4)
    created_at_utc: datetime
    data_snapshot_ids: tuple[str, ...] = Field(min_length=1)
    feature_set_sha256: str = Field(pattern=SHA256_PATTERN)
    config_sha256: str = Field(pattern=SHA256_PATTERN)
    code_sha256: str = Field(pattern=SHA256_PATTERN)
    random_seed: int
    attempted_configurations: int = Field(ge=1)
    split: ResearchSplit
    status: RunStatus = RunStatus.REGISTERED

    @field_validator("created_at_utc")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at_utc must be timezone-aware")
        if value.utcoffset() != timedelta(0):
            raise ValueError("created_at_utc must be stored in UTC")
        return value

    def manifest_sha256(self) -> str:
        payload = self.model_dump(mode="json")
        encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class ExperimentStage(StrEnum):
    INVALID_EVIDENCE = "invalid_evidence"
    WAITING_FOR_EVIDENCE = "waiting_for_evidence"
    REJECTED = "rejected"
    ELIGIBLE_FOR_PAPER = "eligible_for_paper"
    ELIGIBLE_FOR_HUMAN_REVIEW = "eligible_for_human_review"


class ScientificHypothesis(BaseModel):
    """One falsifiable strategy claim; never an executable trading instruction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    hypothesis_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,99}$")
    statement: str = Field(min_length=10, max_length=2000)
    mechanism: str = Field(min_length=10, max_length=2000)
    falsification: str = Field(min_length=10, max_length=2000)
    changed_variable: str = Field(min_length=2, max_length=100)
    control: str = Field(min_length=3, max_length=500)
    validation_plan: str = Field(min_length=10, max_length=3000)
    evidence_ids: tuple[str, ...] = Field(min_length=1, max_length=100)
    production_eligible: Literal[False] = False


class PerformanceEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    trades: int = Field(ge=0)
    win_rate: float | None = Field(default=None, ge=0, le=1)
    average_win_loss: float | None = Field(default=None, ge=0)
    profit_factor: float | None = Field(default=None, ge=0)
    expectancy: float | None = None
    expectancy_ci95: tuple[float | None, float | None] = (None, None)


class ExperimentEvidence(BaseModel):
    """Frozen evidence package for deterministic AI4S admission."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    hypothesis: ScientificHypothesis
    full: PerformanceEvidence
    blind: PerformanceEvidence
    attempted_configurations: int = Field(ge=1)
    blind_evaluations: int = Field(ge=0)
    point_in_time: bool
    quote_aware_costs: bool
    critical_quality_passed: bool
    paper_trading_days: int = Field(default=0, ge=0)
    production_eligible: Literal[False] = False


class ExperimentDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    hypothesis_id: str
    stage: ExperimentStage
    reasons: tuple[str, ...]
    production_eligible: Literal[False] = False


def evaluate_experiment(evidence: ExperimentEvidence) -> ExperimentDecision:
    """Falsify first; at most admit evidence to Paper or human review."""

    def decision(stage: ExperimentStage, *reasons: str) -> ExperimentDecision:
        return ExperimentDecision(
            hypothesis_id=evidence.hypothesis.hypothesis_id,
            stage=stage,
            reasons=reasons,
        )

    if not (
        evidence.point_in_time
        and evidence.quote_aware_costs
        and evidence.critical_quality_passed
        and evidence.blind_evaluations == 1
    ):
        return decision(
            ExperimentStage.INVALID_EVIDENCE,
            "requires point-in-time data, full costs, passed quality checks, "
            "and one blind evaluation",
        )
    if evidence.full.trades < 100 or evidence.blind.trades < 30:
        return decision(
            ExperimentStage.WAITING_FOR_EVIDENCE,
            "requires at least one hundred full-sample and thirty blind trades",
        )
    full = evidence.full
    blind = evidence.blind
    required = (
        full.expectancy,
        full.profit_factor,
        blind.expectancy,
        blind.profit_factor,
        blind.average_win_loss,
        blind.expectancy_ci95[0],
    )
    if any(value is None for value in required):
        return decision(ExperimentStage.INVALID_EVIDENCE, "required metrics are unavailable")
    assert full.expectancy is not None
    assert full.profit_factor is not None
    assert blind.expectancy is not None
    assert blind.profit_factor is not None
    assert blind.average_win_loss is not None
    assert blind.expectancy_ci95[0] is not None
    if not (
        full.expectancy > 0
        and full.profit_factor >= 1
        and blind.expectancy > 0
        and blind.profit_factor >= 1
        and blind.average_win_loss >= 1.2
        and blind.expectancy_ci95[0] > 0
    ):
        return decision(
            ExperimentStage.REJECTED,
            "falsified by non-positive or statistically weak net out-of-sample performance",
        )
    if evidence.paper_trading_days < 30:
        return decision(
            ExperimentStage.ELIGIBLE_FOR_PAPER,
            "historical evidence passed; requires thirty independent Paper trading days",
        )
    return decision(
        ExperimentStage.ELIGIBLE_FOR_HUMAN_REVIEW,
        "historical and Paper evidence passed; production still requires human approval",
    )
