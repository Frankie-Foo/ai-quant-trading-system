"""Secret-safe execution settings with conservative Paper defaults."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from execution.alpaca_paper import PAPER_BASE_URL


class ExecutionSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        populate_by_name=True,
    )

    alpaca_api_key_id: SecretStr = Field(validation_alias="ALPACA_API_KEY_ID")
    alpaca_api_secret_key: SecretStr = Field(validation_alias="ALPACA_API_SECRET_KEY")
    alpaca_trading_base_url: str = Field(
        default=PAPER_BASE_URL,
        validation_alias="ALPACA_TRADING_BASE_URL",
    )
    alpaca_market_data_feed: Literal["sip"] = Field(
        default="sip",
        validation_alias="ALPACA_MARKET_DATA_FEED",
    )
    broker_write_enabled: bool = Field(
        default=False,
        validation_alias="BROKER_WRITE_ENABLED",
    )
    trading_kill_switch: bool = Field(
        default=True,
        validation_alias="TRADING_KILL_SWITCH",
    )

    @field_validator("alpaca_trading_base_url")
    @classmethod
    def paper_only(cls, value: str) -> str:
        if value.rstrip("/") != PAPER_BASE_URL:
            raise ValueError("only the Alpaca Paper Trading endpoint is permitted")
        return PAPER_BASE_URL
