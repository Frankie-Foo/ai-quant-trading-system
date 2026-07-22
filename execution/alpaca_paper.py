"""Keyless Paper Broker client for the isolated cloud platform API."""

from __future__ import annotations

from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

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
    take_profit_price: str = Field(min_length=1)
    stop_loss_price: str = Field(min_length=1)

    def broker_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "client_order_id": self.client_order_id,
            "symbol": self.symbol,
            "qty": str(self.qty),
            "side": self.side,
            "type": self.order_type,
            "time_in_force": self.time_in_force,
            "extended_hours": self.extended_hours,
            "order_class": "bracket",
            "take_profit": {"limit_price": self.take_profit_price},
            "stop_loss": {"stop_price": self.stop_loss_price},
        }
        if self.order_type == "limit":
            if self.limit_price is None:
                raise ValueError("limit order requires limit_price")
            payload["limit_price"] = self.limit_price
        elif self.limit_price is not None:
            raise ValueError("market order cannot include limit_price")
        return payload


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


class CloudPaperBroker:
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
        payload = self._request(
            "DELETE", f"/{PLATFORM_API_VERSION}/paper/orders/cancel/{order_id}"
        )
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

    def submit_close_order_idempotent(
        self, request: PaperCloseRequest
    ) -> BrokerOrder:
        if not self.writes_enabled:
            raise BrokerWritesDisabledError("paper broker writes are disabled")
        payload = self._request(
            "POST",
            f"/{PLATFORM_API_VERSION}/paper/orders",
            json_body={"kind": "close", "request": request.model_dump(mode="json")},
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
