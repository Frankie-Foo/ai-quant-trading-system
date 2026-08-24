"""Load machine-local credentials without putting secrets in the repository."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from dotenv import dotenv_values, load_dotenv
from pydantic import SecretStr

_SHARED_ENV_KEYS = frozenset(
    {
        "MASSIVE_API_KEY",
        "SEC_USER_AGENT",
        "DEEPSEEK_API_KEY",
        "VPS_LIVERMORE_APP_ID",
        "VPS_LIVERMORE_APP_SECRET",
        "VPS_LIVERMORE_CHANNEL_ID",
        "VPS_BUFFETT_APP_ID",
        "VPS_BUFFETT_APP_SECRET",
        "VPS_BUFFETT_CHANNEL_ID",
        "CLOUD_PLATFORM_BASE_URL",
        "CLOUD_MARKET_DATA_API_TOKEN",
        "CLOUD_PAPER_API_TOKEN",
        "CLOUD_FEATURE_API_TOKEN",
        "CLOUD_MARKET_DATA_FEED",
    }
)


def _promote_alias(target: str, *aliases: str) -> None:
    if os.getenv(target, "").strip():
        return
    for alias in aliases:
        value = os.getenv(alias, "").strip()
        if value:
            os.environ[target] = value
            return


def load_project_env(project_root: str | Path) -> None:
    """Load project settings, then an optional user-owned credential file."""

    root = Path(project_root).resolve()
    load_dotenv(root / ".env", override=False)
    shared_env_path = os.getenv("TRADING_SHARED_ENV_FILE", "").strip()
    if shared_env_path:
        for name, value in dotenv_values(Path(shared_env_path).expanduser()).items():
            if name in _SHARED_ENV_KEYS and value and not os.getenv(name, "").strip():
                os.environ[name] = value
    configured_path = os.getenv("ALPACA_ENV_FILE", "").strip()
    external_path = (
        Path(configured_path).expanduser()
        if configured_path
        else Path.home() / "Desktop" / ".gitignore" / "alpaca.env"
    )
    if external_path.is_file():
        load_dotenv(external_path, override=False)

    _promote_alias("ALPACA_API_KEY_ID", "ALPACA_API_KEY", "APCA_API_KEY_ID")
    _promote_alias(
        "ALPACA_API_SECRET_KEY",
        "ALPACA_SECRET_KEY",
        "APCA_API_SECRET_KEY",
    )
    _promote_alias(
        "ALPACA_PAPER_KEY_ID",
        "ALPACA_API_KEY_ID",
        "ALPACA_API_KEY",
        "APCA_API_KEY_ID",
    )
    _promote_alias(
        "ALPACA_PAPER_SECRET_KEY",
        "ALPACA_API_SECRET_KEY",
        "ALPACA_SECRET_KEY",
        "APCA_API_SECRET_KEY",
    )


def project_data_root(project_root: str | Path) -> Path:
    """Resolve the local runtime data root without changing deployment defaults."""

    root = Path(project_root).resolve()
    configured = os.getenv("AI_QUANT_DATA_ROOT", "").strip()
    if not configured:
        return root / "data"
    value = Path(configured).expanduser()
    return value if value.is_absolute() else root / value


def alpaca_paper_credentials(
    environment: Mapping[str, str],
) -> tuple[SecretStr, SecretStr]:
    """Resolve Paper credentials from supported aliases without exposing values."""

    def first(*names: str) -> str | None:
        for name in names:
            value = environment.get(name, "").strip()
            if value:
                return value
        return None

    key_id = first(
        "ALPACA_PAPER_KEY_ID",
        "APCA_API_KEY_ID",
        "ALPACA_API_KEY_ID",
        "ALPACA_API_KEY",
    )
    secret_key = first(
        "ALPACA_PAPER_SECRET_KEY",
        "APCA_API_SECRET_KEY",
        "ALPACA_API_SECRET_KEY",
        "ALPACA_SECRET_KEY",
    )
    if key_id is None or secret_key is None:
        raise RuntimeError("Alpaca Paper credentials are incomplete")
    return SecretStr(key_id), SecretStr(secret_key)
