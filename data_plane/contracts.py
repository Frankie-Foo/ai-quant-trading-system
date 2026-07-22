from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator

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
