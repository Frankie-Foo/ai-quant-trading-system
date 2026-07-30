"""Read-only public perpetual-market adapters for sentiment evidence."""

from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime
from typing import Any, cast

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from kernel.cross_asset_sentiment import PerpObservation


class PerpProviderError(RuntimeError):
    """Sanitized external-provider failure without response content."""


class PerpInstrumentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    market: str = Field(min_length=1)
    instrument: str = Field(min_length=1)

    @field_validator("market", "instrument")
    @classmethod
    def normalize_identity(cls, value: str) -> str:
        return value.strip().lower()


class HyperliquidPerpClient:
    """Public read-only adapter pinned to Hyperliquid's info endpoint."""

    INFO_URL = "https://api.hyperliquid.xyz/info"

    def __init__(
        self,
        *,
        info_url: str = INFO_URL,
        client: httpx.Client | None = None,
    ):
        if info_url != self.INFO_URL:
            raise ValueError("Hyperliquid client must use the pinned info endpoint")
        self._client = client or httpx.Client(timeout=20.0)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def fetch(
        self,
        instruments: tuple[PerpInstrumentRequest, ...],
        *,
        observed_at_utc: datetime,
    ) -> tuple[PerpObservation, ...]:
        requests = _unique_requests(instruments)
        grouped: dict[str, list[PerpInstrumentRequest]] = defaultdict(list)
        for item in requests:
            grouped[item.market].append(item)
        observations: list[PerpObservation] = []
        for market in sorted(grouped):
            payload: dict[str, str] = {"type": "metaAndAssetCtxs"}
            if market != "main":
                payload["dex"] = market
            raw = self._post_info(payload)
            observations.extend(
                self._parse_contexts(
                    raw,
                    requested=tuple(grouped[market]),
                    market=market,
                    observed_at_utc=observed_at_utc,
                )
            )
        return tuple(sorted(observations, key=lambda item: item.key))

    def _post_info(self, payload: dict[str, str]) -> object:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        try:
            response = self._client.post(
                self.INFO_URL,
                headers={"content-type": "application/json; charset=utf-8"},
                content=body,
            )
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, OSError, ValueError) as exc:
            raise PerpProviderError(
                f"Hyperliquid info request failed: {type(exc).__name__}"
            ) from exc

    @staticmethod
    def _parse_contexts(
        raw: object,
        *,
        requested: tuple[PerpInstrumentRequest, ...],
        market: str,
        observed_at_utc: datetime,
    ) -> tuple[PerpObservation, ...]:
        if (
            not isinstance(raw, list)
            or len(raw) != 2
            or not isinstance(raw[0], dict)
            or not isinstance(raw[1], list)
        ):
            raise PerpProviderError("Hyperliquid context contract is invalid")
        meta = cast(dict[str, Any], raw[0])
        contexts = cast(list[object], raw[1])
        universe = meta.get("universe")
        if not isinstance(universe, list) or len(universe) != len(contexts):
            raise PerpProviderError("Hyperliquid universe alignment is invalid")
        by_name: dict[str, dict[str, Any]] = {}
        for raw_asset, raw_context in zip(universe, contexts, strict=True):
            if not isinstance(raw_asset, dict) or not isinstance(raw_context, dict):
                raise PerpProviderError("Hyperliquid context row is invalid")
            name = str(raw_asset.get("name", "")).strip().lower()
            if not name or name in by_name:
                raise PerpProviderError("Hyperliquid asset identity is invalid")
            by_name[name] = cast(dict[str, Any], raw_context)
        observations: list[PerpObservation] = []
        for request in requested:
            context = by_name.get(request.instrument)
            if context is None:
                raise PerpProviderError(
                    "Hyperliquid requested instrument was unavailable"
                )
            try:
                observations.append(
                    PerpObservation(
                        venue="hyperliquid",
                        market=market,
                        instrument=request.instrument,
                        observed_at_utc=observed_at_utc,
                        mark_price=_required_number(context.get("markPx")),
                        oracle_price=_optional_number(context.get("oraclePx")),
                        reference_price=_optional_number(
                            context.get("prevDayPx")
                        ),
                        open_interest=_optional_number(
                            context.get("openInterest")
                        ),
                        funding_rate=_optional_number(context.get("funding")),
                        notional_volume_24h=_optional_number(
                            context.get("dayNtlVlm")
                        ),
                        active=True,
                        provenance=(
                            "hyperliquid.info.metaAndAssetCtxs:"
                            f"{market}:{request.instrument}@"
                            f"{observed_at_utc.isoformat()}"
                        ),
                    )
                )
            except (TypeError, ValueError) as exc:
                raise PerpProviderError(
                    "Hyperliquid context row failed schema validation"
                ) from exc
        return tuple(observations)


class AevoPerpClient:
    """Public read-only adapter pinned to Aevo's mainnet REST host."""

    BASE_URL = "https://api.aevo.xyz"

    def __init__(
        self,
        *,
        base_url: str = BASE_URL,
        client: httpx.Client | None = None,
    ):
        if base_url.rstrip("/") != self.BASE_URL:
            raise ValueError("Aevo client must use the pinned mainnet REST host")
        self._client = client or httpx.Client(timeout=20.0)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def fetch(
        self,
        instruments: tuple[PerpInstrumentRequest, ...],
        *,
        observed_at_utc: datetime,
    ) -> tuple[PerpObservation, ...]:
        requests = _unique_requests(instruments)
        if any(item.market != "mainnet" for item in requests):
            raise ValueError("Aevo public adapter supports only mainnet")
        observations = [
            self._fetch_one(item, observed_at_utc=observed_at_utc)
            for item in requests
        ]
        return tuple(sorted(observations, key=lambda item: item.key))

    def _fetch_one(
        self,
        request: PerpInstrumentRequest,
        *,
        observed_at_utc: datetime,
    ) -> PerpObservation:
        asset = request.instrument.split("-", 1)[0].upper()
        markets = self._get_json("/markets", params={"asset": asset})
        if not isinstance(markets, list):
            raise PerpProviderError("Aevo markets response contract is invalid")
        match: dict[str, Any] | None = None
        for raw in markets:
            if not isinstance(raw, dict):
                raise PerpProviderError("Aevo market row is invalid")
            values = cast(dict[str, Any], raw)
            if (
                str(values.get("instrument_name", "")).strip().lower()
                == request.instrument
            ):
                if match is not None:
                    raise PerpProviderError(
                        "Aevo instrument identity was duplicated"
                    )
                match = values
        if match is None:
            raise PerpProviderError("Aevo requested instrument was unavailable")
        funding_payload = self._get_json(
            "/funding",
            params={"instrument_name": request.instrument.upper()},
            allowed_missing=True,
        )
        funding_rate = (
            _optional_number(funding_payload.get("funding_rate"))
            if isinstance(funding_payload, dict)
            else None
        )
        try:
            return PerpObservation(
                venue="aevo",
                market="mainnet",
                instrument=request.instrument,
                observed_at_utc=observed_at_utc,
                mark_price=_required_number(match.get("mark_price")),
                oracle_price=_optional_number(match.get("index_price")),
                funding_rate=funding_rate,
                active=match.get("is_active") is True,
                provenance=(
                    "aevo.rest.markets+funding:"
                    f"{request.instrument}@{observed_at_utc.isoformat()}"
                ),
            )
        except (TypeError, ValueError) as exc:
            raise PerpProviderError(
                "Aevo market row failed schema validation"
            ) from exc

    def _get_json(
        self,
        path: str,
        *,
        params: dict[str, str],
        allowed_missing: bool = False,
    ) -> object:
        try:
            response = self._client.get(
                f"{self.BASE_URL}{path}",
                params=params,
                headers={"accept": "application/json"},
            )
            if allowed_missing and response.status_code in {400, 404, 422}:
                return None
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, OSError, ValueError) as exc:
            raise PerpProviderError(
                f"Aevo public request failed: {type(exc).__name__}"
            ) from exc


def _unique_requests(
    values: tuple[PerpInstrumentRequest, ...],
) -> tuple[PerpInstrumentRequest, ...]:
    if not values:
        raise ValueError("at least one perpetual instrument is required")
    keys = [(item.market, item.instrument) for item in values]
    if len(keys) != len(set(keys)):
        raise ValueError("perpetual instrument requests must be unique")
    return values


def _required_number(value: object) -> float:
    result = _optional_number(value)
    if result is None or result <= 0:
        raise ValueError("required perpetual number is unavailable")
    return result


def _optional_number(value: object) -> float | None:
    if value is None or isinstance(value, bool) or not isinstance(
        value,
        (str, int, float),
    ):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None
