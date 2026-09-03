from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

import httpx

from .contracts import LoopBinding, LoopOutcomeEnvelope, LoopPolicyCandidate, QuantReviewEnvelope
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
        self.validate_review_contracts(binding=binding, as_of=envelope.as_of)
        task_payload = build_loop_task(envelope, binding)
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
        payload = outcome.model_dump(mode="json", exclude={"schema_version"})
        result = self._request("POST", "/api/v1/knowledge/quant/outcomes", payload)
        if not isinstance(result, dict) or not str(result.get("id") or ""):
            raise RuntimeError("Loop outcome response lacks id")
        return str(result["id"])

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


def build_loop_task(envelope: QuantReviewEnvelope, binding: LoopBinding) -> dict[str, Any]:
    decisions = envelope.top10_decisions
    primary = decisions[0]
    market_regime = str(envelope.market_context.get("regime") or "UNKNOWN")
    top10 = [
        {
            "instrument": item.instrument,
            "verdict": item.verdict,
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
    }
    return {
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
                    {"instrument": item.instrument, "rank": item.rank, **item.features}
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
                "conclusions": list(envelope.conclusions),
                "metadata": metadata,
            },
        },
    }
