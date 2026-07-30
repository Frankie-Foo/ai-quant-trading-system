"""Strict YAML configuration and deterministic hashing."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from importlib.resources import files
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .models import Scope


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CollectionConfig(StrictModel):
    interval_seconds: int = Field(ge=15, le=3600)
    flow_window_seconds: int = Field(ge=15, le=600)
    minimum_flow_trades: int = Field(ge=1, le=100)
    max_age_seconds: int = Field(gt=0, le=3600)
    max_previous_gap_seconds: int = Field(gt=0, le=3600)


class HttpConfig(StrictModel):
    timeout_seconds: float = Field(gt=0, le=120)
    max_attempts: int = Field(ge=1, le=8)
    initial_backoff_seconds: float = Field(ge=0, le=30)


class ComponentWeights(StrictModel):
    price_trend: float = Field(gt=0, le=1)
    price_oi: float = Field(gt=0, le=1)
    funding: float = Field(gt=0, le=1)
    signed_flow: float = Field(gt=0, le=1)
    liquidation: float = Field(gt=0, le=1)
    basis: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def sum_to_one(self) -> ComponentWeights:
        if abs(sum(self.model_dump().values()) - 1.0) > 1e-9:
            raise ValueError("component weights must sum to 1")
        return self


class PolicyConfig(StrictModel):
    component_weights: ComponentWeights
    price_return_full_scale: float = Field(gt=0)
    open_interest_change_full_scale: float = Field(gt=0)
    basis_full_scale: float = Field(gt=0)
    moderate_funding_abs: float = Field(gt=0)
    extreme_funding_abs: float = Field(gt=0)
    max_abs_basis: float = Field(gt=0)
    default_max_spread_bps: float = Field(gt=0)
    minimum_component_weight: float = Field(gt=0, le=1)
    minimum_target_coverage: float = Field(gt=0, le=1)
    risk_on_threshold: float = Field(ge=0, le=100)
    risk_off_threshold: float = Field(ge=-100, le=0)
    strong_risk_on_threshold: float = Field(ge=0, le=100)
    strong_risk_off_threshold: float = Field(ge=-100, le=0)
    boost_minimum_coverage: float = Field(gt=0, le=1)
    conflict_disagreement_threshold: float = Field(gt=0, le=1)
    confirmation_windows: int = Field(ge=2, le=10)
    unavailable_multiplier: float = Field(ge=0, le=1)
    risk_off_multiplier: float = Field(ge=0, le=1)
    neutral_multiplier: float = Field(ge=0, le=1)
    boost_multiplier: float = Field(ge=1, le=1.2)
    require_liquidation_for_boost: bool
    minimum_liquidation_coverage_for_boost: float = Field(ge=0, le=1)
    require_two_venues_for_boost: bool

    @model_validator(mode="after")
    def validate_threshold_order(self) -> PolicyConfig:
        if self.strong_risk_off_threshold > self.risk_off_threshold:
            raise ValueError("strong risk-off threshold must be more negative")
        if self.strong_risk_on_threshold < self.risk_on_threshold:
            raise ValueError("strong risk-on threshold must be more positive")
        if self.moderate_funding_abs >= self.extreme_funding_abs:
            raise ValueError("extreme funding must exceed moderate funding")
        if self.unavailable_multiplier > self.neutral_multiplier:
            raise ValueError("unavailable multiplier cannot exceed neutral")
        return self


class BindingConfig(StrictModel):
    target_id: str = Field(min_length=1)
    scope: Scope
    venue: Literal["hyperliquid", "aevo"]
    market: str = Field(min_length=1)
    instrument: str = Field(min_length=1)
    weight: float = Field(gt=0)
    polarity: Literal[-1, 1] = 1
    min_notional_volume_24h: float | None = Field(default=None, ge=0)
    max_spread_bps: float | None = Field(default=None, gt=0)
    boost_eligible: bool = True

    @field_validator("target_id", "market", mode="after")
    @classmethod
    def normalize_lower(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("instrument", mode="after")
    @classmethod
    def normalize_instrument(cls, value: str) -> str:
        return value.strip().upper()

    @property
    def observation_key(self) -> tuple[str, str, str]:
        return self.venue, self.market, self.instrument


class TargetConfig(StrictModel):
    target_id: str = Field(min_length=1)
    enabled: bool
    unavailable_reason: str | None = None

    @field_validator("target_id", mode="after")
    @classmethod
    def normalize_target(cls, value: str) -> str:
        return value.strip().lower()

    @model_validator(mode="after")
    def validate_disabled_reason(self) -> TargetConfig:
        if not self.enabled and not self.unavailable_reason:
            raise ValueError("disabled target requires unavailable_reason")
        return self


class SessionConfig(StrictModel):
    timezone: str
    exchange_calendar: str
    actionable_start: str
    actionable_end: Literal["regular_close"]

    @field_validator("actionable_start")
    @classmethod
    def validate_time(cls, value: str) -> str:
        parts = value.split(":")
        if len(parts) != 2:
            raise ValueError("actionable_start must be HH:MM")
        hour, minute = (int(item) for item in parts)
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError("actionable_start must be HH:MM")
        return f"{hour:02d}:{minute:02d}"


class StorageConfig(StrictModel):
    database_path: str | None = None
    latest_json_path: str | None = None
    detail_retention_days: int = Field(ge=7, le=3650)


class LiquidationConfig(StrictModel):
    provider: Literal["none", "jsonl", "http"]
    jsonl_path: str | None = None
    http_url: str | None = None
    secret_header: str
    secret_env: str
    keyring_service: str
    keyring_username: str

    @model_validator(mode="after")
    def validate_provider(self) -> LiquidationConfig:
        if self.provider == "jsonl" and not self.jsonl_path:
            raise ValueError("jsonl liquidation provider requires jsonl_path")
        if self.provider == "http" and not self.http_url:
            raise ValueError("http liquidation provider requires http_url")
        return self


class NotificationConfig(StrictModel):
    enabled: bool
    url: str | None = None
    secret_header: str
    secret_env: str
    keyring_service: str
    keyring_username: str
    heartbeat_seconds: int = Field(ge=60, le=86_400)
    language: Literal["zh", "en"]

    @model_validator(mode="after")
    def validate_url(self) -> NotificationConfig:
        if self.enabled and not self.url:
            raise ValueError("enabled notification requires url")
        return self


class AppConfig(StrictModel):
    schema_version: Literal["perp_risk_config.v1"]
    collection: CollectionConfig
    http: HttpConfig
    policy: PolicyConfig
    bindings: tuple[BindingConfig, ...]
    targets: tuple[TargetConfig, ...]
    session: SessionConfig
    storage: StorageConfig
    liquidation: LiquidationConfig
    notification: NotificationConfig

    @model_validator(mode="after")
    def validate_identity(self) -> AppConfig:
        binding_ids = [
            (item.target_id, item.venue, item.market, item.instrument) for item in self.bindings
        ]
        if len(binding_ids) != len(set(binding_ids)):
            raise ValueError("bindings must be unique")
        target_ids = [item.target_id for item in self.targets]
        if len(target_ids) != len(set(target_ids)):
            raise ValueError("targets must be unique")
        enabled = {item.target_id for item in self.targets if item.enabled}
        bound = {item.target_id for item in self.bindings}
        if not bound <= enabled:
            raise ValueError("bindings may reference only enabled targets")
        scopes: dict[str, Scope] = {}
        for binding in self.bindings:
            prior = scopes.setdefault(binding.target_id, binding.scope)
            if prior is not binding.scope:
                raise ValueError("one target cannot mix scopes")
        return self

    @property
    def config_hash(self) -> str:
        body = json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(body).hexdigest()

    @property
    def database_path(self) -> Path:
        if self.storage.database_path:
            return Path(self.storage.database_path).expanduser().resolve()
        return _user_data_directory() / "perp-risk.sqlite3"

    @property
    def latest_json_path(self) -> Path:
        if self.storage.latest_json_path:
            return Path(self.storage.latest_json_path).expanduser().resolve()
        return self.database_path.with_name("latest.json")


def package_default_config_path() -> Path:
    return Path(str(files("perp_risk").joinpath("default-config.yaml")))


def _user_data_directory() -> Path:
    if sys.platform == "win32":
        base = Path(
            os.environ.get(
                "LOCALAPPDATA",
                str(Path.home() / "AppData" / "Local"),
            )
        )
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(
            os.environ.get(
                "XDG_DATA_HOME",
                str(Path.home() / ".local" / "share"),
            )
        )
    return base.expanduser().resolve() / "monitor-perp-risk-positioning"


def load_config(path: Path | None = None) -> AppConfig:
    source = path or package_default_config_path()
    raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("configuration root must be an object")
    return AppConfig.model_validate(raw)


def write_default_config(path: Path, *, force: bool = False) -> Path:
    target = path.expanduser().resolve()
    if target.exists() and not force:
        raise FileExistsError(f"configuration already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        package_default_config_path().read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    return target
