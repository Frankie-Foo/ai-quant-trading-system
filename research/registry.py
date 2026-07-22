from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SHA256_PATTERN = r"^[0-9a-f]{64}$"


class RunStatus(StrEnum):
    REGISTERED = "registered"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ResearchSplit(BaseModel):
    """Chronological train/validation/test split with no overlapping windows."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    train_start: date
    train_end: date
    validation_start: date
    validation_end: date
    test_start: date
    test_end: date

    @model_validator(mode="after")
    def enforce_chronology(self) -> Self:
        if not (
            self.train_start <= self.train_end
            < self.validation_start
            <= self.validation_end
            < self.test_start
            <= self.test_end
        ):
            raise ValueError("research windows must be ordered and non-overlapping")
        return self


class ResearchRun(BaseModel):
    """Reproducible registry record for one research or model experiment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID = Field(default_factory=uuid4)
    created_at_utc: datetime
    data_snapshot_ids: tuple[str, ...] = Field(min_length=1)
    feature_set_sha256: str = Field(pattern=SHA256_PATTERN)
    config_sha256: str = Field(pattern=SHA256_PATTERN)
    code_sha256: str = Field(pattern=SHA256_PATTERN)
    random_seed: int
    attempted_configurations: int = Field(ge=1)
    split: ResearchSplit
    status: RunStatus = RunStatus.REGISTERED

    @field_validator("created_at_utc")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at_utc must be timezone-aware")
        if value.utcoffset() != timedelta(0):
            raise ValueError("created_at_utc must be stored in UTC")
        return value

    def manifest_sha256(self) -> str:
        payload = self.model_dump(mode="json")
        encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
