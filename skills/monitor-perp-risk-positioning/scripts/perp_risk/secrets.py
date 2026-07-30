"""Secret lookup with OS keyring priority and environment fallback."""

from __future__ import annotations

import os
from typing import cast


def resolve_secret(
    *,
    keyring_service: str,
    keyring_username: str,
    environment_name: str,
) -> str | None:
    try:
        import keyring

        value = cast(
            str | None,
            keyring.get_password(keyring_service, keyring_username),
        )
        if value:
            return value
    except Exception:
        # Headless containers often have no usable keyring backend.
        pass
    value = os.environ.get(environment_name)
    return value if value else None
