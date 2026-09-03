from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from .contracts import FrozenModel, LoopBinding

ARTIFACT_ENDPOINTS = {
    "signal_contract": "/api/v1/knowledge/quant/signal-contracts",
    "fsm_contract": "/api/v1/knowledge/quant/fsm-contracts",
    "golden_case_suite": "/api/v1/knowledge/quant/golden-suites",
}


def config_sha256(payload: dict[str, Any]) -> str:
    material = _hash_ready(payload)
    metadata = material.get("metadata")
    if isinstance(metadata, dict):
        metadata.pop("config_sha256", None)
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _hash_ready(value: Any, *, field_name: str = "") -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, str) and field_name in {"effective_at", "available_at"}:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError(f"{field_name} must be timezone-aware")
        return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, dict):
        return {str(key): _hash_ready(child, field_name=str(key)) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_hash_ready(child) for child in value]
    return value


class ControlArtifactSpec(FrozenModel):
    artifact_type: Literal["signal_contract", "fsm_contract", "golden_case_suite"]
    payload: dict[str, Any]

    @model_validator(mode="after")
    def validate_payload(self):
        if not str(self.payload.get("id") or "").strip():
            raise ValueError("control artifact requires a deterministic id")
        if self.payload.get("market_scope") != "US-equity":
            raise ValueError("control artifact market_scope must be US-equity")
        if self.payload.get("status") != "active":
            raise ValueError("control artifact must be active")
        if self.payload.get("mode") != "PAPER_ONLY":
            raise ValueError("control artifact must be PAPER_ONLY")
        metadata = self.payload.get("metadata")
        if not isinstance(metadata, dict):
            raise ValueError("control artifact metadata is required")
        if metadata.get("allow_order_execution") is not False:
            raise ValueError("control artifact must forbid order execution")
        if metadata.get("production_eligible") is not False:
            raise ValueError("control artifact must not claim production eligibility")
        return self

    @property
    def expected_sha256(self) -> str:
        return config_sha256(self.payload)

    def request_payload(self) -> dict[str, Any]:
        payload = _hash_ready(self.payload)
        payload["metadata"]["config_sha256"] = self.expected_sha256
        return payload


class LoopControlPlaneManifest(FrozenModel):
    schema_version: Literal["ai_quant.loop_control_plane.v1"]
    version: str = Field(min_length=1, max_length=64)
    market_scope: Literal["US-equity"]
    workflow_version_id: str = Field(min_length=1)
    fsm_review_event_type: str = Field(min_length=1)
    golden_actual_results: dict[str, dict[str, Any]] = Field(min_length=1)
    artifacts: tuple[ControlArtifactSpec, ...] = Field(min_length=3, max_length=3)

    @field_validator("artifacts")
    @classmethod
    def unique_types(cls, value: tuple[ControlArtifactSpec, ...]):
        if {item.artifact_type for item in value} != set(ARTIFACT_ENDPOINTS):
            raise ValueError("manifest requires one Signal, FSM and Golden contract")
        return value

    def binding(self) -> LoopBinding:
        indexed = {item.artifact_type: item for item in self.artifacts}
        return LoopBinding(
            workflow_version_id=self.workflow_version_id,
            market_scope=self.market_scope,
            signal_contract_id=str(indexed["signal_contract"].payload["id"]),
            signal_contract_sha256=indexed["signal_contract"].expected_sha256,
            fsm_contract_id=str(indexed["fsm_contract"].payload["id"]),
            fsm_contract_sha256=indexed["fsm_contract"].expected_sha256,
            fsm_review_event_type=self.fsm_review_event_type,
            golden_suite_id=str(indexed["golden_case_suite"].payload["id"]),
            golden_suite_sha256=indexed["golden_case_suite"].expected_sha256,
            golden_actual_results=self.golden_actual_results,
        )


class LoopControlArtifact(FrozenModel):
    id: str
    artifact_type: str
    market_scope: str
    status: str
    effective_at: datetime
    available_at: datetime
    source_run_id: str = ""
    payload: dict[str, Any]
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @field_validator("effective_at", "available_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("control artifact timestamps must be timezone-aware")
        return value
