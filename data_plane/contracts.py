from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SHA256_PATTERN = r"^[0-9a-f]{64}$"


class DatasetRejectedError(RuntimeError):
    """Raised when a critical quality failure makes a snapshot unusable."""


class QualitySeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class DataQualityCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    severity: QualitySeverity
    passed: bool
    observed: str = Field(min_length=1)
    expected: str = Field(min_length=1)
    provenance: str = Field(min_length=1)


class DatasetSnapshot(BaseModel):
    """Immutable identity and quality envelope for one point-in-time dataset."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    asof_utc: datetime
    content_sha256: str = Field(pattern=SHA256_PATTERN)
    schema_version: str = Field(min_length=1)
    row_count: int = Field(ge=0)
    parent_snapshot_ids: tuple[str, ...] = ()
    checks: tuple[DataQualityCheck, ...] = ()

    @field_validator("asof_utc")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("asof_utc must be timezone-aware")
        if value.utcoffset() != timedelta(0):
            raise ValueError("asof_utc must be stored in UTC")
        return value

    @property
    def usable(self) -> bool:
        return not any(
            check.severity is QualitySeverity.CRITICAL and not check.passed
            for check in self.checks
        )

    def assert_usable(self) -> Self:
        if not self.usable:
            failed = [
                check.name
                for check in self.checks
                if check.severity is QualitySeverity.CRITICAL and not check.passed
            ]
            raise DatasetRejectedError(
                f"dataset {self.dataset_id!r} quarantined by critical checks: {failed}"
            )
        return self


class EventObservation(BaseModel):
    """Source evidence, not permission to backdate a ledger write."""

    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")

    source: str = Field(min_length=1)
    source_event_id: str = Field(min_length=1)
    symbols: tuple[str, ...] = Field(min_length=1)
    headline: str | None = None
    summary: str | None = None
    published_at: datetime
    updated_at: datetime | None = None
    first_seen_at: datetime | None = None
    source_url: str | None = None
    source_type: str = Field(default="news", min_length=1)
    origin: Literal["forward", "historical", "manual"]
    # Preserve all remaining canonical catalyst evidence without mutable containers.
    publisher: str | None = None
    event_subtype: str | None = None
    cik: str | None = None
    accession_number: str | None = None
    form_items: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    provenance: str | None = None

    @field_validator("source", "source_event_id", "source_type")
    @classmethod
    def require_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("event source fields must not be blank")
        return value

    @field_validator("symbols")
    @classmethod
    def normalize_symbols(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not symbol.strip() for symbol in value):
            raise ValueError("event symbols must not be blank")
        return tuple(sorted({symbol.strip().upper() for symbol in value}))

    @field_validator("published_at", "updated_at", "first_seen_at")
    @classmethod
    def normalize_time(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("event timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def require_update_order(self) -> Self:
        if self.updated_at is not None and self.updated_at < self.published_at:
            raise ValueError("updated_at must not precede published_at")
        return self


class EventRevision(BaseModel):
    """Immutable source version and its actual point-in-time availability."""

    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")

    event_id: str = Field(pattern=SHA256_PATTERN)
    revision: int = Field(ge=1)
    event_cluster_id: str = Field(pattern=SHA256_PATTERN)
    body_hash: str = Field(pattern=SHA256_PATTERN)
    observation: EventObservation
    recorded_at: datetime
    available_at: datetime
    forward_eligible: bool

    @field_validator("recorded_at", "available_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("ledger timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def require_availability(self) -> Self:
        observation = self.observation
        times = (
            self.recorded_at,
            observation.published_at,
            observation.updated_at,
            observation.first_seen_at,
        )
        if self.available_at < max(value for value in times if value is not None):
            raise ValueError("available_at must cover every evidence timestamp")
        eligible = observation.origin == "forward" and observation.first_seen_at is not None
        if self.forward_eligible != eligible:
            raise ValueError("forward_eligible must match origin and first-seen evidence")
        return self
