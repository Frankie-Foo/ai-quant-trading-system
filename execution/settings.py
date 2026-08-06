"""Secret-safe execution settings with conservative Paper defaults."""

from __future__ import annotations

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ExecutionSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        populate_by_name=True,
    )

    cloud_platform_base_url: str = Field(
        validation_alias="CLOUD_PLATFORM_BASE_URL",
    )
    cloud_market_data_api_token: SecretStr = Field(
        validation_alias="CLOUD_MARKET_DATA_API_TOKEN",
    )
    cloud_paper_api_token: SecretStr = Field(
        validation_alias="CLOUD_PAPER_API_TOKEN",
    )
    broker_write_enabled: bool = Field(
        default=False,
        validation_alias="BROKER_WRITE_ENABLED",
    )
    trading_kill_switch: bool = Field(
        default=True,
        validation_alias="TRADING_KILL_SWITCH",
    )

    @field_validator("cloud_platform_base_url")
    @classmethod
    def secure_platform_url(cls, value: str) -> str:
        normalized = value.rstrip("/")
        if not normalized.startswith(("https://", "http://127.0.0.1", "http://localhost")):
            raise ValueError("cloud platform API must use HTTPS outside localhost")
        return normalized
