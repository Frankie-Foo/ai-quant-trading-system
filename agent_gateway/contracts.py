"""Strict data contracts for the slow-loop agent boundary.

Decision-bearing numbers are represented as ``Fact`` objects so every numeric value
has an explicit availability state and provenance. Narrative fields are deliberately
kept separate from facts and may not contain bare numeric tokens.
"""

from __future__ import annotations

import math
import re
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

BARE_NUMBER = re.compile(r"(?<![A-Za-z])[-+]?\d+(?:\.\d+)?(?:%|x)?(?![A-Za-z])")
SYMBOL_PATTERN = r"^[A-Z][A-Z0-9.-]{0,15}$"


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AgentRole(StrEnum):
    COMMANDER = "commander"
    RISK = "risk"
    FACTOR_HUNTER = "factor-hunter"
    ORDER_FLOW = "order-flow"
    SHORT_THESIS = "short-thesis"
    SENTIMENT = "sentiment"
    DISCIPLINE = "discipline"
    PDCA = "pdca"


class Availability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "N/A"


class Fact(FrozenModel):
    name: str = Field(min_length=1, max_length=100)
    value: bool | int | float | str | None
    availability: Availability
    provenance: str = Field(min_length=1, max_length=1000)
    asof_utc: datetime | None = None

    @field_validator("value")
    @classmethod
    def finite_number(
        cls, value: bool | int | float | str | None
    ) -> bool | int | float | str | None:
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("fact numbers must be finite")
        return value

    @field_validator("asof_utc")
    @classmethod
    def utc_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() != timedelta(0)):
            raise ValueError("fact asof_utc must be timezone-aware UTC")
        return value

    @model_validator(mode="after")
    def availability_matches_value(self) -> Self:
        if self.availability is Availability.AVAILABLE and self.value is None:
            raise ValueError("available facts require a value")
        if self.availability is Availability.UNAVAILABLE and self.value is not None:
            raise ValueError("N/A facts must use a null value")
        return self


class ThesisStage(StrEnum):
    PREMARKET = "premarket"
    VERIFY = "verify"
    POSTMARKET = "postmarket"


class ThesisStance(StrEnum):
    GO = "GO"
    NO_GO = "NO-GO"
    WATCH = "WATCH"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


def _normalize_symbol(value: str) -> str:
    return value.strip().upper()


def _no_bare_number(value: str) -> str:
    if BARE_NUMBER.search(value):
        raise ValueError("narrative decision numbers must be supplied as provenance-bearing facts")
    return value.strip()


class Thesis(FrozenModel):
    agent: AgentRole
    symbol: str = Field(pattern=SYMBOL_PATTERN)
    trade_date: date
    stage: ThesisStage
    stance: ThesisStance
    facts: tuple[Fact, ...] = Field(min_length=1, max_length=100)
    inference: str = Field(min_length=10, max_length=3000)
    falsification: str = Field(min_length=10, max_length=2000)
    source_snapshot_ids: tuple[str, ...] = Field(min_length=1, max_length=100)

    _symbol = field_validator("symbol", mode="before")(_normalize_symbol)
    _inference = field_validator("inference")(_no_bare_number)
    _falsification = field_validator("falsification")(_no_bare_number)

    @field_validator("source_snapshot_ids")
    @classmethod
    def unique_sources(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(item.strip() for item in value)
        if any(not item for item in cleaned) or len(set(cleaned)) != len(cleaned):
            raise ValueError("source snapshot IDs must be non-empty and unique")
        return cleaned


class LessonCategory(StrEnum):
    SELECTION_REVIEW = "selection_review"
    SIGNAL_DECAY = "signal_decay"
    EXECUTION_GAP = "execution_gap"
    COST_DRIFT = "cost_drift"


class AuditSeverity(StrEnum):
    RED = "red"
    YELLOW = "yellow"
    WHITE = "white"


class AuditFinding(FrozenModel):
    rule: str = Field(min_length=3, max_length=100)
    severity: AuditSeverity
    summary: str = Field(min_length=10, max_length=2000)
    metrics: tuple[Fact, ...] = Field(min_length=1, max_length=100)
    source_record_ids: tuple[str, ...] = Field(min_length=1, max_length=200)

    _summary = field_validator("summary")(_no_bare_number)


class AuditReport(FrozenModel):
    agent: AgentRole
    trade_date: date
    status: str = Field(pattern=r"^(complete|incomplete_evidence)$")
    findings: tuple[AuditFinding, ...] = Field(default=(), max_length=500)
    source_record_ids: tuple[str, ...] = Field(default=(), max_length=500)

    @model_validator(mode="after")
    def discipline_only(self) -> Self:
        if self.agent is not AgentRole.DISCIPLINE:
            raise ValueError("only discipline may create audit reports")
        if self.status == "complete" and not self.source_record_ids:
            raise ValueError("complete audit reports require source records")
        return self


class Lesson(FrozenModel):
    agent: AgentRole
    category: LessonCategory
    trade_date: date
    hypothesis: str = Field(min_length=10, max_length=2000)
    observation: str = Field(min_length=10, max_length=4000)
    conclusion: str = Field(min_length=10, max_length=3000)
    metrics: tuple[Fact, ...] = Field(min_length=1, max_length=100)
    source_record_ids: tuple[str, ...] = Field(min_length=1, max_length=200)
    factor_profile: tuple[str, ...] = Field(default=(), max_length=50)

    _hypothesis = field_validator("hypothesis")(_no_bare_number)
    _observation = field_validator("observation")(_no_bare_number)
    _conclusion = field_validator("conclusion")(_no_bare_number)

    @field_validator("factor_profile")
    @classmethod
    def factor_profile_has_no_bare_numbers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_no_bare_number(item) for item in value)


class EvolutionProposal(FrozenModel):
    agent: AgentRole
    proposal_month: date
    hypothesis: str = Field(min_length=10, max_length=3000)
    expected_effect: str = Field(min_length=10, max_length=3000)
    validation_plan: str = Field(min_length=10, max_length=5000)
    target_metrics: tuple[Fact, ...] = Field(min_length=1, max_length=100)
    evidence_lesson_ids: tuple[str, ...] = Field(min_length=1, max_length=500)
    attempted_config_hashes: tuple[str, ...] = Field(default=(), max_length=500)
    status: str = Field(default="draft", pattern=r"^draft$")
    production_eligible: bool = False

    _hypothesis = field_validator("hypothesis")(_no_bare_number)
    _expected_effect = field_validator("expected_effect")(_no_bare_number)
    _validation_plan = field_validator("validation_plan")(_no_bare_number)

    @field_validator("attempted_config_hashes")
    @classmethod
    def valid_config_hashes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(item.strip().lower() for item in value)
        if any(re.fullmatch(r"[0-9a-f]{64}", item) is None for item in cleaned):
            raise ValueError("attempted configuration hashes must be SHA-256 hex")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("attempted configuration hashes must be unique")
        return cleaned

    @model_validator(mode="after")
    def force_human_approval(self) -> Self:
        if self.status != "draft" or self.production_eligible:
            raise ValueError("agent proposals are always draft and never production eligible")
        if self.agent is not AgentRole.PDCA:
            raise ValueError("only pdca may create evolution proposals")
        return self


class QueryEntity(StrEnum):
    THESES = "agent_theses"
    TRADE_PLANS = "trade_plans"
    EXECUTIONS = "executions"
    BARRIER_EVENTS = "barrier_events"
    FACTOR_SNAPSHOTS = "factor_snapshots"
    UNIVERSE_SNAPSHOTS = "universe_snapshots"
    TRADING_EPISODES = "trading_episodes"
    AUDIT_REPORTS = "audit_reports"
    LESSONS = "lessons"
    PROPOSALS = "evolution_proposals"
    TRADEPLAN_DRAFTS = "agent_tradeplan_drafts"
    TOOL_AUDIT = "tool_audit"


class StoreQuery(FrozenModel):
    entity: QueryEntity
    actor: AgentRole | None = None
    trade_date: date | None = None
    category: LessonCategory | None = None
    limit: int = Field(default=100, ge=1, le=200)


class ToolEnvelope(FrozenModel):
    tool: str = Field(min_length=1)
    asof_utc: datetime
    availability: Availability
    provenance: str = Field(min_length=1)
    snapshot_ids: tuple[str, ...] = ()
    data: dict[str, object] | list[dict[str, object]]

    @field_validator("asof_utc")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("tool timestamps must be timezone-aware UTC")
        return value


def now_utc() -> datetime:
    return datetime.now(UTC)
