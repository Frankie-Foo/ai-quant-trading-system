"""Keyless Paper Broker client for the isolated cloud platform API."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

PLATFORM_API_VERSION = "v1"


class BrokerError(RuntimeError):
    """Sanitized broker failure that never includes credentials or response bodies."""


class BrokerWritesDisabledError(BrokerError):
    """Raised before any network call when broker writes are not explicitly armed."""


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class PaperAccount(FrozenModel):
    status: str
    account_blocked: bool
    trading_blocked: bool
    equity: str
    last_equity: str
    buying_power: str


class PaperPosition(FrozenModel):
    symbol: str
    qty: str
    side: str
    market_value: str
    avg_entry_price: str | None = None
    current_price: str | None = None

    @field_validator("symbol")
    @classmethod
    def normalize_position_symbol(cls, value: str) -> str:
        return value.strip().upper()


class BrokerOrder(FrozenModel):
    id: str
    client_order_id: str
    symbol: str
    qty: int = Field(gt=0)
    filled_qty: str
    status: str
    filled_avg_price: str | None = None
    side: str | None = None
    order_type: str | None = Field(default=None, alias="type")
    legs: tuple[BrokerOrder, ...] = ()

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return value.strip().upper()


class PaperOrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    client_order_id: str = Field(min_length=1, max_length=128)
    symbol: str = Field(min_length=1, max_length=16, pattern=r"^[A-Z][A-Z0-9.-]*$")
    qty: int = Field(gt=0)
    side: Literal["buy"] = "buy"
    order_type: Literal["market", "limit"] = "market"
    time_in_force: Literal["day"] = "day"
    extended_hours: Literal[False] = False
    limit_price: str | None = None
    take_profit_price: str | None = Field(default=None, min_length=1)
    stop_loss_price: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def require_stop_for_take_profit(self) -> PaperOrderRequest:
        if self.take_profit_price is not None and self.stop_loss_price is None:
            raise ValueError("take-profit entry requires stop_loss_price")
        return self

    def broker_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "client_order_id": self.client_order_id,
            "symbol": self.symbol,
            "qty": str(self.qty),
            "side": self.side,
            "type": self.order_type,
            "time_in_force": self.time_in_force,
            "extended_hours": self.extended_hours,
        }
        if self.stop_loss_price is not None:
            payload["order_class"] = (
                "bracket" if self.take_profit_price is not None else "oto"
            )
            payload["stop_loss"] = {"stop_price": self.stop_loss_price}
            if self.take_profit_price is not None:
                payload["take_profit"] = {"limit_price": self.take_profit_price}
        if self.order_type == "limit":
            if self.limit_price is None:
                raise ValueError("limit order requires limit_price")
            payload["limit_price"] = self.limit_price
        elif self.limit_price is not None:
            raise ValueError("market order cannot include limit_price")
        return payload


@dataclass(frozen=True)
class FreshNbboQuote:
    symbol: str
    bid: Decimal
    ask: Decimal
    asof_utc: datetime
    feed: str

    def __post_init__(self) -> None:
        if not self.symbol or self.symbol != self.symbol.strip().upper():
            raise ValueError("NBBO symbol must be normalized uppercase")
        if self.bid <= 0 or self.ask < self.bid:
            raise ValueError("NBBO prices are invalid")
        if self.asof_utc.tzinfo is None or self.asof_utc.utcoffset() != UTC.utcoffset(
            self.asof_utc
        ):
            raise ValueError("NBBO timestamp must be UTC")


class ProtectedPaperEntryRequest(BaseModel):
    """Regular-hours Paper entry that is protected atomically at submission."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    client_order_id: str = Field(min_length=1, max_length=128)
    symbol: str = Field(min_length=1, max_length=16, pattern=r"^[A-Z][A-Z0-9.-]*$")
    qty: int = Field(gt=0)
    limit_price: str = Field(min_length=1)
    stop_loss_price: str = Field(min_length=1)
    take_profit_price: str = Field(min_length=1)
    side: Literal["buy"] = "buy"
    order_type: Literal["limit"] = "limit"
    time_in_force: Literal["day"] = "day"
    extended_hours: Literal[False] = False

    def broker_payload(self) -> dict[str, object]:
        return {
            "client_order_id": self.client_order_id,
            "symbol": self.symbol,
            "qty": str(self.qty),
            "side": self.side,
            "type": self.order_type,
            "time_in_force": self.time_in_force,
            "extended_hours": self.extended_hours,
            "limit_price": self.limit_price,
            "order_class": "bracket",
            "stop_loss": {"stop_price": self.stop_loss_price},
            "take_profit": {"limit_price": self.take_profit_price},
        }


def build_protected_entry(
    *,
    client_order_id: str,
    symbol: str,
    qty: int,
    signal_reference: Decimal,
    structural_stop: Decimal,
    quote: FreshNbboQuote,
    observed_at_utc: datetime,
    maximum_spread_or_slippage: Decimal = Decimal("0.001"),
    maximum_quote_age_seconds: Decimal = Decimal("2"),
    maximum_all_in_stop: Decimal = Decimal("0.02"),
    stop_slippage_reserve: Decimal = Decimal("0"),
    target_r: Decimal = Decimal("3"),
) -> ProtectedPaperEntryRequest:
    """Build one marketable limit bracket from an immediate Alpaca SIP NBBO."""
    if observed_at_utc.tzinfo is None or observed_at_utc.utcoffset() != UTC.utcoffset(
        observed_at_utc
    ):
        raise ValueError("entry observation timestamp must be UTC")
    normalized_symbol = symbol.strip().upper()
    if quote.symbol != normalized_symbol:
        raise ValueError("NBBO symbol does not match entry symbol")
    if quote.feed.strip().lower() != "sip":
        raise ValueError("Alpaca SIP NBBO is required for Paper entry")
    age_seconds = Decimal(str((observed_at_utc - quote.asof_utc).total_seconds()))
    if age_seconds < 0 or age_seconds > maximum_quote_age_seconds:
        raise ValueError("immediate NBBO is stale")
    midpoint = (quote.bid + quote.ask) / Decimal(2)
    spread = (quote.ask - quote.bid) / midpoint
    if spread > maximum_spread_or_slippage:
        raise ValueError("immediate NBBO spread exceeds 0.10%")
    if signal_reference <= 0 or quote.ask > signal_reference * (
        Decimal(1) + maximum_spread_or_slippage
    ):
        raise ValueError("immediate NBBO slippage exceeds 0.10%")
    if structural_stop <= 0 or structural_stop >= quote.ask:
        raise ValueError("protective stop must be below the entry ask")
    all_in_stop = (quote.ask - structural_stop) / quote.ask + stop_slippage_reserve
    if all_in_stop > maximum_all_in_stop:
        raise ValueError("all-in stop exceeds 2%")
    limit_price = _price_text(quote.ask, rounding=ROUND_CEILING)
    stop_price = _price_text(structural_stop, rounding=ROUND_FLOOR)
    target = quote.ask + target_r * (quote.ask - structural_stop)
    target_price = _price_text(target, rounding=ROUND_CEILING)
    return ProtectedPaperEntryRequest(
        client_order_id=client_order_id,
        symbol=normalized_symbol,
        qty=qty,
        limit_price=limit_price,
        stop_loss_price=stop_price,
        take_profit_price=target_price,
    )


def _price_text(value: Decimal, *, rounding: str) -> str:
    tick = Decimal("0.0001") if value < 1 else Decimal("0.01")
    return format(value.quantize(tick, rounding=rounding), "f")


class PaperCloseRequest(BaseModel):
    """Long-position time exit; quantity is explicit so it cannot open a short."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    client_order_id: str = Field(min_length=1, max_length=128)
    symbol: str = Field(min_length=1, max_length=16, pattern=r"^[A-Z][A-Z0-9.-]*$")
    qty: int = Field(gt=0)
    side: Literal["sell"] = "sell"
    order_type: Literal["market"] = "market"
    time_in_force: Literal["day"] = "day"
    extended_hours: Literal[False] = False

    def broker_payload(self) -> dict[str, object]:
        return {
            "client_order_id": self.client_order_id,
            "symbol": self.symbol,
            "qty": str(self.qty),
            "side": self.side,
            "type": self.order_type,
            "time_in_force": self.time_in_force,
            "extended_hours": self.extended_hours,
        }


class PaperStopRequest(BaseModel):
    """Regular-hours protective stop that can only reduce a whole-share long."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    client_order_id: str = Field(min_length=1, max_length=128)
    symbol: str = Field(min_length=1, max_length=16, pattern=r"^[A-Z][A-Z0-9.-]*$")
    qty: int = Field(gt=0)
    stop_price: str = Field(min_length=1)
    side: Literal["sell"] = "sell"
    order_type: Literal["stop"] = "stop"
    time_in_force: Literal["day"] = "day"
    extended_hours: Literal[False] = False

    def broker_payload(self) -> dict[str, object]:
        return {
            "client_order_id": self.client_order_id,
            "symbol": self.symbol,
            "qty": str(self.qty),
            "side": self.side,
            "type": self.order_type,
            "time_in_force": self.time_in_force,
            "extended_hours": self.extended_hours,
            "stop_price": self.stop_price,
        }


class PaperExtendedLimitRequest(BaseModel):
    """Extended-hours limit order; sell quantity may only close a long position."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    client_order_id: str = Field(min_length=1, max_length=128)
    symbol: str = Field(min_length=1, max_length=16, pattern=r"^[A-Z][A-Z0-9.-]*$")
    qty: int = Field(gt=0)
    side: Literal["buy", "sell"]
    order_type: Literal["limit"] = "limit"
    time_in_force: Literal["day"] = "day"
    extended_hours: Literal[True] = True
    limit_price: str = Field(min_length=1)

    def broker_payload(self) -> dict[str, object]:
        return {
            "client_order_id": self.client_order_id,
            "symbol": self.symbol,
            "qty": str(self.qty),
            "side": self.side,
            "type": self.order_type,
            "time_in_force": self.time_in_force,
            "extended_hours": self.extended_hours,
            "limit_price": self.limit_price,
        }


class CloudPaperBroker:
    broker_identity = "cloud.paper"

    def __init__(
        self,
        *,
        base_url: str,
        token: SecretStr,
        writes_enabled: bool = False,
        client: httpx.Client | None = None,
    ):
        normalized = base_url.rstrip("/")
        if not normalized.startswith(("https://", "http://127.0.0.1", "http://localhost")):
            raise ValueError("cloud platform API must use HTTPS outside localhost")
        if not token.get_secret_value().strip():
            raise ValueError("cloud Paper API token is required")
        self.base_url = normalized
        self.writes_enabled = writes_enabled
        self._headers = {"Authorization": f"Bearer {token.get_secret_value()}"}
        self._client = client or httpx.Client(timeout=20.0)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def get_account(self) -> PaperAccount:
        payload = self._request("GET", f"/{PLATFORM_API_VERSION}/paper/account")
        return PaperAccount.model_validate(payload.get("account"))

    def get_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        payload = self._request(
            "GET",
            f"/{PLATFORM_API_VERSION}/paper/orders/by-client-id",
            params={"client_order_id": client_order_id},
        )
        order = payload.get("order")
        return None if order is None else BrokerOrder.model_validate(order)

    def list_positions(self) -> tuple[PaperPosition, ...]:
        payload = self._request("GET", f"/{PLATFORM_API_VERSION}/paper/positions")
        positions = payload.get("positions")
        if not isinstance(positions, list):
            raise BrokerError("broker positions response was not a list")
        return tuple(PaperPosition.model_validate(item) for item in positions)

    def list_open_orders(self) -> tuple[BrokerOrder, ...]:
        payload = self._request("GET", f"/{PLATFORM_API_VERSION}/paper/orders/open")
        orders = payload.get("orders")
        if not isinstance(orders, list):
            raise BrokerError("broker open-orders response was not a list")
        return tuple(BrokerOrder.model_validate(item) for item in orders)

    def cancel_order(self, order_id: str) -> bool:
        if not self.writes_enabled:
            raise BrokerWritesDisabledError("paper broker writes are disabled")
        payload = self._request("DELETE", f"/{PLATFORM_API_VERSION}/paper/orders/cancel/{order_id}")
        return payload.get("cancelled") is True

    def submit_order_idempotent(self, request: PaperOrderRequest) -> BrokerOrder:
        if not self.writes_enabled:
            raise BrokerWritesDisabledError("paper broker writes are disabled")
        payload = self._request(
            "POST",
            f"/{PLATFORM_API_VERSION}/paper/orders",
            json_body={"kind": "entry", "request": request.model_dump(mode="json")},
        )
        return BrokerOrder.model_validate(payload.get("order"))

    def submit_protected_entry_idempotent(
        self,
        request: ProtectedPaperEntryRequest,
    ) -> BrokerOrder:
        if not self.writes_enabled:
            raise BrokerWritesDisabledError("paper broker writes are disabled")
        payload = self._request(
            "POST",
            f"/{PLATFORM_API_VERSION}/paper/orders",
            json_body={"kind": "entry", "request": request.model_dump(mode="json")},
        )
        return BrokerOrder.model_validate(payload.get("order"))

    def submit_close_order_idempotent(self, request: PaperCloseRequest) -> BrokerOrder:
        if not self.writes_enabled:
            raise BrokerWritesDisabledError("paper broker writes are disabled")
        payload = self._request(
            "POST",
            f"/{PLATFORM_API_VERSION}/paper/orders",
            json_body={"kind": "close", "request": request.model_dump(mode="json")},
        )
        return BrokerOrder.model_validate(payload.get("order"))

    def submit_stop_order_idempotent(
        self,
        request: PaperStopRequest,
    ) -> BrokerOrder:
        if not self.writes_enabled:
            raise BrokerWritesDisabledError("paper broker writes are disabled")
        payload = self._request(
            "POST",
            f"/{PLATFORM_API_VERSION}/paper/orders",
            json_body={
                "kind": "protective_stop",
                "request": request.model_dump(mode="json"),
            },
        )
        return BrokerOrder.model_validate(payload.get("order"))

    def submit_extended_limit_idempotent(self, request: PaperExtendedLimitRequest) -> BrokerOrder:
        if not self.writes_enabled:
            raise BrokerWritesDisabledError("paper broker writes are disabled")
        payload = self._request(
            "POST",
            f"/{PLATFORM_API_VERSION}/paper/orders",
            json_body={
                "kind": "extended_limit",
                "request": request.model_dump(mode="json"),
            },
        )
        return BrokerOrder.model_validate(payload.get("order"))

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict[str, object] | None = None,
    ) -> dict[str, object]:
        try:
            response = self._client.request(
                method,
                f"{self.base_url}{path}",
                headers=self._headers,
                params=params,
                json=json_body,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, OSError, ValueError) as exc:
            raise BrokerError(f"broker request failed: {type(exc).__name__}") from exc
        if not isinstance(payload, dict) or payload.get("api_version") != PLATFORM_API_VERSION:
            raise BrokerError("broker API response contract is invalid")
        return payload


class DirectAlpacaPaperBroker:
    """Temporary direct adapter pinned to Alpaca's Paper-only trading host."""

    broker_identity = "alpaca.paper.direct"
    PAPER_BASE_URL = "https://paper-api.alpaca.markets"

    def __init__(
        self,
        *,
        key_id: SecretStr,
        secret_key: SecretStr,
        writes_enabled: bool = False,
        base_url: str = PAPER_BASE_URL,
        client: httpx.Client | None = None,
    ):
        normalized = base_url.rstrip("/")
        if normalized != self.PAPER_BASE_URL:
            raise ValueError("direct Alpaca adapter must use the Paper host")
        if not key_id.get_secret_value().strip():
            raise ValueError("Alpaca Paper key ID is required")
        if not secret_key.get_secret_value().strip():
            raise ValueError("Alpaca Paper secret key is required")
        self.base_url = normalized
        self.writes_enabled = writes_enabled
        self._headers = {
            "APCA-API-KEY-ID": key_id.get_secret_value(),
            "APCA-API-SECRET-KEY": secret_key.get_secret_value(),
        }
        self._client = client or httpx.Client(timeout=20.0)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def get_account(self) -> PaperAccount:
        payload = self._request_object("GET", "/v2/account")
        return PaperAccount.model_validate(payload)

    def list_positions(self) -> tuple[PaperPosition, ...]:
        payload = self._request_list("GET", "/v2/positions")
        return tuple(PaperPosition.model_validate(item) for item in payload)

    def list_open_orders(self) -> tuple[BrokerOrder, ...]:
        payload = self._request_list(
            "GET",
            "/v2/orders",
            params={"status": "open", "limit": "500", "nested": "true"},
        )
        return tuple(BrokerOrder.model_validate(item) for item in payload)

    def get_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        response = self._request_raw(
            "GET",
            "/v2/orders:by_client_order_id",
            params={"client_order_id": client_order_id, "nested": "true"},
            allowed_statuses=frozenset({200, 404}),
        )
        if response.status_code == 404:
            return None
        return BrokerOrder.model_validate(self._response_object(response))

    def cancel_order(self, order_id: str) -> bool:
        self._require_writes_enabled()
        response = self._request_raw(
            "DELETE",
            f"/v2/orders/{order_id}",
            allowed_statuses=frozenset({204, 404, 422}),
        )
        return response.status_code == 204

    def submit_order_idempotent(self, request: PaperOrderRequest) -> BrokerOrder:
        return self._submit_idempotent(
            client_order_id=request.client_order_id,
            payload=request.broker_payload(),
        )

    def submit_protected_entry_idempotent(
        self,
        request: ProtectedPaperEntryRequest,
    ) -> BrokerOrder:
        return self._submit_idempotent(
            client_order_id=request.client_order_id,
            payload=request.broker_payload(),
        )

    def submit_close_order_idempotent(
        self,
        request: PaperCloseRequest,
    ) -> BrokerOrder:
        return self._submit_idempotent(
            client_order_id=request.client_order_id,
            payload=request.broker_payload(),
        )

    def submit_stop_order_idempotent(
        self,
        request: PaperStopRequest,
    ) -> BrokerOrder:
        return self._submit_idempotent(
            client_order_id=request.client_order_id,
            payload=request.broker_payload(),
        )

    def submit_extended_limit_idempotent(
        self,
        request: PaperExtendedLimitRequest,
    ) -> BrokerOrder:
        return self._submit_idempotent(
            client_order_id=request.client_order_id,
            payload=request.broker_payload(),
        )

    def _submit_idempotent(
        self,
        *,
        client_order_id: str,
        payload: dict[str, object],
    ) -> BrokerOrder:
        self._require_writes_enabled()
        existing = self.get_order_by_client_id(client_order_id)
        if existing is not None:
            return existing
        response = self._request_raw(
            "POST",
            "/v2/orders",
            json_body=payload,
            allowed_statuses=frozenset({200, 201, 422}),
        )
        if response.status_code == 422:
            raced = self.get_order_by_client_id(client_order_id)
            if raced is not None:
                return raced
            raise BrokerError("Alpaca Paper order was rejected")
        return BrokerOrder.model_validate(self._response_object(response))

    def _require_writes_enabled(self) -> None:
        if not self.writes_enabled:
            raise BrokerWritesDisabledError("paper broker writes are disabled")

    def _request_object(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
    ) -> dict[str, object]:
        response = self._request_raw(
            method,
            path,
            params=params,
            allowed_statuses=frozenset({200}),
        )
        return self._response_object(response)

    def _request_list(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
    ) -> list[object]:
        response = self._request_raw(
            method,
            path,
            params=params,
            allowed_statuses=frozenset({200}),
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise BrokerError("Alpaca Paper response was not valid JSON") from exc
        if not isinstance(payload, list):
            raise BrokerError("Alpaca Paper response contract is invalid")
        return payload

    def _request_raw(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict[str, object] | None = None,
        allowed_statuses: frozenset[int],
    ) -> httpx.Response:
        try:
            response = self._client.request(
                method,
                f"{self.base_url}{path}",
                headers=self._headers,
                params=params,
                json=json_body,
            )
        except (httpx.HTTPError, OSError) as exc:
            raise BrokerError(f"Alpaca Paper request failed: {type(exc).__name__}") from exc
        if response.status_code not in allowed_statuses:
            raise BrokerError(f"Alpaca Paper request failed with HTTP {response.status_code}")
        return response

    @staticmethod
    def _response_object(response: httpx.Response) -> dict[str, object]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise BrokerError("Alpaca Paper response was not valid JSON") from exc
        if not isinstance(payload, dict):
            raise BrokerError("Alpaca Paper response contract is invalid")
        return payload
