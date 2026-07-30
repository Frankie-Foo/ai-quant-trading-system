"""Read-only public perpetual-market adapters for sentiment evidence."""

from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
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
        clock: Callable[[], datetime] | None = None,
        flow_window_seconds: int = 60,
        minimum_flow_trades: int = 3,
    ):
        if info_url != self.INFO_URL:
            raise ValueError("Hyperliquid client must use the pinned info endpoint")
        self._client = client or httpx.Client(timeout=20.0)
        self._owns_client = client is None
        self._clock = clock or _utc_now
        self._flow_window_seconds = flow_window_seconds
        self._minimum_flow_trades = minimum_flow_trades

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def fetch(
        self,
        instruments: tuple[PerpInstrumentRequest, ...],
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
            books: dict[str, object | None] = {}
            trades: dict[str, object | None] = {}
            for request in grouped[market]:
                coin = _hyperliquid_coin(request)
                books[request.instrument] = self._post_optional_info(
                    {"type": "l2Book", "coin": coin}
                )
                trades[request.instrument] = self._post_optional_info(
                    {"type": "recentTrades", "coin": coin}
                )
            observed_at_utc = _require_clock_utc(self._clock())
            observations.extend(
                self._parse_contexts(
                    raw,
                    requested=tuple(grouped[market]),
                    market=market,
                    observed_at_utc=observed_at_utc,
                    books=books,
                    trades=trades,
                    flow_window_seconds=self._flow_window_seconds,
                    minimum_flow_trades=self._minimum_flow_trades,
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

    def _post_optional_info(self, payload: dict[str, str]) -> object | None:
        try:
            return self._post_info(payload)
        except PerpProviderError:
            return None

    @staticmethod
    def _parse_contexts(
        raw: object,
        *,
        requested: tuple[PerpInstrumentRequest, ...],
        market: str,
        observed_at_utc: datetime,
        books: dict[str, object | None],
        trades: dict[str, object | None],
        flow_window_seconds: int,
        minimum_flow_trades: int,
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
        by_name: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        for raw_asset, raw_context in zip(universe, contexts, strict=True):
            if not isinstance(raw_asset, dict) or not isinstance(raw_context, dict):
                raise PerpProviderError("Hyperliquid context row is invalid")
            name = str(raw_asset.get("name", "")).strip().lower()
            if not name or name in by_name:
                raise PerpProviderError("Hyperliquid asset identity is invalid")
            by_name[name] = (
                cast(dict[str, Any], raw_asset),
                cast(dict[str, Any], raw_context),
            )
        observations: list[PerpObservation] = []
        for request in requested:
            pair = by_name.get(request.instrument)
            if pair is None:
                raise PerpProviderError(
                    "Hyperliquid requested instrument was unavailable"
                )
            asset, context = pair
            bid_price, ask_price = _hyperliquid_bbo(
                books.get(request.instrument),
                expected_coin=_hyperliquid_coin(request, asset_name=str(asset["name"])),
            )
            aggressor_imbalance, aggressor_trade_count = (
                _hyperliquid_aggressor_imbalance(
                    trades.get(request.instrument),
                    expected_coin=_hyperliquid_coin(
                        request,
                        asset_name=str(asset["name"]),
                    ),
                    observed_at_utc=observed_at_utc,
                    flow_window_seconds=flow_window_seconds,
                    minimum_flow_trades=minimum_flow_trades,
                )
            )
            evidence = ["metaAndAssetCtxs"]
            if books.get(request.instrument) is not None:
                evidence.append("l2Book")
            if trades.get(request.instrument) is not None:
                evidence.append("recentTrades")
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
                        bid_price=bid_price,
                        ask_price=ask_price,
                        aggressor_imbalance=aggressor_imbalance,
                        aggressor_trade_count=aggressor_trade_count,
                        active=asset.get("isDelisted") is not True,
                        provenance=(
                            f"hyperliquid.info.{'+'.join(evidence)}:"
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
        clock: Callable[[], datetime] | None = None,
        flow_window_seconds: int = 60,
        minimum_flow_trades: int = 3,
    ):
        if base_url.rstrip("/") != self.BASE_URL:
            raise ValueError("Aevo client must use the pinned mainnet REST host")
        self._client = client or httpx.Client(timeout=20.0)
        self._owns_client = client is None
        self._clock = clock or _utc_now
        self._flow_window_seconds = flow_window_seconds
        self._minimum_flow_trades = minimum_flow_trades

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def fetch(
        self,
        instruments: tuple[PerpInstrumentRequest, ...],
    ) -> tuple[PerpObservation, ...]:
        requests = _unique_requests(instruments)
        if any(item.market != "mainnet" for item in requests):
            raise ValueError("Aevo public adapter supports only mainnet")
        observations = [
            self._fetch_one(item)
            for item in requests
        ]
        return tuple(sorted(observations, key=lambda item: item.key))

    def _fetch_one(
        self,
        request: PerpInstrumentRequest,
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
        request_cutoff = _require_clock_utc(self._clock())
        orderbook = self._get_optional_json(
            "/orderbook",
            params={"instrument_name": request.instrument.upper()},
        )
        trade_history = self._get_optional_json(
            f"/instrument/{request.instrument.upper()}/trade-history",
            params={
                "start_time": str(
                    int(
                        (
                            request_cutoff
                            - timedelta(seconds=self._flow_window_seconds)
                        ).timestamp()
                        * 1_000_000_000
                    )
                ),
                "end_time": str(
                    int(request_cutoff.timestamp() * 1_000_000_000)
                ),
            },
        )
        observed_at_utc = _require_clock_utc(self._clock())
        bid_price, ask_price = _aevo_bbo(
            orderbook,
            expected_instrument=request.instrument,
        )
        aggressor_imbalance, aggressor_trade_count = (
            _aevo_aggressor_imbalance(
                trade_history,
                expected_instrument=request.instrument,
                observed_at_utc=observed_at_utc,
                flow_window_seconds=self._flow_window_seconds,
                minimum_flow_trades=self._minimum_flow_trades,
            )
        )
        evidence = ["markets", "funding"]
        if orderbook is not None:
            evidence.append("orderbook")
        if trade_history is not None:
            evidence.append("trade-history")
        try:
            return PerpObservation(
                venue="aevo",
                market="mainnet",
                instrument=request.instrument,
                observed_at_utc=observed_at_utc,
                mark_price=_required_number(match.get("mark_price")),
                oracle_price=_optional_number(match.get("index_price")),
                funding_rate=funding_rate,
                bid_price=bid_price,
                ask_price=ask_price,
                aggressor_imbalance=aggressor_imbalance,
                aggressor_trade_count=aggressor_trade_count,
                active=match.get("is_active") is True,
                provenance=(
                    f"aevo.rest.{'+'.join(evidence)}:"
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

    def _get_optional_json(
        self,
        path: str,
        *,
        params: dict[str, str],
    ) -> object | None:
        try:
            return self._get_json(path, params=params, allowed_missing=True)
        except PerpProviderError:
            return None


def _unique_requests(
    values: tuple[PerpInstrumentRequest, ...],
) -> tuple[PerpInstrumentRequest, ...]:
    if not values:
        raise ValueError("at least one perpetual instrument is required")
    keys = [(item.market, item.instrument) for item in values]
    if len(keys) != len(set(keys)):
        raise ValueError("perpetual instrument requests must be unique")
    return values


def _hyperliquid_coin(
    request: PerpInstrumentRequest,
    *,
    asset_name: str | None = None,
) -> str:
    name = asset_name or request.instrument.upper()
    return name if request.market == "main" else f"{request.market}:{name}"


def _hyperliquid_bbo(
    raw: object | None,
    *,
    expected_coin: str,
) -> tuple[float | None, float | None]:
    if raw is None:
        return None, None
    if not isinstance(raw, dict):
        raise PerpProviderError("Hyperliquid l2Book contract is invalid")
    values = cast(dict[str, Any], raw)
    if str(values.get("coin", "")).lower() != expected_coin.lower():
        raise PerpProviderError("Hyperliquid l2Book identity is invalid")
    levels = values.get("levels")
    if (
        not isinstance(levels, list)
        or len(levels) != 2
        or not all(isinstance(side, list) for side in levels)
    ):
        raise PerpProviderError("Hyperliquid l2Book levels are invalid")
    bids = _book_prices(cast(list[object], levels[0]))
    asks = _book_prices(cast(list[object], levels[1]))
    if not bids or not asks:
        return None, None
    bid = max(bids)
    ask = min(asks)
    if ask <= bid:
        raise PerpProviderError("Hyperliquid l2Book is crossed")
    return bid, ask


def _book_prices(rows: list[object]) -> list[float]:
    prices: list[float] = []
    for row in rows:
        if not isinstance(row, dict):
            raise PerpProviderError("Hyperliquid l2Book row is invalid")
        price = _optional_number(cast(dict[str, Any], row).get("px"))
        if price is None or price <= 0:
            raise PerpProviderError("Hyperliquid l2Book price is invalid")
        prices.append(price)
    return prices


def _hyperliquid_aggressor_imbalance(
    raw: object | None,
    *,
    expected_coin: str,
    observed_at_utc: datetime,
    flow_window_seconds: int,
    minimum_flow_trades: int,
) -> tuple[float | None, int | None]:
    if raw is None:
        return None, None
    if not isinstance(raw, list):
        raise PerpProviderError("Hyperliquid recentTrades contract is invalid")
    start_ms = int(
        (observed_at_utc - timedelta(seconds=flow_window_seconds)).timestamp()
        * 1_000
    )
    end_ms = int(observed_at_utc.timestamp() * 1_000)
    buy_notional = 0.0
    sell_notional = 0.0
    count = 0
    seen: set[int] = set()
    for raw_trade in raw:
        if not isinstance(raw_trade, dict):
            raise PerpProviderError("Hyperliquid recentTrades row is invalid")
        trade = cast(dict[str, Any], raw_trade)
        if str(trade.get("coin", "")).lower() != expected_coin.lower():
            continue
        timestamp = _optional_int(trade.get("time"))
        trade_id = _optional_int(trade.get("tid"))
        if (
            timestamp is None
            or timestamp < start_ms
            or timestamp > end_ms
            or trade_id is None
            or trade_id in seen
        ):
            continue
        price = _optional_number(trade.get("px"))
        size = _optional_number(trade.get("sz"))
        side = str(trade.get("side", "")).upper()
        if price is None or price <= 0 or size is None or size <= 0:
            continue
        if side not in {"B", "A"}:
            continue
        seen.add(trade_id)
        notional = price * size
        if side == "B":
            buy_notional += notional
        else:
            sell_notional += notional
        count += 1
    return _imbalance(buy_notional, sell_notional, count, minimum_flow_trades)


def _aevo_bbo(
    raw: object | None,
    *,
    expected_instrument: str,
) -> tuple[float | None, float | None]:
    if raw is None:
        return None, None
    if not isinstance(raw, dict):
        raise PerpProviderError("Aevo orderbook contract is invalid")
    values = cast(dict[str, Any], raw)
    identity = str(values.get("instrument_name", "")).strip().lower()
    if identity and identity != expected_instrument:
        raise PerpProviderError("Aevo orderbook identity is invalid")
    bids = _aevo_book_prices(values.get("bids"))
    asks = _aevo_book_prices(values.get("asks"))
    if not bids or not asks:
        return None, None
    bid = max(bids)
    ask = min(asks)
    if ask <= bid:
        raise PerpProviderError("Aevo orderbook is crossed")
    return bid, ask


def _aevo_book_prices(raw: object) -> list[float]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise PerpProviderError("Aevo orderbook levels are invalid")
    prices: list[float] = []
    for row in raw:
        if not isinstance(row, list) or len(row) < 2:
            raise PerpProviderError("Aevo orderbook row is invalid")
        price = _optional_number(row[0])
        size = _optional_number(row[1])
        if price is None or price <= 0 or size is None or size <= 0:
            continue
        prices.append(price)
    return prices


def _aevo_aggressor_imbalance(
    raw: object | None,
    *,
    expected_instrument: str,
    observed_at_utc: datetime,
    flow_window_seconds: int,
    minimum_flow_trades: int,
) -> tuple[float | None, int | None]:
    if raw is None:
        return None, None
    if not isinstance(raw, dict):
        raise PerpProviderError("Aevo trade-history contract is invalid")
    rows = cast(dict[str, Any], raw).get("trade_history")
    if rows is None:
        return None, 0
    if not isinstance(rows, list):
        raise PerpProviderError("Aevo trade-history rows are invalid")
    start_ns = int(
        (observed_at_utc - timedelta(seconds=flow_window_seconds)).timestamp()
        * 1_000_000_000
    )
    end_ns = int(observed_at_utc.timestamp() * 1_000_000_000)
    buy_notional = 0.0
    sell_notional = 0.0
    count = 0
    seen: set[str] = set()
    for raw_trade in rows:
        if not isinstance(raw_trade, dict):
            raise PerpProviderError("Aevo trade-history row is invalid")
        trade = cast(dict[str, Any], raw_trade)
        identity = str(trade.get("instrument_name", "")).strip().lower()
        if identity != expected_instrument:
            continue
        timestamp = _optional_int(trade.get("created_timestamp"))
        trade_id = str(trade.get("trade_id", "")).strip()
        if (
            timestamp is None
            or timestamp < start_ns
            or timestamp > end_ns
            or not trade_id
            or trade_id in seen
        ):
            continue
        price = _optional_number(trade.get("price"))
        amount = _optional_number(trade.get("amount"))
        side = str(trade.get("side", "")).strip().lower()
        if price is None or price <= 0 or amount is None or amount <= 0:
            continue
        if side not in {"buy", "sell"}:
            continue
        seen.add(trade_id)
        notional = price * amount
        if side == "buy":
            buy_notional += notional
        else:
            sell_notional += notional
        count += 1
    return _imbalance(buy_notional, sell_notional, count, minimum_flow_trades)


def _imbalance(
    buy_notional: float,
    sell_notional: float,
    count: int,
    minimum_flow_trades: int,
) -> tuple[float | None, int]:
    total = buy_notional + sell_notional
    if count < minimum_flow_trades or total <= 0:
        return None, count
    value = (buy_notional - sell_notional) / total
    return min(max(value, -1.0), 1.0), count


def _optional_int(value: object) -> int | None:
    if (
        value is None
        or isinstance(value, bool)
        or not isinstance(value, (str, int, float))
    ):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _require_clock_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("perpetual provider clock must return UTC")
    return value


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
