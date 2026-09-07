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
    workflow_version_id: str = Field(default="workflow-version-quant-daily-review-v6", min_length=1)
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
    metrics: dict[str, int | float]
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


OUTCOME_GOVERNANCE_KEYS = (
    "strategy_revision_id",
    "evaluation_role",
    "point_in_time_guard_passed",
)
OUTCOME_HORIZON_SESSIONS = {"1d": 1, "5d": 5, "20d": 20}
OUTCOME_EXCESS_FORMULA = (
    "strategy_return-benchmark_return-transaction_cost-slippage"
)


class LoopOutcomeEnvelope(FrozenModel):
    schema_version: Literal[
        "ai_quant.loop_outcome.v1", "ai_quant.loop_outcome.v2"
    ] = "ai_quant.loop_outcome.v1"
    id: str = Field(min_length=1, max_length=64)
    decision_event_id: str = Field(min_length=1, max_length=64)
    source_run_id: str = Field(min_length=1, max_length=64)
    market_scope: str = Field(min_length=1, max_length=128)
    instrument: str = Field(pattern=r"^[A-Z][A-Z0-9.-]{0,15}$")
    horizon: Literal["1d", "5d", "20d"]
    observed_at: datetime
    strategy_return: float | None = None
    benchmark_return: float | None = None
    excess_return: float | None = None
    max_drawdown: float | None = None
    risk_adjusted_return: float | None = None
    transaction_cost: float | None = None
    slippage: float | None = None
    direction_correct: bool | None = None
    status: str = Field(default="observed", min_length=1, max_length=64)
    evidence: dict[str, Any]
    metadata: dict[str, Any]

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_governance_location(cls, raw: Any) -> Any:
        schema_version = (
            raw.get("schema_version", "ai_quant.loop_outcome.v1")
            if isinstance(raw, dict)
            else ""
        )
        if not isinstance(raw, dict) or schema_version != "ai_quant.loop_outcome.v1":
            return raw
        payload = dict(raw)
        evidence = dict(payload.get("evidence") or {})
        metadata = dict(payload.get("metadata") or {})
        for key in OUTCOME_GOVERNANCE_KEYS:
            evidence_value = evidence.get(key)
            metadata_value = metadata.get(key)
            if (
                evidence_value is not None
                and metadata_value is not None
                and evidence_value != metadata_value
            ):
                raise ValueError(f"outcome has conflicting governance lineage for {key}")
            if evidence_value is None and metadata_value is not None:
                evidence[key] = metadata_value
        payload["evidence"] = evidence
        return payload

    @field_validator("observed_at")
    @classmethod
    def observed_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("observed_at must be timezone-aware UTC")
        return value

    @model_validator(mode="after")
    def require_point_in_time_lineage(self) -> Self:
        if self.evidence.get("point_in_time_guard_passed") is not True:
            raise ValueError("outcome requires a passed point-in-time guard")
        if self.evidence.get("evaluation_role") not in {
            "holdout",
            "walk_forward",
            "forward",
        }:
            raise ValueError("outcome evaluation_role is invalid")
        if not str(self.evidence.get("strategy_revision_id") or "").strip():
            raise ValueError("outcome requires strategy_revision_id")
        if self.schema_version == "ai_quant.loop_outcome.v2":
            self._validate_v2()
        return self

    def _validate_v2(self) -> None:
        if any(key in self.metadata for key in OUTCOME_GOVERNANCE_KEYS):
            raise ValueError("outcome v2 governance lineage belongs in evidence")
        if self.evidence.get("schema_version") != "quant-outcome-evidence-v2":
            raise ValueError("outcome v2 evidence schema is invalid")
        try:
            decision_date = date.fromisoformat(
                str(self.evidence["decision_trading_date"])
            )
            horizon_date = date.fromisoformat(
                str(self.evidence["horizon_end_trading_date"])
            )
            horizon_close = datetime.fromisoformat(
                str(self.evidence["horizon_end_market_close_utc"]).replace(
                    "Z", "+00:00"
                )
            )
        except (KeyError, ValueError) as exc:
            raise ValueError("outcome v2 requires valid horizon evidence") from exc
        if (
            horizon_close.tzinfo is None
            or horizon_close.utcoffset() != UTC.utcoffset(horizon_close)
        ):
            raise ValueError("horizon close must be timezone-aware UTC")
        if horizon_date <= decision_date:
            raise ValueError("horizon trading date must follow decision trading date")
        if self.observed_at < horizon_close:
            raise ValueError("observed_at cannot precede horizon close")
        raw_sessions = self.evidence.get("trading_session_dates")
        if not isinstance(raw_sessions, list):
            raise ValueError("outcome v2 requires trading_session_dates")
        try:
            sessions = [date.fromisoformat(str(value)) for value in raw_sessions]
        except ValueError as exc:
            raise ValueError("trading_session_dates must contain ISO dates") from exc
        if (
            len(sessions) != OUTCOME_HORIZON_SESSIONS[self.horizon]
            or sessions != sorted(set(sessions))
            or any(value <= decision_date for value in sessions)
            or sessions[-1] != horizon_date
        ):
            raise ValueError("trading_session_dates do not match the declared horizon")
        calendar = self.evidence.get("trading_calendar")
        if not isinstance(calendar, dict) or any(
            not str(calendar.get(key) or "").strip()
            for key in ("name", "source", "version")
        ):
            raise ValueError("outcome v2 requires versioned trading_calendar evidence")
        if not str(self.evidence.get("benchmark_id") or "").strip():
            raise ValueError("outcome v2 requires benchmark_id")
        snapshots = self.evidence.get("price_snapshot_ids")
        if not isinstance(snapshots, list) or len(snapshots) < 2:
            raise ValueError("outcome v2 requires start and horizon price snapshots")
        semantics = self.evidence.get("return_semantics")
        required_semantics = {
            "unit": "decimal_fraction",
            "method": "close_to_close_split_adjusted",
            "strategy_return_basis": "gross_before_costs",
            "excess_return_formula": OUTCOME_EXCESS_FORMULA,
        }
        if not isinstance(semantics, dict) or any(
            semantics.get(key) != value for key, value in required_semantics.items()
        ):
            raise ValueError("outcome v2 return_semantics are invalid")
        metrics = (
            self.strategy_return,
            self.benchmark_return,
            self.excess_return,
            self.max_drawdown,
            self.transaction_cost,
            self.slippage,
        )
        if any(value is None or not math.isfinite(float(value)) for value in metrics):
            raise ValueError("outcome v2 requires finite metrics")
        if float(self.transaction_cost or 0) < 0 or float(self.slippage or 0) < 0:
            raise ValueError("outcome v2 costs and slippage must be non-negative")
        if float(self.max_drawdown or 0) > 0:
            raise ValueError("outcome v2 max_drawdown must be non-positive")
        if self.direction_correct is None:
            raise ValueError("outcome v2 requires direction_correct")
        expected_excess = (
            float(self.strategy_return or 0)
            - float(self.benchmark_return or 0)
            - float(self.transaction_cost or 0)
            - float(self.slippage or 0)
        )
        if not math.isclose(
            float(self.excess_return or 0),
            expected_excess,
            rel_tol=0.0,
            abs_tol=1e-10,
        ):
            raise ValueError("outcome v2 excess_return formula mismatch")


class LoopOutcomeAssignment(FrozenModel):
    schema_version: Literal["quant-outcome-assignment-v1"] = (
        "quant-outcome-assignment-v1"
    )
    strategy_revision_id: str = Field(min_length=1, max_length=128)
    strategy_lineage_id: str = Field(min_length=1, max_length=128)
    decision_event_id: str = Field(min_length=1, max_length=128)
    source_run_id: str = Field(min_length=1, max_length=128)
    market_scope: str = Field(min_length=1, max_length=128)
    instrument: str = Field(pattern=r"^[A-Z][A-Z0-9.-]{0,15}$")
    decision_trading_date: date
    observed_verdict: Literal["accept", "watch", "reject", "block"]
    target_verdict: Literal["accept", "watch", "reject", "block"]
    evaluation_role: Literal["holdout", "walk_forward", "forward"]
    outstanding_horizons: tuple[Literal["1d", "5d", "20d"], ...] = Field(
        min_length=1
    )


class OutcomeReporterConfig(FrozenModel):
    schema_version: Literal["ai_quant.loop_outcome_reporter.v1"] = (
        "ai_quant.loop_outcome_reporter.v1"
    )
    market_scope: str = Field(default="US-equity", min_length=1, max_length=128)
    benchmark_symbol: str = Field(pattern=r"^[A-Z][A-Z0-9.-]{0,15}$")
    transaction_cost_bps_round_trip: float = Field(ge=0)
    slippage_bps_round_trip: float = Field(ge=0)
    watch_neutral_band_bps: float = Field(default=25.0, ge=0)
    cost_model_version: str = Field(min_length=1, max_length=128)
    approved_by: str = Field(min_length=1, max_length=128)
    approved_at_utc: datetime
    price_source: Literal["massive.grouped_daily"] = "massive.grouped_daily"
    adjustment: Literal["split_adjusted"] = "split_adjusted"

    @field_validator("approved_at_utc")
    @classmethod
    def approval_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("approved_at_utc must be timezone-aware UTC")
        return value


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
