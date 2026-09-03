from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, date, datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LoopBinding(FrozenModel):
    schema_version: Literal["loop_quant_binding.v2"] = "loop_quant_binding.v2"
    workflow_id: Literal["quant_daily_review"] = "quant_daily_review"
    workflow_version_id: str = Field(default="workflow-version-quant-daily-review-v5", min_length=1)
    market_scope: str = Field(default="US-equity", min_length=1, max_length=128)
    signal_contract_id: str = Field(min_length=1, max_length=64)
    signal_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    fsm_contract_id: str = Field(min_length=1, max_length=64)
    fsm_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    fsm_review_event_type: str = Field(min_length=1, max_length=128)
    golden_suite_id: str = Field(min_length=1, max_length=64)
    golden_suite_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    golden_actual_results: dict[str, dict[str, Any]] = Field(min_length=1)


class ReviewProvenance(FrozenModel):
    source_system: Literal["ai-quant-trading-system"] = "ai-quant-trading-system"
    synthetic: bool = False
    not_real_market_data: bool = False
    code_commit: str = Field(min_length=7, max_length=64)
    config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_snapshot_ids: tuple[str, ...] = Field(min_length=1)
    feature_schema_versions: tuple[str, ...] = ()
    cost_model_version: str = Field(min_length=1)
    created_at_utc: datetime

    @field_validator("created_at_utc")
    @classmethod
    def utc_only(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("created_at_utc must be timezone-aware UTC")
        return value

    @model_validator(mode="after")
    def real_data_is_not_synthetic(self) -> Self:
        if self.synthetic == self.not_real_market_data:
            if self.synthetic:
                return self
            return self
        raise ValueError("synthetic and not_real_market_data must agree")


class StrategyIdentity(FrozenModel):
    strategy_id: str = Field(min_length=1, max_length=128)
    strategy_version: str = Field(min_length=1, max_length=128)
    active_policy_version: str = Field(min_length=1, max_length=128)
    active_policy_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReviewDecision(FrozenModel):
    instrument: str = Field(pattern=r"^[A-Z][A-Z0-9.-]{0,15}$")
    rank: int = Field(ge=1)
    verdict: Literal["accept", "watch", "reject", "block"]
    reason: str = Field(min_length=1, max_length=6000)
    event_time: datetime
    available_at: datetime
    features: dict[str, float | int | str | bool | None]
    one_minute_path: tuple[dict[str, Any], ...] = ()
    trigger_results: dict[str, Any]
    risk_controls: tuple[str, ...] = Field(min_length=1)
    invalidation_conditions: tuple[str, ...] = Field(min_length=1)
    source_snapshot_ids: tuple[str, ...] = Field(min_length=1)

    @field_validator("event_time", "available_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("decision timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def available_after_event(self) -> Self:
        if self.available_at < self.event_time:
            raise ValueError("available_at cannot precede event_time")
        for value in self.features.values():
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("decision features must be finite or null")
        return self


class QuantReviewEnvelope(FrozenModel):
    schema_version: Literal["ai_quant.loop_daily_review.v1"] = "ai_quant.loop_daily_review.v1"
    event_id: str = Field(min_length=1, max_length=255)
    trading_date: date
    market_scope: str = Field(min_length=1, max_length=128)
    as_of: datetime
    strategy: StrategyIdentity
    provenance: ReviewProvenance
    market_context: dict[str, Any]
    top10_decisions: tuple[ReviewDecision, ...] = Field(min_length=10, max_length=10)
    execution_summary: dict[str, Any]
    risk_policy: dict[str, Any]
    metrics: dict[str, float]
    conclusions: tuple[str, ...] = Field(min_length=1)

    @field_validator("as_of")
    @classmethod
    def as_of_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        ranks = tuple(item.rank for item in self.top10_decisions)
        symbols = tuple(item.instrument for item in self.top10_decisions)
        if ranks != tuple(range(1, 11)):
            raise ValueError("Top10 decisions must have contiguous ranks 1..10")
        if len(set(symbols)) != 10:
            raise ValueError("Top10 decisions must contain ten unique instruments")
        if any(item.available_at > self.as_of for item in self.top10_decisions):
            raise ValueError("review cannot consume information after as_of")
        return self

    @property
    def payload_sha256(self) -> str:
        raw = self.model_dump(mode="json")
        encoded = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


class LoopOutcomeEnvelope(FrozenModel):
    schema_version: Literal["ai_quant.loop_outcome.v1"] = "ai_quant.loop_outcome.v1"
    id: str = Field(min_length=1, max_length=64)
    decision_event_id: str = Field(min_length=1, max_length=64)
    source_run_id: str = Field(min_length=1, max_length=64)
    market_scope: str = Field(min_length=1, max_length=128)
    instrument: str = Field(pattern=r"^[A-Z][A-Z0-9.-]{0,15}$")
    horizon: Literal["1d", "5d", "20d"]
    observed_at: datetime
    strategy_return: float | None = None
    benchmark_return: float | None = None
    max_drawdown: float | None = None
    transaction_cost: float | None = None
    slippage: float | None = None
    direction_correct: bool | None = None
    evidence: dict[str, Any]
    metadata: dict[str, Any]

    @field_validator("observed_at")
    @classmethod
    def observed_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def require_point_in_time_lineage(self) -> Self:
        if self.metadata.get("point_in_time_guard_passed") is not True:
            raise ValueError("outcome requires a passed point-in-time guard")
        if self.metadata.get("evaluation_role") not in {
            "holdout",
            "walk_forward",
            "forward",
        }:
            raise ValueError("outcome evaluation_role is invalid")
        if not str(self.metadata.get("strategy_revision_id") or "").strip():
            raise ValueError("outcome requires strategy_revision_id")
        return self


class LoopPolicyCandidate(FrozenModel):
    id: str = Field(min_length=1, max_length=128)
    artifact_type: Literal["strategy_policy_candidate"]
    market_scope: str = Field(min_length=1, max_length=128)
    status: Literal["candidate"]
    effective_at: datetime
    available_at: datetime
    source_run_id: str = Field(default="", max_length=64)
    payload: dict[str, Any]
    created_at: datetime
    updated_at: datetime

    @field_validator("effective_at", "available_at", "created_at", "updated_at")
    @classmethod
    def candidate_timestamps_are_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Loop candidate timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def enforce_advisory_boundary(self) -> Self:
        if self.available_at < self.effective_at:
            raise ValueError("Loop candidate availability precedes its effective time")
        if self.payload.get("schema_version") != "quant-strategy-policy-v3":
            raise ValueError("unsupported Loop strategy policy schema")
        if self.payload.get("mode") != "PAPER_ONLY":
            raise ValueError("Loop candidate is not PAPER_ONLY")
        if self.payload.get("allow_order_execution") is not False:
            raise ValueError("Loop candidate attempted to authorize orders")
        if self.payload.get("production_eligible") is not False:
            raise ValueError("Loop candidate attempted to claim production eligibility")
        if not str(self.payload.get("strategy_revision_id") or "").strip():
            raise ValueError("Loop candidate lacks a strategy revision")
        if not str(self.payload.get("strategy_fingerprint") or "").strip():
            raise ValueError("Loop candidate lacks a strategy fingerprint")
        return self
