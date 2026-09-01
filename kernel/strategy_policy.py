"""Versioned, owner-approved selection-policy overrides."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ALLOWED_PARAMETER = "universe.min_rvol"
MINIMUM_RVOL = 2.0
MAXIMUM_RVOL = 8.0


class StrategyPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["strategy_policy.v1"] = "strategy_policy.v1"
    version: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,63}$")
    status: Literal["active", "shadow"]
    created_at_utc: datetime
    previous_version: str | None = None
    source_snapshot_ids: tuple[str, ...] = ()
    parameter_overrides: dict[str, float]
    approved_by: str | None = None
    approved_at_utc: datetime | None = None
    policy_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("created_at_utc", "approved_at_utc")
    @classmethod
    def timestamps_are_utc(cls, value: datetime | None) -> datetime | None:
        if value is not None and (
            value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value)
        ):
            raise ValueError("strategy policy timestamps must be timezone-aware UTC")
        return value

    @model_validator(mode="after")
    def validate_policy(self) -> Self:
        if set(self.parameter_overrides) != {ALLOWED_PARAMETER}:
            raise ValueError("strategy policy parameters must match the allowlisted set")
        min_rvol = self.parameter_overrides[ALLOWED_PARAMETER]
        if not MINIMUM_RVOL <= min_rvol <= MAXIMUM_RVOL:
            raise ValueError(
                f"strategy policy min_rvol must be between {MINIMUM_RVOL} and {MAXIMUM_RVOL}"
            )
        if self.status == "active" and (not self.approved_by or self.approved_at_utc is None):
            raise ValueError("active strategy policy requires owner approval")
        if self.status == "shadow" and (
            self.approved_by is not None or self.approved_at_utc is not None
        ):
            raise ValueError("shadow strategy policy cannot carry production approval")
        if self.policy_hash != strategy_policy_hash(self):
            raise ValueError("strategy policy hash mismatch")
        return self

    @property
    def min_rvol(self) -> float:
        return self.parameter_overrides[ALLOWED_PARAMETER]


def strategy_policy_hash(policy: StrategyPolicy | dict[str, object]) -> str:
    payload = (
        policy.model_dump(mode="python", exclude={"policy_hash"})
        if isinstance(policy, StrategyPolicy)
        else {key: value for key, value in policy.items() if key != "policy_hash"}
    )
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=lambda value: value.isoformat() if isinstance(value, datetime) else str(value),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def build_strategy_policy(
    *,
    version: str,
    status: Literal["active", "shadow"],
    min_rvol: float,
    created_at_utc: datetime,
    previous_version: str | None = None,
    source_snapshot_ids: tuple[str, ...] = (),
    approved_by: str | None = None,
    approved_at_utc: datetime | None = None,
    extra_overrides: dict[str, float] | None = None,
) -> StrategyPolicy:
    overrides = {ALLOWED_PARAMETER: min_rvol, **(extra_overrides or {})}
    payload: dict[str, object] = {
        "schema_version": "strategy_policy.v1",
        "version": version,
        "status": status,
        "created_at_utc": created_at_utc.isoformat(),
        "previous_version": previous_version,
        "source_snapshot_ids": source_snapshot_ids,
        "parameter_overrides": overrides,
        "approved_by": approved_by,
        "approved_at_utc": approved_at_utc.isoformat() if approved_at_utc else None,
    }
    payload["policy_hash"] = strategy_policy_hash(payload)
    return StrategyPolicy.model_validate(payload)


def load_strategy_policy(
    path: str | Path,
    *,
    required_status: Literal["active", "shadow"] | None = None,
) -> StrategyPolicy:
    policy = StrategyPolicy.model_validate_json(Path(path).read_text(encoding="utf-8"))
    if required_status is not None and policy.status != required_status:
        raise ValueError(f"strategy policy must have {required_status} status")
    return policy


def write_strategy_policy(path: str | Path, policy: StrategyPolicy) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(policy.model_dump_json(indent=2), encoding="utf-8")
    temporary.replace(target)
