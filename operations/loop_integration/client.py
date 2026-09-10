from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

import httpx

from .contracts import (
    LoopBinding,
    LoopEventOutcomeAssignment,
    LoopOutcomeAssignment,
    LoopOutcomeEnvelope,
    LoopOutcomeSyncStatus,
    LoopPolicyCandidate,
    QuantReviewEnvelope,
)
from .control_plane import (
    ARTIFACT_ENDPOINTS,
    ControlArtifactSpec,
    LoopControlArtifact,
    LoopControlPlaneManifest,
    config_sha256,
)

JsonRequest = Callable[[str, str, dict[str, Any] | None], Any]


class LoopPreconditionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class AuditOnlyBackfillRequired(LoopPreconditionError):
    pass


class LoopRunFailedError(RuntimeError):
    def __init__(
        self,
        *,
        task_id: str,
        run_id: str,
        failed_node: str,
        error_code: str,
    ) -> None:
        super().__init__(f"Loop Run failed at {failed_node or 'unknown'}: {error_code}")
        self.task_id = task_id
        self.run_id = run_id
        self.failed_node = failed_node
        self.error_code = error_code


class LoopRunIncompleteError(RuntimeError):
    pass


class LoopClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_seconds: float = 30,
        request: JsonRequest | None = None,
    ) -> None:
        if not base_url.strip() or not api_key.strip():
            raise ValueError("Loop base URL and API key are required")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self._request_override = request

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        if self._request_override is not None:
            return self._request_override(method, path, payload)
        response = httpx.request(
            method,
            f"{self.base_url}{path}",
            headers={
                "X-Loop-API-Key": self.api_key,
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        return response.json()

    def submit_review(self, envelope: QuantReviewEnvelope, binding: LoopBinding) -> tuple[str, str]:
        contracts = self.validate_review_contracts(binding=binding, as_of=envelope.as_of)
        task_payload = build_loop_task(envelope, binding)
        signal_contract = next(
            item for item in contracts if item.artifact_type == "signal_contract"
        )
        validate_loop_task_signal_contract(task_payload, signal_contract)
        task = self._request("POST", "/api/v1/tasks", task_payload)
        if not isinstance(task, dict) or not str(task.get("id") or ""):
            raise RuntimeError("Loop create-task response lacks task id")
        task_id = str(task["id"])
        run = self._request("POST", f"/api/v1/tasks/{task_id}/run", {"approve": False})
        if not isinstance(run, dict) or not str(run.get("id") or ""):
            raise RuntimeError("Loop run response lacks run id")
        run_id = str(run["id"])
        status = str(run.get("status") or "").upper()
        if status == "COMPLETED":
            return task_id, run_id
        if status == "FAILED":
            failed_node, error_code = _run_failure(run)
            raise LoopRunFailedError(
                task_id=task_id,
                run_id=run_id,
                failed_node=failed_node,
                error_code=error_code,
            )
        raise LoopRunIncompleteError(
            f"Loop Run {run_id} returned non-terminal status {status or 'UNKNOWN'}"
        )

    def initialize_control_plane(self, manifest: LoopControlPlaneManifest) -> LoopBinding:
        for spec in manifest.artifacts:
            matches = self._list_control_artifacts(
                artifact_type=spec.artifact_type,
                market_scope=manifest.market_scope,
            )
            existing = next((item for item in matches if item.id == spec.payload["id"]), None)
            if existing is not None:
                self._validate_spec(existing, spec)
                continue
            created = self._request(
                "POST", ARTIFACT_ENDPOINTS[spec.artifact_type], spec.request_payload()
            )
            self._validate_spec(LoopControlArtifact.model_validate(created), spec)
        return manifest.binding()

    def validate_review_contracts(
        self, *, binding: LoopBinding, as_of: datetime
    ) -> tuple[LoopControlArtifact, ...]:
        expected = (
            ("signal_contract", binding.signal_contract_id, binding.signal_contract_sha256),
            ("fsm_contract", binding.fsm_contract_id, binding.fsm_contract_sha256),
            ("golden_case_suite", binding.golden_suite_id, binding.golden_suite_sha256),
        )
        validated: list[LoopControlArtifact] = []
        for artifact_type, artifact_id, expected_hash in expected:
            matches = self._list_control_artifacts(
                artifact_type=artifact_type,
                market_scope=binding.market_scope,
            )
            artifact = next((item for item in matches if item.id == artifact_id), None)
            if artifact is None:
                raise LoopPreconditionError(
                    "CONTRACT_NOT_FOUND", f"missing {artifact_type}: {artifact_id}"
                )
            self._validate_artifact(
                artifact,
                artifact_type=artifact_type,
                artifact_id=artifact_id,
                market_scope=binding.market_scope,
                expected_hash=expected_hash,
            )
            validated.append(artifact)
        unavailable = [item for item in validated if item.available_at > as_of]
        if unavailable:
            raise AuditOnlyBackfillRequired(
                "CONTRACT_NOT_AVAILABLE_AT_AS_OF",
                "review as_of predates control contract availability",
            )
        return tuple(validated)

    def _list_control_artifacts(
        self, *, artifact_type: str, market_scope: str
    ) -> tuple[LoopControlArtifact, ...]:
        query = urlencode(
            {
                "artifact_type": artifact_type,
                "market_scope": market_scope,
                "limit": 200,
            }
        )
        result = self._request("GET", f"/api/v1/knowledge/quant/control-artifacts?{query}", None)
        if not isinstance(result, list):
            raise LoopPreconditionError(
                "INVALID_CONTROL_ARTIFACT_RESPONSE",
                "Loop control-artifact response is not a list",
            )
        try:
            return tuple(LoopControlArtifact.model_validate(item) for item in result)
        except ValueError as exc:
            raise LoopPreconditionError(
                "INVALID_CONTROL_ARTIFACT_RESPONSE",
                "Loop returned a malformed control artifact",
            ) from exc

    @staticmethod
    def _validate_artifact(
        artifact: LoopControlArtifact,
        *,
        artifact_type: str,
        artifact_id: str,
        market_scope: str,
        expected_hash: str,
    ) -> None:
        metadata = artifact.payload.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        try:
            payload_hash_matches = config_sha256(artifact.payload) == expected_hash
        except (TypeError, ValueError):
            payload_hash_matches = False
        checks = {
            "id": artifact.id == artifact_id,
            "type": artifact.artifact_type == artifact_type,
            "status": artifact.status == "active",
            "market_scope": artifact.market_scope == market_scope,
            "mode": artifact.payload.get("mode") == "PAPER_ONLY",
            "allow_order_execution": metadata.get("allow_order_execution") is False,
            "production_eligible": metadata.get("production_eligible") is False,
            "config_sha256": metadata.get("config_sha256") == expected_hash,
            "payload_sha256": payload_hash_matches,
        }
        failed = [name for name, passed in checks.items() if not passed]
        if failed:
            raise LoopPreconditionError(
                "CONTRACT_MISMATCH",
                f"control contract {artifact_id} failed: {','.join(failed)}",
            )

    @classmethod
    def _validate_spec(cls, artifact: LoopControlArtifact, spec: ControlArtifactSpec) -> None:
        cls._validate_artifact(
            artifact,
            artifact_type=spec.artifact_type,
            artifact_id=str(spec.payload["id"]),
            market_scope=str(spec.payload["market_scope"]),
            expected_hash=spec.expected_sha256,
        )

    def submit_outcome(self, outcome: LoopOutcomeEnvelope) -> str:
        payload = outcome.model_dump(mode="json")
        result = self._request("POST", "/api/v1/knowledge/quant/outcomes", payload)
        if not isinstance(result, dict) or not str(result.get("id") or ""):
            raise RuntimeError("Loop outcome response lacks id")
        return str(result["id"])

    def submit_outcome_sync_statuses(self, statuses: tuple[LoopOutcomeSyncStatus, ...]) -> int:
        if not statuses:
            return 0
        batch_size = 1000
        for offset in range(0, len(statuses), batch_size):
            batch = statuses[offset : offset + batch_size]
            result = self._request(
                "POST",
                "/api/v1/knowledge/quant/outcome-sync-statuses",
                {"statuses": [item.model_dump(mode="json") for item in batch]},
            )
            if not isinstance(result, dict) or int(result.get("saved") or 0) != len(batch):
                raise RuntimeError("Loop outcome sync-status response is incomplete")
        return len(statuses)

    def list_outcome_assignments(
        self,
        *,
        market_scope: str,
        limit: int = 5000,
    ) -> tuple[LoopOutcomeAssignment, ...]:
        query = urlencode({"market_scope": market_scope, "limit": limit})
        result = self._request(
            "GET",
            f"/api/v1/knowledge/quant/outcome-assignments?{query}",
            None,
        )
        if not isinstance(result, list):
            raise RuntimeError("Loop outcome-assignment response is not a list")
        return tuple(LoopOutcomeAssignment.model_validate(item) for item in result)

    def list_event_outcome_assignments(
        self,
        *,
        market_scope: str,
        limit: int = 5000,
    ) -> tuple[LoopEventOutcomeAssignment, ...]:
        query = urlencode({"market_scope": market_scope, "limit": limit})
        result = self._request(
            "GET",
            f"/api/v1/knowledge/quant/event-outcome-assignments?{query}",
            None,
        )
        if not isinstance(result, list):
            raise RuntimeError("Loop event-outcome-assignment response is not a list")
        return tuple(LoopEventOutcomeAssignment.model_validate(item) for item in result)

    def list_policy_candidates(self, *, market_scope: str) -> tuple[LoopPolicyCandidate, ...]:
        query = urlencode(
            {
                "artifact_type": "strategy_policy_candidate",
                "market_scope": market_scope,
                "status": "candidate",
                "limit": 200,
            }
        )
        path = f"/api/v1/knowledge/quant/control-artifacts?{query}"
        result = self._request("GET", path, None)
        if not isinstance(result, list):
            raise RuntimeError("Loop policy-candidate response is not a list")
        return tuple(LoopPolicyCandidate.model_validate(item) for item in result)


def _run_failure(run: dict[str, Any]) -> tuple[str, str]:
    for event in reversed(run.get("events") or []):
        if not isinstance(event, dict):
            continue
        if event.get("event") == "quant_step_failed" or event.get("type") == "quant_step_failed":
            return (
                str(event.get("step_id") or ""),
                str(event.get("error_type") or "LOOP_RUN_FAILED"),
            )
    result = run.get("result")
    result = result if isinstance(result, dict) else {}
    return (
        str(result.get("failed_step_id") or ""),
        str(result.get("error_code") or "LOOP_RUN_FAILED"),
    )


def validate_loop_task_cohort(task_payload: dict[str, Any]) -> None:
    """Fail before submission when one review mixes different daily Top10 cohorts."""

    input_data = task_payload.get("input_data")
    if not isinstance(input_data, dict):
        raise ValueError("Loop task requires input_data")
    dynamic_rescan = input_data.get("dynamic_rescan")
    adjudication = input_data.get("top10_adjudication")
    daily_review = input_data.get("daily_review")
    if not all(isinstance(item, dict) for item in (dynamic_rescan, adjudication, daily_review)):
        raise ValueError("Loop task requires daily review cohort sections")

    ranked = dynamic_rescan.get("ranked_candidates")
    adjudicated = adjudication.get("decisions")
    reviewed = daily_review.get("top10_verdicts")
    if not isinstance(ranked, list) or len(ranked) < 10:
        raise ValueError("Loop task requires at least 10 ranked candidates")
    if not isinstance(adjudicated, list) or len(adjudicated) != 10:
        raise ValueError("Loop task requires exactly 10 Top10 adjudication decisions")
    if not isinstance(reviewed, list) or len(reviewed) != 10:
        raise ValueError("Loop task requires exactly 10 daily review verdicts")

    def instruments(items: list[Any], field_name: str) -> tuple[str, ...]:
        result = tuple(
            str(item.get("instrument") or "").strip()
            if isinstance(item, dict)
            else ""
            for item in items
        )
        if any(not instrument for instrument in result):
            raise ValueError(f"{field_name} requires non-empty instruments")
        if len(set(result)) != len(result):
            raise ValueError(f"{field_name} requires unique instruments")
        return result

    required = instruments(ranked, "dynamic_rescan.ranked_candidates")[:10]
    for section_name, actual in (
        ("top10_adjudication", instruments(adjudicated, "top10_adjudication.decisions")),
        ("daily_review", instruments(reviewed, "daily_review.top10_verdicts")),
    ):
        required_set = set(required)
        actual_set = set(actual)
        if required_set != actual_set:
            missing = [item for item in required if item not in actual_set]
            unexpected = [item for item in actual if item not in required_set]
            raise ValueError(
                f"Top10 cohort mismatch in {section_name}: "
                f"missing={','.join(missing) or '-'}; "
                f"unexpected={','.join(unexpected) or '-'}"
            )

    adjudicated_verdicts = {
        str(item["instrument"]).strip(): str(item.get("verdict") or "").strip().lower()
        for item in adjudicated
    }
    reviewed_verdicts = {
        str(item["instrument"]).strip(): str(item.get("verdict") or "").strip().lower()
        for item in reviewed
    }
    drift = [
        instrument
        for instrument in required
        if adjudicated_verdicts[instrument] != reviewed_verdicts[instrument]
    ]
    if drift:
        raise ValueError(
            "daily_review verdicts differ from Top10 adjudication: " + ", ".join(drift)
        )


def build_loop_task(envelope: QuantReviewEnvelope, binding: LoopBinding) -> dict[str, Any]:
    decisions = envelope.top10_decisions
    primary = decisions[0]
    market_regime = str(envelope.market_context.get("market_regime") or "UNKNOWN")
    top10 = [
        {
            "instrument": item.instrument,
            "market_regime": item.market_regime,
            "classification": item.classification,
            "classification_source": item.classification_source,
            "logging_policy_id": item.logging_policy_id,
            "logged_action": item.logged_action,
            "logging_action_probability": item.logging_action_probability,
            "reward_model_logged": item.reward_model_logged,
            "verdict": item.verdict,
            "decision_intent": item.decision_intent.model_dump(mode="json"),
            "reason": item.reason,
            "one_minute_path": list(item.one_minute_path),
            "trigger_results": item.trigger_results,
            "risk_controls": list(item.risk_controls),
            "risk_policy": envelope.risk_policy,
            "conditions": item.features,
            "invalidation_conditions": list(item.invalidation_conditions),
            "source_snapshot_ids": list(item.source_snapshot_ids),
        }
        for item in decisions
    ]
    metadata = {
        "source_system": envelope.provenance.source_system,
        "synthetic": envelope.provenance.synthetic,
        "not_real_market_data": envelope.provenance.not_real_market_data,
        "code_commit": envelope.provenance.code_commit,
        "config_sha256": envelope.provenance.config_sha256,
        "source_snapshot_ids": list(envelope.provenance.source_snapshot_ids),
        "strategy_id": envelope.strategy.strategy_id,
        "strategy_version": envelope.strategy.strategy_version,
        "active_policy_version": envelope.strategy.active_policy_version,
        "active_policy_hash": envelope.strategy.active_policy_hash,
        "payload_sha256": envelope.payload_sha256,
        "market_regime": market_regime,
        "decision_cohort_id": f"quant-review-cohort:{envelope.payload_sha256[:32]}",
        "decision_trading_date": envelope.trading_date.isoformat(),
    }
    task_payload = {
        "workflow_id": binding.workflow_id,
        "workflow_version_id": binding.workflow_version_id,
        "objective": (
            f"Review {envelope.market_scope} {envelope.trading_date.isoformat()} "
            f"{envelope.strategy.strategy_id} evidence"
        ),
        "source_system": envelope.provenance.source_system,
        "source_external_id": envelope.event_id,
        "constraints": {
            "execution_mode": "PAPER_ONLY",
            "allow_order_execution": False,
            "synthetic": envelope.provenance.synthetic,
            "not_real_market_data": envelope.provenance.not_real_market_data,
            "source_system": envelope.provenance.source_system,
        },
        "success_criteria": [
            "point-in-time evidence passes",
            "Top10 adjudication is complete",
            "Golden replay passes",
            "order execution remains forbidden",
        ],
        "input_data": {
            "market_scope": envelope.market_scope,
            "as_of": envelope.as_of.isoformat(),
            "signal_validation": {
                "contract_id": binding.signal_contract_id,
                "as_of": envelope.as_of.isoformat(),
                "signal": {
                    "instrument": primary.instrument,
                    "signal_type": "long" if primary.verdict == "accept" else "watch",
                    "event_time": primary.event_time.isoformat(),
                    "available_at": primary.available_at.isoformat(),
                    "features": primary.features,
                    "metadata": metadata,
                },
            },
            "dynamic_rescan": {
                "market_scope": envelope.market_scope,
                "as_of": envelope.as_of.isoformat(),
                "available_at": envelope.as_of.isoformat(),
                "trigger": "scheduled",
                "trigger_evidence": {"review_event_id": envelope.event_id},
                "universe": [item.instrument for item in decisions],
                "ranked_candidates": [
                    {
                        "instrument": item.instrument,
                        "rank": item.rank,
                        "market_regime": item.market_regime,
                        "classification": item.classification,
                        "classification_source": item.classification_source,
                        **item.features,
                    }
                    for item in decisions
                ],
                "top_n": 10,
                "source_snapshot_ids": list(envelope.provenance.source_snapshot_ids),
                "metadata": metadata,
            },
            "top10_adjudication": {"decisions": top10, "metadata": metadata},
            "fsm_transition": {
                "contract_id": binding.fsm_contract_id,
                "market_scope": envelope.market_scope,
                "instrument": primary.instrument,
                "event_type": binding.fsm_review_event_type,
                "event_time": primary.event_time.isoformat(),
                "available_at": primary.available_at.isoformat(),
                "as_of": envelope.as_of.isoformat(),
                "guard_snapshot": envelope.execution_summary,
                "reason": "daily_review_completed",
                "metadata": metadata,
            },
            "golden_replay": {
                "suite_id": binding.golden_suite_id,
                "actual_results": binding.golden_actual_results,
                "metadata": metadata,
            },
            "daily_review": {
                "market_scope": envelope.market_scope,
                "trading_date": envelope.trading_date.isoformat(),
                "signal_contract_id": binding.signal_contract_id,
                "fsm_contract_id": binding.fsm_contract_id,
                "outcome_ids": [],
                "top10_verdicts": top10,
                "risk_policy": envelope.risk_policy,
                "metrics": envelope.metrics,
                "metric_semantics": {
                    "schema_version": "quant-review-metrics-v2",
                    "return_unit": "decimal_fraction",
                    "return_aggregation": ("unweighted_sum_of_instrument_close_returns"),
                    "positive_rate_denominator": ("top10_instruments_with_close_return"),
                    "portfolio_pnl_available": False,
                },
                "conclusions": list(envelope.conclusions),
                "metadata": metadata,
            },
        },
    }
    validate_loop_task_cohort(task_payload)
    return task_payload


def validate_loop_task_signal_contract(
    task_payload: dict[str, Any],
    signal_contract: LoopControlArtifact,
) -> None:
    """Reject a Task locally when Loop's frozen SignalContract would reject it."""
    input_data = task_payload.get("input_data")
    if not isinstance(input_data, dict):
        raise LoopPreconditionError(
            "INVALID_SIGNAL_VALIDATION",
            "Task input_data must be an object",
        )
    validation = input_data.get("signal_validation")
    signal = validation.get("signal") if isinstance(validation, dict) else None
    if not isinstance(validation, dict) or not isinstance(signal, dict):
        raise LoopPreconditionError(
            "INVALID_SIGNAL_VALIDATION",
            "Task input_data.signal_validation.signal must be an object",
        )

    contract_id = str(validation.get("contract_id") or "").strip()
    if contract_id != signal_contract.id:
        raise LoopPreconditionError(
            "SIGNAL_CONTRACT_MISMATCH",
            f"Task references {contract_id or '-'} but validated {signal_contract.id}",
        )

    features = signal.get("features")
    if not isinstance(features, dict):
        raise LoopPreconditionError(
            "INVALID_SIGNAL_VALIDATION",
            "signal.features must be an object",
        )

    required_features = tuple(
        str(item).strip()
        for item in signal_contract.payload.get("required_features") or ()
        if str(item).strip()
    )
    missing = [name for name in required_features if name not in features]
    signal_type = str(signal.get("signal_type") or "").strip().lower()
    allowed_signal_types = {
        str(item).strip().lower()
        for item in signal_contract.payload.get("allowed_signal_types") or ()
        if str(item).strip()
    }
    try:
        as_of = _aware_task_datetime(
            validation.get("as_of") or input_data.get("as_of"),
            field_name="signal_validation.as_of",
        )
        event_time = _aware_task_datetime(
            signal.get("event_time"), field_name="signal.event_time"
        )
        available_at = _aware_task_datetime(
            signal.get("available_at"), field_name="signal.available_at"
        )
    except (TypeError, ValueError) as exc:
        raise LoopPreconditionError("INVALID_SIGNAL_VALIDATION", str(exc)) from exc

    if available_at < event_time:
        raise LoopPreconditionError(
            "INVALID_SIGNAL_VALIDATION",
            "signal available_at must not precede event_time",
        )

    violations: list[str] = []
    if missing:
        violations.append("missing_features:" + ",".join(missing))
    if signal_type not in allowed_signal_types:
        violations.append("unsupported_signal_type")
    if available_at > as_of:
        violations.append("future_information")
    age_seconds = (as_of - event_time).total_seconds()
    if age_seconds < 0:
        violations.append("future_event")
    if age_seconds > int(signal_contract.payload.get("max_signal_age_seconds") or 300):
        violations.append("stale_signal")
    if violations:
        raise LoopPreconditionError(
            "SIGNAL_CONTRACT_REJECTED",
            f"SignalContract {signal_contract.id} rejected signal: {';'.join(violations)}",
        )


def _aware_task_datetime(value: Any, *, field_name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    else:
        raise ValueError(f"{field_name} must be an ISO datetime")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return parsed
