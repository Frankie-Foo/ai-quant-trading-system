from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from execution.settings import ExecutionSettings


def _settings(**updates: object) -> ExecutionSettings:
    payload = {
        "CLOUD_PLATFORM_BASE_URL": "https://platform.example.com",
        "CLOUD_MARKET_DATA_API_TOKEN": SecretStr("market-token"),
        "CLOUD_PAPER_API_TOKEN": SecretStr("paper-token"),
    }
    payload.update({key.upper(): value for key, value in updates.items()})
    return ExecutionSettings.model_validate(payload)


def test_execution_settings_default_to_killed_and_keyless() -> None:
    settings = _settings()
    assert settings.broker_write_enabled is False
    assert settings.trading_kill_switch is True
    assert settings.cloud_platform_base_url == "https://platform.example.com"
    assert "alpaca" not in " ".join(ExecutionSettings.model_fields)


def test_execution_settings_reject_insecure_remote_platform_url() -> None:
    with pytest.raises(ValidationError):
        _settings(cloud_platform_base_url="http://example.com")
