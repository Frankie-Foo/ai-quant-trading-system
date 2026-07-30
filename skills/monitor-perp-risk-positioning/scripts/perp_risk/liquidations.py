"""Provider-neutral liquidation event ingestion and aggregation."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import Field, field_validator

from .config import HttpConfig, LiquidationConfig
from .models import FrozenModel, PerpObservation, require_utc
from .providers import JsonTransport, ProviderError
from .secrets import resolve_secret


class LiquidationEvent(FrozenModel):
    schema_version: Literal["perp_risk_liquidation_event.v1"] = "perp_risk_liquidation_event.v1"
    event_id: str = Field(min_length=1)
    venue: str = Field(min_length=1)
    market: str = Field(min_length=1)
    instrument: str = Field(min_length=1)
    liquidated_side: Literal["long", "short"]
    notional_usd: float = Field(gt=0)
    observed_at_utc: datetime
    provenance: str = Field(min_length=1)

    @field_validator("venue", "market", mode="after")
    @classmethod
    def normalize_lower(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("instrument", mode="after")
    @classmethod
    def normalize_instrument(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("observed_at_utc")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        return require_utc(value, name="liquidation observed_at_utc")


class LiquidationSource:
    def fetch(
        self,
        *,
        start_utc: datetime,
        end_utc: datetime,
    ) -> tuple[LiquidationEvent, ...]:
        raise NotImplementedError


class NullLiquidationSource(LiquidationSource):
    def fetch(
        self,
        *,
        start_utc: datetime,
        end_utc: datetime,
    ) -> tuple[LiquidationEvent, ...]:
        require_utc(start_utc, name="start_utc")
        require_utc(end_utc, name="end_utc")
        return ()


class JsonlLiquidationSource(LiquidationSource):
    def __init__(self, path: Path):
        self._path = path.expanduser().resolve()

    def fetch(
        self,
        *,
        start_utc: datetime,
        end_utc: datetime,
    ) -> tuple[LiquidationEvent, ...]:
        require_utc(start_utc, name="start_utc")
        require_utc(end_utc, name="end_utc")
        if not self._path.is_file():
            raise ProviderError("liquidation JSONL source is unavailable")
        events: list[LiquidationEvent] = []
        with self._path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    event = LiquidationEvent.model_validate_json(line)
                except ValueError as exc:
                    raise ProviderError(
                        f"liquidation JSONL schema error at line {line_number}"
                    ) from exc
                if start_utc <= event.observed_at_utc <= end_utc:
                    events.append(event)
        return _deduplicate(events)


class HttpLiquidationSource(LiquidationSource):
    def __init__(
        self,
        *,
        config: LiquidationConfig,
        http: HttpConfig,
        transport: JsonTransport | None = None,
    ):
        if not config.http_url:
            raise ValueError("HTTP liquidation source requires a URL")
        self._config = config
        self._transport = transport or JsonTransport(config=http)
        self._owns_transport = transport is None

    def close(self) -> None:
        if self._owns_transport:
            self._transport.close()

    def fetch(
        self,
        *,
        start_utc: datetime,
        end_utc: datetime,
    ) -> tuple[LiquidationEvent, ...]:
        require_utc(start_utc, name="start_utc")
        require_utc(end_utc, name="end_utc")
        headers = {"accept": "application/json"}
        secret = resolve_secret(
            keyring_service=self._config.keyring_service,
            keyring_username=self._config.keyring_username,
            environment_name=self._config.secret_env,
        )
        if secret:
            headers[self._config.secret_header] = secret
        raw = self._transport.request_json(
            "GET",
            cast(str, self._config.http_url),
            params={
                "start_utc": start_utc.isoformat(),
                "end_utc": end_utc.isoformat(),
            },
            headers=headers,
        )
        rows: object
        if isinstance(raw, dict):
            rows = cast(dict[str, Any], raw).get("events")
        else:
            rows = raw
        if not isinstance(rows, list):
            raise ProviderError("liquidation HTTP contract is invalid")
        try:
            events = [LiquidationEvent.model_validate(item) for item in rows]
        except ValueError as exc:
            raise ProviderError("liquidation HTTP event schema is invalid") from exc
        return _deduplicate(item for item in events if start_utc <= item.observed_at_utc <= end_utc)


def build_liquidation_source(
    config: LiquidationConfig,
    http: HttpConfig,
) -> LiquidationSource:
    if config.provider == "none":
        return NullLiquidationSource()
    if config.provider == "jsonl":
        return JsonlLiquidationSource(Path(cast(str, config.jsonl_path)))
    return HttpLiquidationSource(config=config, http=http)


def apply_liquidations(
    observations: tuple[PerpObservation, ...],
    events: tuple[LiquidationEvent, ...],
    *,
    source_complete: bool = False,
) -> tuple[PerpObservation, ...]:
    if not events and not source_complete:
        return observations
    grouped: dict[tuple[str, str, str], list[LiquidationEvent]] = {}
    for event in events:
        grouped.setdefault(
            (event.venue, event.market, event.instrument),
            [],
        ).append(event)
    enriched: list[PerpObservation] = []
    for observation in observations:
        matching = grouped.get(observation.key)
        if not matching and not source_complete:
            enriched.append(observation)
            continue
        matching = matching or []
        long_total = sum(item.notional_usd for item in matching if item.liquidated_side == "long")
        short_total = sum(item.notional_usd for item in matching if item.liquidated_side == "short")
        sources = sorted({item.provenance for item in matching})
        liquidation_provenance = ",".join(sources) if sources else "complete_window:no_events"
        enriched.append(
            observation.model_copy(
                update={
                    "long_liquidation_usd": long_total,
                    "short_liquidation_usd": short_total,
                    "liquidation_event_count": len(matching),
                    "provenance": (
                        f"{observation.provenance}|liquidations:{liquidation_provenance}"
                    ),
                }
            )
        )
    return tuple(enriched)


def _deduplicate(
    events: Iterable[LiquidationEvent],
) -> tuple[LiquidationEvent, ...]:
    result: dict[str, LiquidationEvent] = {}
    for event in events:
        if event.event_id in result:
            raise ProviderError("duplicate liquidation event_id")
        result[event.event_id] = event
    return tuple(
        sorted(
            result.values(),
            key=lambda item: (item.observed_at_utc, item.event_id),
        )
    )
