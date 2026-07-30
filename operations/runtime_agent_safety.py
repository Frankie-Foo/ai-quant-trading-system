"""Strict three-agent health contract for the intraday safety envelope."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

from operations.autonomous_policy_adapter import RuntimeSafetyEnvelope

ASSESSMENT_SCHEMA_VERSION = "runtime_agent_assessment.v1"
PUSH_HEALTH_SCHEMA_VERSION = "push_health_evidence.v1"


class RuntimeAgentRole(StrEnum):
    CATALYST = "catalyst"
    RED_TEAM = "red_team"
    SUPERVISOR = "supervisor"


class RuntimeAgentVerdict(StrEnum):
    CLEAR = "clear"
    BLOCK = "block"
    INSUFFICIENT = "insufficient"


@dataclass(frozen=True)
class RuntimeAgentAssessment:
    trade_date: date
    symbol: str
    role: RuntimeAgentRole
    generated_at_utc: datetime
    expires_at_utc: datetime
    verdict: RuntimeAgentVerdict
    healthy: bool
    negative_news_clear: bool | None
    material_negative: bool
    model_id: str
    prompt_sha256: str
    source_snapshot_ids: tuple[str, ...]
    provenance: str

    def __post_init__(self) -> None:
        _identity_and_window(
            symbol=self.symbol,
            generated_at_utc=self.generated_at_utc,
            expires_at_utc=self.expires_at_utc,
        )
        if not self.model_id.strip():
            raise ValueError("runtime agent model_id is required")
        if re.fullmatch(r"[0-9a-f]{64}", self.prompt_sha256) is None:
            raise ValueError("runtime agent prompt_sha256 must be SHA-256 hex")
        if not self.source_snapshot_ids or any(
            not item.strip() for item in self.source_snapshot_ids
        ):
            raise ValueError("runtime agent source snapshot IDs are required")
        if not self.provenance.strip():
            raise ValueError("runtime agent provenance is required")
        if not self.healthy:
            if (
                self.verdict is not RuntimeAgentVerdict.INSUFFICIENT
                or self.negative_news_clear is not None
                or self.material_negative
            ):
                raise ValueError(
                    "unhealthy agent must publish insufficient with no verdict facts"
                )
            return
        if self.role is RuntimeAgentRole.SUPERVISOR:
            if self.negative_news_clear is not None or self.material_negative:
                raise ValueError(
                    "supervisor cannot publish news or material-negative facts"
                )
            if self.verdict is not RuntimeAgentVerdict.CLEAR:
                raise ValueError("healthy supervisor verdict must be clear")
            return
        if self.verdict is RuntimeAgentVerdict.CLEAR:
            if self.negative_news_clear is not True or self.material_negative:
                raise ValueError(
                    "clear news verdict requires explicit clear and no material negative"
                )
        elif self.verdict is RuntimeAgentVerdict.BLOCK:
            if self.negative_news_clear is not False:
                raise ValueError("blocking news verdict must explicitly reject clearance")
        else:
            if self.negative_news_clear is not None or self.material_negative:
                raise ValueError(
                    "insufficient verdict cannot publish news conclusions"
                )

    def is_current(self, observed_at_utc: datetime) -> bool:
        _require_utc(observed_at_utc, name="observed_at_utc")
        return self.generated_at_utc <= observed_at_utc < self.expires_at_utc


@dataclass(frozen=True)
class PushHealthEvidence:
    generated_at_utc: datetime
    expires_at_utc: datetime
    healthy: bool
    source_snapshot_id: str
    provenance: str

    def __post_init__(self) -> None:
        _identity_and_window(
            symbol="PUSH",
            generated_at_utc=self.generated_at_utc,
            expires_at_utc=self.expires_at_utc,
        )
        if not self.source_snapshot_id.strip():
            raise ValueError("push health source snapshot ID is required")
        if not self.provenance.strip():
            raise ValueError("push health provenance is required")

    def is_current(self, observed_at_utc: datetime) -> bool:
        _require_utc(observed_at_utc, name="observed_at_utc")
        return self.generated_at_utc <= observed_at_utc < self.expires_at_utc


def write_runtime_agent_assessment(
    path: Path,
    assessment: RuntimeAgentAssessment,
) -> None:
    """Persist one validated agent result atomically."""

    _write_atomic_json(
        path,
        {
            "schema_version": ASSESSMENT_SCHEMA_VERSION,
            "trade_date": assessment.trade_date.isoformat(),
            "symbol": assessment.symbol,
            "role": assessment.role.value,
            "generated_at_utc": assessment.generated_at_utc.isoformat(),
            "expires_at_utc": assessment.expires_at_utc.isoformat(),
            "verdict": assessment.verdict.value,
            "healthy": assessment.healthy,
            "negative_news_clear": assessment.negative_news_clear,
            "material_negative": assessment.material_negative,
            "model_id": assessment.model_id,
            "prompt_sha256": assessment.prompt_sha256,
            "source_snapshot_ids": list(assessment.source_snapshot_ids),
            "provenance": assessment.provenance,
        },
    )


def load_runtime_agent_assessment(path: Path) -> RuntimeAgentAssessment:
    values = _read_strict_json(
        path,
        expected={
            "schema_version",
            "trade_date",
            "symbol",
            "role",
            "generated_at_utc",
            "expires_at_utc",
            "verdict",
            "healthy",
            "negative_news_clear",
            "material_negative",
            "model_id",
            "prompt_sha256",
            "source_snapshot_ids",
            "provenance",
        },
        kind="runtime agent assessment",
    )
    if values["schema_version"] != ASSESSMENT_SCHEMA_VERSION:
        raise ValueError("unsupported runtime agent assessment schema")
    _require_bool(values, "healthy", kind="runtime agent assessment")
    _require_bool(values, "material_negative", kind="runtime agent assessment")
    negative_news_clear = values["negative_news_clear"]
    if negative_news_clear is not None and not isinstance(
        negative_news_clear, bool
    ):
        raise ValueError(
            "runtime agent assessment negative_news_clear must be boolean or null"
        )
    source_ids = _string_tuple(
        values["source_snapshot_ids"],
        name="runtime agent assessment source_snapshot_ids",
    )
    return RuntimeAgentAssessment(
        trade_date=date.fromisoformat(_string(values, "trade_date")),
        symbol=_string(values, "symbol"),
        role=RuntimeAgentRole(_string(values, "role")),
        generated_at_utc=datetime.fromisoformat(
            _string(values, "generated_at_utc")
        ),
        expires_at_utc=datetime.fromisoformat(_string(values, "expires_at_utc")),
        verdict=RuntimeAgentVerdict(_string(values, "verdict")),
        healthy=cast(bool, values["healthy"]),
        negative_news_clear=negative_news_clear,
        material_negative=cast(bool, values["material_negative"]),
        model_id=_string(values, "model_id"),
        prompt_sha256=_string(values, "prompt_sha256"),
        source_snapshot_ids=source_ids,
        provenance=_string(values, "provenance"),
    )


def write_push_health_evidence(
    path: Path,
    evidence: PushHealthEvidence,
) -> None:
    """Persist one validated push-health probe atomically."""

    _write_atomic_json(
        path,
        {
            "schema_version": PUSH_HEALTH_SCHEMA_VERSION,
            "generated_at_utc": evidence.generated_at_utc.isoformat(),
            "expires_at_utc": evidence.expires_at_utc.isoformat(),
            "healthy": evidence.healthy,
            "source_snapshot_id": evidence.source_snapshot_id,
            "provenance": evidence.provenance,
        },
    )


def load_push_health_evidence(path: Path) -> PushHealthEvidence:
    values = _read_strict_json(
        path,
        expected={
            "schema_version",
            "generated_at_utc",
            "expires_at_utc",
            "healthy",
            "source_snapshot_id",
            "provenance",
        },
        kind="push health evidence",
    )
    if values["schema_version"] != PUSH_HEALTH_SCHEMA_VERSION:
        raise ValueError("unsupported push health evidence schema")
    _require_bool(values, "healthy", kind="push health evidence")
    return PushHealthEvidence(
        generated_at_utc=datetime.fromisoformat(
            _string(values, "generated_at_utc")
        ),
        expires_at_utc=datetime.fromisoformat(_string(values, "expires_at_utc")),
        healthy=cast(bool, values["healthy"]),
        source_snapshot_id=_string(values, "source_snapshot_id"),
        provenance=_string(values, "provenance"),
    )


def assemble_runtime_safety_envelope(
    *,
    trade_date: date,
    symbol: str,
    observed_at_utc: datetime,
    assessments: tuple[RuntimeAgentAssessment, ...],
    push_health: PushHealthEvidence | None,
) -> RuntimeSafetyEnvelope:
    _require_utc(observed_at_utc, name="observed_at_utc")
    normalized_symbol = symbol.strip().upper()
    if not normalized_symbol or normalized_symbol != symbol:
        raise ValueError("runtime safety symbol must be normalized uppercase")
    by_role: dict[RuntimeAgentRole, list[RuntimeAgentAssessment]] = {
        role: [] for role in RuntimeAgentRole
    }
    identity_invalid = False
    for assessment in assessments:
        if (
            assessment.trade_date != trade_date
            or assessment.symbol != normalized_symbol
        ):
            identity_invalid = True
            continue
        by_role[assessment.role].append(assessment)

    current: dict[RuntimeAgentRole, RuntimeAgentAssessment] = {}
    missing: list[str] = []
    stale: list[str] = []
    duplicates: list[str] = []
    for role in RuntimeAgentRole:
        candidates = by_role[role]
        if not candidates:
            missing.append(role.value)
            continue
        if len(candidates) != 1:
            duplicates.append(role.value)
            continue
        assessment = candidates[0]
        if not assessment.is_current(observed_at_utc):
            stale.append(role.value)
            continue
        current[role] = assessment
    complete = (
        not identity_invalid
        and not missing
        and not stale
        and not duplicates
        and len(current) == len(RuntimeAgentRole)
    )
    agents_healthy = complete and all(
        assessment.healthy for assessment in current.values()
    )
    news_roles = (RuntimeAgentRole.CATALYST, RuntimeAgentRole.RED_TEAM)
    material_negative = any(
        current[role].material_negative
        for role in news_roles
        if role in current
    )
    negative_news_clear = (
        all(current[role].negative_news_clear is True for role in news_roles)
        and not material_negative
        if agents_healthy
        else None
    )
    push_current = bool(
        push_health is not None and push_health.is_current(observed_at_utc)
    )
    push_healthy = bool(
        push_current and push_health is not None and push_health.healthy
    )

    sources = {
        source
        for assessment in current.values()
        for source in assessment.source_snapshot_ids
    }
    if push_health is not None and push_current:
        sources.add(push_health.source_snapshot_id)
    health_material = "|".join(
        (
            trade_date.isoformat(),
            normalized_symbol,
            observed_at_utc.isoformat(),
            f"missing={','.join(sorted(missing)) or 'none'}",
            f"stale={','.join(sorted(stale)) or 'none'}",
            f"duplicates={','.join(sorted(duplicates)) or 'none'}",
            f"identity_invalid={str(identity_invalid).lower()}",
            f"push_current={str(push_current).lower()}",
        )
    )
    health_id = (
        "runtime-agent-health-"
        + hashlib.sha256(health_material.encode()).hexdigest()[:24]
    )
    sources.add(health_id)
    live_expiries = [
        assessment.expires_at_utc for assessment in current.values()
    ]
    if push_health is not None and push_current:
        live_expiries.append(push_health.expires_at_utc)
    expires_at = min(
        [observed_at_utc + timedelta(seconds=30), *live_expiries]
    )
    if expires_at <= observed_at_utc:
        expires_at = observed_at_utc + timedelta(microseconds=1)
    provenance = "|".join(
        (
            "operations.runtime_agent_safety.v1",
            f"missing={','.join(sorted(missing)) or 'none'}",
            f"stale={','.join(sorted(stale)) or 'none'}",
            f"duplicates={','.join(sorted(duplicates)) or 'none'}",
            f"identity_invalid={str(identity_invalid).lower()}",
            f"push_current={str(push_current).lower()}",
        )
    )
    return RuntimeSafetyEnvelope(
        trade_date=trade_date,
        symbol=normalized_symbol,
        generated_at_utc=observed_at_utc,
        expires_at_utc=expires_at,
        negative_news_clear=negative_news_clear,
        material_negative=material_negative,
        agents_healthy=agents_healthy,
        push_healthy=push_healthy,
        source_snapshot_ids=tuple(sorted(sources)),
        provenance=provenance,
    )


def _identity_and_window(
    *,
    symbol: str,
    generated_at_utc: datetime,
    expires_at_utc: datetime,
) -> None:
    _require_utc(generated_at_utc, name="generated_at_utc")
    _require_utc(expires_at_utc, name="expires_at_utc")
    if not symbol or symbol != symbol.strip().upper():
        raise ValueError("runtime evidence symbol must be normalized uppercase")
    ttl = expires_at_utc - generated_at_utc
    if not timedelta(0) < ttl <= timedelta(minutes=5):
        raise ValueError("runtime evidence TTL must be in (0, 5m]")


def _require_utc(value: datetime, *, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")


def _write_atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_strict_json(
    path: Path,
    *,
    expected: set[str],
    kind: str,
) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{kind} is unreadable") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{kind} root must be an object")
    values = cast(dict[str, Any], payload)
    unexpected = set(values) - expected
    missing = expected - set(values)
    if unexpected:
        raise ValueError(f"{kind} has unexpected fields")
    if missing:
        raise ValueError(f"{kind} is missing required fields")
    return values


def _require_bool(values: dict[str, Any], name: str, *, kind: str) -> None:
    if not isinstance(values[name], bool):
        raise ValueError(f"{kind} {name} must be boolean")


def _string(values: dict[str, Any], name: str) -> str:
    value = values[name]
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value


def _string_tuple(value: Any, *, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) for item in value
    ):
        raise ValueError(f"{name} must be strings")
    return tuple(value)
