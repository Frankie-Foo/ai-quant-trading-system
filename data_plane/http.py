from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx


class DownloadError(RuntimeError):
    """Raised when a provider request fails after bounded retries."""


QueryValue = str | int | float | bool | None


def _safe_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def get_response(
    url: str,
    *,
    params: Mapping[str, QueryValue] | None = None,
    headers: Mapping[str, str] | None = None,
    attempts: int = 4,
) -> httpx.Response:
    last_error: Exception | None = None
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        for attempt in range(attempts):
            try:
                response = client.get(url, params=params, headers=headers)
                response.raise_for_status()
                return response
            except (httpx.HTTPError, OSError) as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    retry_after = 0.0
                    if isinstance(exc, httpx.HTTPStatusError):
                        value = exc.response.headers.get("retry-after")
                        if value:
                            try:
                                retry_after = float(value)
                            except ValueError:
                                retry_after = 0.0
                        if exc.response.status_code == 429 and retry_after == 0.0:
                            retry_after = 60.0
                    time.sleep(max(retry_after, min(60.0, float(2**attempt))))
    detail = type(last_error).__name__ if last_error else "unknown error"
    if isinstance(last_error, httpx.HTTPStatusError):
        detail = f"HTTP {last_error.response.status_code}"
    raise DownloadError(f"download failed for {_safe_url(url)}: {detail}")


def get_json(
    url: str,
    *,
    params: Mapping[str, QueryValue] | None = None,
    headers: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    payload = get_response(url, params=params, headers=headers).json()
    if not isinstance(payload, dict):
        raise DownloadError(f"expected JSON object from {url}")
    return payload
