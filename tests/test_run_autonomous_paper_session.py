from __future__ import annotations

import pytest

from scripts.run_autonomous_paper_session import (
    _livermore_push,
    direct_paper_credentials,
    resolve_paper_authorization,
)


def test_paper_authorization_requires_cli_env_and_released_kill_switch() -> None:
    assert (
        resolve_paper_authorization(
            arm_paper=False,
            broker_write_enabled=True,
            trading_kill_switch=False,
        )
        is False
    )
    with pytest.raises(RuntimeError, match="BROKER_WRITE_ENABLED"):
        resolve_paper_authorization(
            arm_paper=True,
            broker_write_enabled=False,
            trading_kill_switch=False,
        )
    with pytest.raises(RuntimeError, match="kill switch"):
        resolve_paper_authorization(
            arm_paper=True,
            broker_write_enabled=True,
            trading_kill_switch=True,
        )
    assert (
        resolve_paper_authorization(
            arm_paper=True,
            broker_write_enabled=True,
            trading_kill_switch=False,
        )
        is True
    )


def test_direct_credentials_accept_canonical_names_without_returning_env_map() -> None:
    key_id, secret_key = direct_paper_credentials(
        {
            "ALPACA_PAPER_KEY_ID": "paper-key",
            "ALPACA_PAPER_SECRET_KEY": "paper-secret",
        }
    )

    assert key_id.get_secret_value() == "paper-key"
    assert secret_key.get_secret_value() == "paper-secret"


def test_direct_credentials_fail_closed_when_incomplete() -> None:
    with pytest.raises(RuntimeError, match="credentials"):
        direct_paper_credentials({"ALPACA_PAPER_KEY_ID": "paper-key"})


def test_livermore_push_requires_app_secret_and_exact_channel() -> None:
    with pytest.raises(ValueError, match="secret"):
        _livermore_push(
            {
                "VPS_LIVERMORE_APP_ID": "app-id",
                "VPS_LIVERMORE_CHANNEL_ID": "channel-id",
            }
        )

    client = _livermore_push(
        {
            "VPS_LIVERMORE_APP_ID": "app-id",
            "VPS_LIVERMORE_APP_SECRET": "secret",
            "VPS_LIVERMORE_CHANNEL_ID": "channel-id",
        }
    )
    try:
        assert client.channel_id == "channel-id"
    finally:
        client.close()
