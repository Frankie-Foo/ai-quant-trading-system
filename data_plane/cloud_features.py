"""Versioned feature API client plus a local cache for the deterministic fast loop."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from db.migrations.sqlite import SQLiteMigration, apply_sqlite_migrations

CLOUD_FEATURE_API_VERSION = "v1"


class CloudFeatureApiError(RuntimeError):
    """Sanitized remote contract, authorization, or availability failure."""


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RemoteFeatureValue(FrozenModel):
    name: str = Field(min_length=1)
    value: bool | int | float | str | None
    asof_utc: datetime
    definition_version: str = Field(min_length=1)
    provenance: str = Field(min_length=1)

    @field_validator("asof_utc")
    @classmethod
    def utc_only(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("feature timestamp must be UTC")
        return value


class RemoteFeatureVector(FrozenModel):
    symbol: str = Field(min_length=1)
    asof_utc: datetime
    input_event_id: str = Field(min_length=1)
    features: tuple[RemoteFeatureValue, ...] = Field(min_length=1)

    @field_validator("asof_utc")
    @classmethod
    def utc_only(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("feature-vector timestamp must be UTC")
        return value


class FeatureApiEnvelope(FrozenModel):
    api_version: str
    feature_vector: RemoteFeatureVector | None

    @field_validator("api_version")
    @classmethod
    def supported_version(cls, value: str) -> str:
        if value != CLOUD_FEATURE_API_VERSION:
            raise ValueError("unsupported cloud feature API version")
        return value


class CloudFeatureClient:
    """Slow-loop sync client; do not call it from a decision-time kernel function."""

    def __init__(
        self,
        *,
        base_url: str,
        token: SecretStr,
        timeout_seconds: float = 5.0,
        client: httpx.Client | None = None,
    ):
        if not base_url.startswith(("https://", "http://127.0.0.1", "http://localhost")):
            raise ValueError("feature API must use HTTPS outside localhost")
        if not token.get_secret_value().strip() or timeout_seconds <= 0:
            raise ValueError("feature API token and positive timeout are required")
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._client = client or httpx.Client(timeout=timeout_seconds)
        self._owns_client = client is None

    def fetch(self, symbol: str, *, asof_utc: datetime) -> RemoteFeatureVector | None:
        if asof_utc.tzinfo is None or asof_utc.utcoffset() != timedelta(0):
            raise ValueError("asof_utc must be timezone-aware UTC")
        normalized = symbol.strip().upper()
        if not normalized:
            raise ValueError("symbol is required")
        try:
            response = self._client.get(
                f"{self.base_url}/{CLOUD_FEATURE_API_VERSION}/features/{normalized}",
                params={"asof": asof_utc.isoformat()},
                headers={"Authorization": f"Bearer {self._token.get_secret_value()}"},
            )
            response.raise_for_status()
            envelope = FeatureApiEnvelope.model_validate(response.json())
        except (httpx.HTTPError, ValueError) as exc:
            raise CloudFeatureApiError("cloud feature API request failed closed") from exc
        return envelope.feature_vector

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


def _create_cloud_feature_vectors(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS cloud_feature_vectors (
            symbol TEXT NOT NULL,
            asof_utc TEXT NOT NULL,
            input_event_id TEXT NOT NULL,
            api_version TEXT NOT NULL,
            vector_json TEXT NOT NULL,
            fetched_at_utc TEXT NOT NULL,
            PRIMARY KEY (symbol, asof_utc, input_event_id)
        )
        """
    )


CLOUD_FEATURE_CACHE_MIGRATIONS = (
    SQLiteMigration(
        version=1,
        name="cloud_feature_vectors",
        signature="cloud_feature_vectors.v1",
        apply=_create_cloud_feature_vectors,
    ),
)


class CloudFeatureCache:
    """Process-independent local point-in-time cache read by realtime strategy code."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            apply_sqlite_migrations(
                connection,
                owner="data_plane.cloud_feature_cache",
                migrations=CLOUD_FEATURE_CACHE_MIGRATIONS,
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def put(self, vector: RemoteFeatureVector) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO cloud_feature_vectors (
                    symbol, asof_utc, input_event_id, api_version,
                    vector_json, fetched_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    vector.symbol,
                    vector.asof_utc.isoformat(),
                    vector.input_event_id,
                    CLOUD_FEATURE_API_VERSION,
                    vector.model_dump_json(),
                    datetime.now(UTC).isoformat(),
                ),
            )

    def latest(self, symbol: str, *, asof_utc: datetime) -> RemoteFeatureVector | None:
        if asof_utc.tzinfo is None or asof_utc.utcoffset() != timedelta(0):
            raise ValueError("asof_utc must be timezone-aware UTC")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT vector_json FROM cloud_feature_vectors
                WHERE symbol=? AND asof_utc<=?
                ORDER BY asof_utc DESC, input_event_id DESC LIMIT 1
                """,
                (symbol.strip().upper(), asof_utc.isoformat()),
            ).fetchone()
        return None if row is None else RemoteFeatureVector.model_validate_json(str(row[0]))
