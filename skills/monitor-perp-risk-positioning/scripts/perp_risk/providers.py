"""Read-only public Hyperliquid and Aevo adapters."""

from __future__ import annotations

import json
import math
import time
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx

from .config import BindingConfig, CollectionConfig, HttpConfig
from .models import PerpObservation, ProviderStatus, require_utc


class ProviderError(RuntimeError):
    """Sanitized provider failure that never includes a response body."""


@dataclass(frozen=True)
class ProviderFetch:
    observations: tuple[PerpObservation, ...]
    status: ProviderStatus


class JsonTransport:
    """Small retrying JSON transport with injectable clock/sleep for tests."""

    def __init__(
        self,
        *,
        config: HttpConfig,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._config = config
        self._client = client or httpx.Client(timeout=config.timeout_seconds)
        self._owns_client = client is None
        self._sleep = sleep

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def request_json(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        allow_missing: bool = False,
    ) -> object | None:
        last_error = "request_failed"
        for attempt in range(self._config.max_attempts):
            try:
                response = self._client.request(
                    method,
                    url,
                    content=(
                        None
                        if json_body is None
                        else json.dumps(
                            json_body,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ),
                    params=params,
                    headers=headers,
                )
                if allow_missing and response.status_code in {400, 404, 422}:
                    return None
                if response.status_code == 429 or response.status_code >= 500:
                    last_error = f"http_{response.status_code}"
                    if attempt + 1 < self._config.max_attempts:
                        self._sleep(self._config.initial_backoff_seconds * (2**attempt))
                        continue
                response.raise_for_status()
                return cast(object, response.json())
            except (httpx.HTTPError, OSError, ValueError) as exc:
                last_error = type(exc).__name__
                if attempt + 1 < self._config.max_attempts:
                    self._sleep(self._config.initial_backoff_seconds * (2**attempt))
                    continue
        raise ProviderError(f"public provider request failed: {last_error}")


class HyperliquidClient:
    INFO_URL = "https://api.hyperliquid.xyz/info"

    def __init__(
        self,
        *,
        collection: CollectionConfig,
        http: HttpConfig,
        transport: JsonTransport | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self._collection = collection
        self._transport = transport or JsonTransport(config=http)
        self._owns_transport = transport is None
        self._clock = clock or (lambda: datetime.now(UTC))

    def close(self) -> None:
        if self._owns_transport:
            self._transport.close()

    def fetch(self, bindings: tuple[BindingConfig, ...]) -> ProviderFetch:
        selected = _unique_bindings(item for item in bindings if item.venue == "hyperliquid")
        if not selected:
            return ProviderFetch(
                observations=(),
                status=ProviderStatus(
                    venue="hyperliquid",
                    status="unavailable",
                    observation_count=0,
                    warnings=("not_configured",),
                ),
            )
        grouped: dict[str, list[BindingConfig]] = defaultdict(list)
        for binding in selected:
            grouped[binding.market].append(binding)
        observations: list[PerpObservation] = []
        warnings: list[str] = []
        for market in sorted(grouped):
            try:
                payload = {"type": "metaAndAssetCtxs"}
                if market != "main":
                    payload["dex"] = market
                raw = self._post(payload)
                books: dict[str, object | None] = {}
                trades: dict[str, object | None] = {}
                for binding in grouped[market]:
                    coin = _hyperliquid_coin(market, binding.instrument)
                    books[binding.instrument] = self._post_optional(
                        {"type": "l2Book", "coin": coin},
                        warning=f"{coin}:l2_unavailable",
                        warnings=warnings,
                    )
                    trades[binding.instrument] = self._post_optional(
                        {"type": "recentTrades", "coin": coin},
                        warning=f"{coin}:recent_trades_unavailable",
                        warnings=warnings,
                    )
                observed_at = _clock_utc(self._clock())
                parsed, parse_warnings = self._parse_contexts(
                    raw,
                    market=market,
                    bindings=tuple(grouped[market]),
                    books=books,
                    trades=trades,
                    observed_at=observed_at,
                )
                observations.extend(parsed)
                warnings.extend(parse_warnings)
            except ProviderError:
                warnings.append(f"{market}:context_unavailable")
        return ProviderFetch(
            observations=tuple(sorted(observations, key=lambda item: item.key)),
            status=ProviderStatus(
                venue="hyperliquid",
                status=("unavailable" if not observations else ("partial" if warnings else "ok")),
                observation_count=len(observations),
                warnings=tuple(sorted(set(warnings))),
            ),
        )

    def _post(self, payload: dict[str, str]) -> object:
        raw = self._transport.request_json(
            "POST",
            self.INFO_URL,
            json_body=payload,
            headers={"content-type": "application/json; charset=utf-8"},
        )
        if raw is None:
            raise ProviderError("Hyperliquid returned an empty response")
        return raw

    def _post_optional(
        self,
        payload: dict[str, str],
        *,
        warning: str,
        warnings: list[str],
    ) -> object | None:
        try:
            return self._post(payload)
        except ProviderError:
            warnings.append(warning)
            return None

    def _parse_contexts(
        self,
        raw: object,
        *,
        market: str,
        bindings: tuple[BindingConfig, ...],
        books: dict[str, object | None],
        trades: dict[str, object | None],
        observed_at: datetime,
    ) -> tuple[tuple[PerpObservation, ...], tuple[str, ...]]:
        if (
            not isinstance(raw, list)
            or len(raw) != 2
            or not isinstance(raw[0], dict)
            or not isinstance(raw[1], list)
        ):
            raise ProviderError("Hyperliquid context contract is invalid")
        meta = cast(dict[str, Any], raw[0])
        contexts = cast(list[object], raw[1])
        universe = meta.get("universe")
        if not isinstance(universe, list) or len(universe) != len(contexts):
            raise ProviderError("Hyperliquid universe alignment is invalid")
        by_name: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        for raw_asset, raw_context in zip(universe, contexts, strict=True):
            if not isinstance(raw_asset, dict) or not isinstance(raw_context, dict):
                raise ProviderError("Hyperliquid context row is invalid")
            asset = cast(dict[str, Any], raw_asset)
            context = cast(dict[str, Any], raw_context)
            identity = _hyperliquid_base_name(str(asset.get("name", "")), market)
            if not identity or identity in by_name:
                raise ProviderError("Hyperliquid asset identity is invalid")
            by_name[identity] = asset, context
        parsed: list[PerpObservation] = []
        warnings: list[str] = []
        for binding in bindings:
            pair = by_name.get(binding.instrument)
            if pair is None:
                warnings.append(f"{market}:{binding.instrument}:instrument_unavailable")
                continue
            asset, context = pair
            coin = _hyperliquid_coin(market, binding.instrument)
            try:
                bid, ask = _hyperliquid_bbo(books.get(binding.instrument), coin)
                flow, trade_count = _hyperliquid_flow(
                    trades.get(binding.instrument),
                    expected_coin=coin,
                    observed_at=observed_at,
                    window_seconds=self._collection.flow_window_seconds,
                    minimum_trades=self._collection.minimum_flow_trades,
                )
                evidence = ["metaAndAssetCtxs"]
                if books.get(binding.instrument) is not None:
                    evidence.append("l2Book")
                if trades.get(binding.instrument) is not None:
                    evidence.append("recentTrades")
                parsed.append(
                    PerpObservation(
                        venue="hyperliquid",
                        market=market,
                        instrument=binding.instrument,
                        observed_at_utc=observed_at,
                        mark_price=_required_number(context.get("markPx")),
                        oracle_price=_optional_number(context.get("oraclePx")),
                        reference_price=_optional_number(context.get("prevDayPx")),
                        open_interest=_optional_number(context.get("openInterest")),
                        funding_rate=_optional_number(context.get("funding")),
                        notional_volume_24h=_optional_number(context.get("dayNtlVlm")),
                        bid_price=bid,
                        ask_price=ask,
                        aggressor_imbalance=flow,
                        aggressor_trade_count=trade_count,
                        active=asset.get("isDelisted") is not True,
                        provenance=(
                            f"hyperliquid.info.{'+'.join(evidence)}:"
                            f"{coin}@{observed_at.isoformat()}"
                        ),
                    )
                )
            except (ProviderError, TypeError, ValueError):
                warnings.append(f"{market}:{binding.instrument}:schema_invalid")
        return tuple(parsed), tuple(warnings)


class AevoClient:
    BASE_URL = "https://api.aevo.xyz"

    def __init__(
        self,
        *,
        collection: CollectionConfig,
        http: HttpConfig,
        transport: JsonTransport | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self._collection = collection
        self._transport = transport or JsonTransport(config=http)
        self._owns_transport = transport is None
        self._clock = clock or (lambda: datetime.now(UTC))

    def close(self) -> None:
        if self._owns_transport:
            self._transport.close()

    def fetch(self, bindings: tuple[BindingConfig, ...]) -> ProviderFetch:
        selected = _unique_bindings(item for item in bindings if item.venue == "aevo")
        if not selected:
            return ProviderFetch(
                observations=(),
                status=ProviderStatus(
                    venue="aevo",
                    status="unavailable",
                    observation_count=0,
                    warnings=("not_configured",),
                ),
            )
        observations: list[PerpObservation] = []
        warnings: list[str] = []
        for binding in selected:
            if binding.market != "mainnet":
                raise ProviderError("Aevo public adapter supports only mainnet")
            try:
                observations.append(self._fetch_one(binding, warnings=warnings))
            except ProviderError:
                warnings.append(f"{binding.instrument}:instrument_unavailable")
        return ProviderFetch(
            observations=tuple(sorted(observations, key=lambda item: item.key)),
            status=ProviderStatus(
                venue="aevo",
                status=("unavailable" if not observations else ("partial" if warnings else "ok")),
                observation_count=len(observations),
                warnings=tuple(sorted(set(warnings))),
            ),
        )

    def _fetch_one(
        self,
        binding: BindingConfig,
        *,
        warnings: list[str],
    ) -> PerpObservation:
        asset = binding.instrument.split("-", 1)[0]
        markets = self._get("/markets", params={"asset": asset})
        if not isinstance(markets, list):
            raise ProviderError("Aevo markets contract is invalid")
        matches = [
            cast(dict[str, Any], item)
            for item in markets
            if isinstance(item, dict)
            and str(item.get("instrument_name", "")).upper() == binding.instrument
        ]
        if len(matches) != 1:
            raise ProviderError(f"Aevo instrument unavailable: {binding.instrument}")
        market = matches[0]
        funding = self._get_optional(
            "/funding",
            params={"instrument_name": binding.instrument},
            warning=f"{binding.instrument}:funding_unavailable",
            warnings=warnings,
        )
        request_cutoff = _clock_utc(self._clock())
        book = self._get_optional(
            "/orderbook",
            params={"instrument_name": binding.instrument},
            warning=f"{binding.instrument}:orderbook_unavailable",
            warnings=warnings,
        )
        history = self._get_optional(
            f"/instrument/{binding.instrument}/trade-history",
            params={
                "start_time": str(
                    int(
                        (
                            request_cutoff - timedelta(seconds=self._collection.flow_window_seconds)
                        ).timestamp()
                        * 1_000_000_000
                    )
                ),
                "end_time": str(int(request_cutoff.timestamp() * 1_000_000_000)),
            },
            warning=f"{binding.instrument}:trade_history_unavailable",
            warnings=warnings,
        )
        observed_at = _clock_utc(self._clock())
        bid, ask = _aevo_bbo(book, binding.instrument)
        flow, trade_count = _aevo_flow(
            history,
            expected_instrument=binding.instrument,
            observed_at=observed_at,
            window_seconds=self._collection.flow_window_seconds,
            minimum_trades=self._collection.minimum_flow_trades,
        )
        funding_rate = (
            _optional_number(cast(dict[str, Any], funding).get("funding_rate"))
            if isinstance(funding, dict)
            else None
        )
        evidence = ["markets"]
        if funding is not None:
            evidence.append("funding")
        if book is not None:
            evidence.append("orderbook")
        if history is not None:
            evidence.append("trade-history")
        try:
            return PerpObservation(
                venue="aevo",
                market="mainnet",
                instrument=binding.instrument,
                observed_at_utc=observed_at,
                mark_price=_required_number(market.get("mark_price")),
                oracle_price=_optional_number(market.get("index_price")),
                funding_rate=funding_rate,
                bid_price=bid,
                ask_price=ask,
                aggressor_imbalance=flow,
                aggressor_trade_count=trade_count,
                active=market.get("is_active") is True,
                provenance=(
                    f"aevo.rest.{'+'.join(evidence)}:{binding.instrument}@{observed_at.isoformat()}"
                ),
            )
        except (TypeError, ValueError) as exc:
            raise ProviderError(f"Aevo schema validation failed: {binding.instrument}") from exc

    def _get(self, path: str, *, params: dict[str, str]) -> object:
        raw = self._transport.request_json(
            "GET",
            f"{self.BASE_URL}{path}",
            params=params,
            headers={"accept": "application/json"},
        )
        if raw is None:
            raise ProviderError("Aevo returned an empty response")
        return raw

    def _get_optional(
        self,
        path: str,
        *,
        params: dict[str, str],
        warning: str,
        warnings: list[str],
    ) -> object | None:
        try:
            return self._transport.request_json(
                "GET",
                f"{self.BASE_URL}{path}",
                params=params,
                headers={"accept": "application/json"},
                allow_missing=True,
            )
        except ProviderError:
            warnings.append(warning)
            return None


def _unique_bindings(values: Iterable[BindingConfig]) -> tuple[BindingConfig, ...]:
    selected = tuple(values)
    keys = [item.observation_key for item in selected]
    if len(keys) != len(set(keys)):
        raise ValueError("provider bindings must be unique")
    return selected


def _clock_utc(value: datetime) -> datetime:
    return require_utc(value, name="provider clock")


def _hyperliquid_base_name(raw_name: str, market: str) -> str:
    name = raw_name.strip()
    prefix = f"{market}:"
    if market != "main" and name.lower().startswith(prefix.lower()):
        name = name[len(prefix) :]
    return name.upper()


def _hyperliquid_coin(market: str, instrument: str) -> str:
    return instrument if market == "main" else f"{market}:{instrument}"


def _hyperliquid_bbo(
    raw: object | None,
    expected_coin: str,
) -> tuple[float | None, float | None]:
    if raw is None:
        return None, None
    if not isinstance(raw, dict):
        raise ProviderError("Hyperliquid l2Book contract is invalid")
    values = cast(dict[str, Any], raw)
    if str(values.get("coin", "")).lower() != expected_coin.lower():
        raise ProviderError("Hyperliquid l2Book identity is invalid")
    levels = values.get("levels")
    if (
        not isinstance(levels, list)
        or len(levels) != 2
        or not all(isinstance(side, list) for side in levels)
    ):
        raise ProviderError("Hyperliquid l2Book levels are invalid")
    bids = _hyperliquid_book_prices(cast(list[object], levels[0]))
    asks = _hyperliquid_book_prices(cast(list[object], levels[1]))
    if not bids or not asks:
        return None, None
    bid, ask = max(bids), min(asks)
    if ask <= bid:
        raise ProviderError("Hyperliquid l2Book is crossed")
    return bid, ask


def _hyperliquid_book_prices(rows: list[object]) -> list[float]:
    prices: list[float] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ProviderError("Hyperliquid l2Book row is invalid")
        price = _optional_number(cast(dict[str, Any], row).get("px"))
        if price is None or price <= 0:
            raise ProviderError("Hyperliquid l2Book price is invalid")
        prices.append(price)
    return prices


def _hyperliquid_flow(
    raw: object | None,
    *,
    expected_coin: str,
    observed_at: datetime,
    window_seconds: int,
    minimum_trades: int,
) -> tuple[float | None, int | None]:
    if raw is None:
        return None, None
    if not isinstance(raw, list):
        raise ProviderError("Hyperliquid recentTrades contract is invalid")
    start_ms = int((observed_at - timedelta(seconds=window_seconds)).timestamp() * 1000)
    end_ms = int(observed_at.timestamp() * 1000)
    buy_notional = 0.0
    sell_notional = 0.0
    seen: set[int] = set()
    count = 0
    for raw_trade in raw:
        if not isinstance(raw_trade, dict):
            raise ProviderError("Hyperliquid recentTrades row is invalid")
        trade = cast(dict[str, Any], raw_trade)
        if str(trade.get("coin", "")).lower() != expected_coin.lower():
            continue
        timestamp = _optional_int(trade.get("time"))
        trade_id = _optional_int(trade.get("tid"))
        if (
            timestamp is None
            or not start_ms <= timestamp <= end_ms
            or trade_id is None
            or trade_id in seen
        ):
            continue
        price = _optional_number(trade.get("px"))
        size = _optional_number(trade.get("sz"))
        side = str(trade.get("side", "")).upper()
        if price is None or price <= 0 or size is None or size <= 0 or side not in {"B", "A"}:
            continue
        seen.add(trade_id)
        notional = price * size
        buy_notional += notional if side == "B" else 0
        sell_notional += notional if side == "A" else 0
        count += 1
    return _imbalance(buy_notional, sell_notional, count, minimum_trades)


def _aevo_bbo(
    raw: object | None,
    expected_instrument: str,
) -> tuple[float | None, float | None]:
    if raw is None:
        return None, None
    if not isinstance(raw, dict):
        raise ProviderError("Aevo orderbook contract is invalid")
    values = cast(dict[str, Any], raw)
    identity = str(values.get("instrument_name", "")).upper()
    if identity and identity != expected_instrument:
        raise ProviderError("Aevo orderbook identity is invalid")
    bids = _aevo_book_prices(values.get("bids"))
    asks = _aevo_book_prices(values.get("asks"))
    if not bids or not asks:
        return None, None
    bid, ask = max(bids), min(asks)
    if ask <= bid:
        raise ProviderError("Aevo orderbook is crossed")
    return bid, ask


def _aevo_book_prices(raw: object) -> list[float]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ProviderError("Aevo orderbook levels are invalid")
    prices: list[float] = []
    for row in raw:
        if not isinstance(row, list) or len(row) < 2:
            raise ProviderError("Aevo orderbook row is invalid")
        price = _optional_number(row[0])
        size = _optional_number(row[1])
        if price is not None and price > 0 and size is not None and size > 0:
            prices.append(price)
    return prices


def _aevo_flow(
    raw: object | None,
    *,
    expected_instrument: str,
    observed_at: datetime,
    window_seconds: int,
    minimum_trades: int,
) -> tuple[float | None, int | None]:
    if raw is None:
        return None, None
    if not isinstance(raw, dict):
        raise ProviderError("Aevo trade-history contract is invalid")
    rows = cast(dict[str, Any], raw).get("trade_history")
    if rows is None:
        return None, 0
    if not isinstance(rows, list):
        raise ProviderError("Aevo trade-history rows are invalid")
    start_ns = int((observed_at - timedelta(seconds=window_seconds)).timestamp() * 1_000_000_000)
    end_ns = int(observed_at.timestamp() * 1_000_000_000)
    buy_notional = 0.0
    sell_notional = 0.0
    seen: set[str] = set()
    count = 0
    for raw_trade in rows:
        if not isinstance(raw_trade, dict):
            raise ProviderError("Aevo trade-history row is invalid")
        trade = cast(dict[str, Any], raw_trade)
        if str(trade.get("instrument_name", "")).upper() != expected_instrument:
            continue
        timestamp = _optional_int(trade.get("created_timestamp"))
        trade_id = str(trade.get("trade_id", "")).strip()
        if (
            timestamp is None
            or not start_ns <= timestamp <= end_ns
            or not trade_id
            or trade_id in seen
        ):
            continue
        price = _optional_number(trade.get("price"))
        amount = _optional_number(trade.get("amount"))
        side = str(trade.get("side", "")).lower()
        if (
            price is None
            or price <= 0
            or amount is None
            or amount <= 0
            or side not in {"buy", "sell"}
        ):
            continue
        seen.add(trade_id)
        notional = price * amount
        buy_notional += notional if side == "buy" else 0
        sell_notional += notional if side == "sell" else 0
        count += 1
    return _imbalance(buy_notional, sell_notional, count, minimum_trades)


def _imbalance(
    buy_notional: float,
    sell_notional: float,
    count: int,
    minimum_trades: int,
) -> tuple[float | None, int]:
    total = buy_notional + sell_notional
    if count < minimum_trades or total <= 0:
        return None, count
    return min(max((buy_notional - sell_notional) / total, -1.0), 1.0), count


def _required_number(value: object) -> float:
    result = _optional_number(value)
    if result is None or result <= 0:
        raise ValueError("required number is unavailable")
    return result


def _optional_number(value: object) -> float | None:
    if value is None or isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _optional_int(value: object) -> int | None:
    if value is None or isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
