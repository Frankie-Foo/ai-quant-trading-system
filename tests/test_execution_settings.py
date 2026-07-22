from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from execution.settings import ExecutionSettings


def test_execution_settings_default_to_killed_and_read_only() -> None:
    settings = ExecutionSettings.model_validate(
        {
            "alpaca_api_key_id": SecretStr("key"),
            "alpaca_api_secret_key": SecretStr("secret"),
        }
    )

    assert settings.broker_write_enabled is False
    assert settings.trading_kill_switch is True
    assert settings.alpaca_trading_base_url == "https://paper-api.alpaca.markets"
    assert settings.alpaca_market_data_feed == "sip"


def test_execution_settings_reject_live_endpoint_or_non_sip_feed() -> None:
    with pytest.raises(ValidationError):
        ExecutionSettings.model_validate(
            {
                "alpaca_api_key_id": SecretStr("key"),
                "alpaca_api_secret_key": SecretStr("secret"),
                "alpaca_trading_base_url": "https://api.alpaca.markets",
            }
        )
    with pytest.raises(ValidationError):
        ExecutionSettings.model_validate(
            {
                "alpaca_api_key_id": SecretStr("key"),
                "alpaca_api_secret_key": SecretStr("secret"),
                "alpaca_market_data_feed": "iex",
            }
        )
