"""Fail-closed operating policy for the only automated Alpaca Paper path."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time

PAPER_BASE_URL = "https://paper-api.alpaca.markets"


@dataclass(frozen=True)
class ExecutionAuthorization:
    trade_date: date
    selection_snapshot_id: str
    open_confirmation_id: str
    feishu_record_id: str
    livermore_message_id: str
    strategy_version: str

    def is_complete(self) -> bool:
        return all(
            value.strip()
            for value in (
                self.selection_snapshot_id,
                self.open_confirmation_id,
                self.feishu_record_id,
                self.livermore_message_id,
                self.strategy_version,
            )
        )


@dataclass(frozen=True)
class PaperRuntimePolicy:
    earliest_entry: time = time(9, 56)
    entry_cutoff: time = time(15, 0)
    cancel_entry_orders: time = time(15, 45)
    flatten_by: time = time(15, 50)
    symbol_risk_fraction: float = 0.005
    sector_risk_fraction: float = 0.0075
    portfolio_risk_fraction: float = 0.015
    stop_new_entries_fraction: float = 0.015
    flatten_account_fraction: float = 0.02
    maximum_all_in_stop_pct: float = 0.02

    def validate_arming(
        self,
        *,
        trade_date: date,
        broker_write_enabled: bool,
        trading_kill_switch: bool,
        broker_base_url: str,
        authorization: ExecutionAuthorization,
    ) -> None:
        if not broker_write_enabled:
            raise RuntimeError("Paper writes are disabled")
        if trading_kill_switch:
            raise RuntimeError("Paper kill switch is active")
        if broker_base_url.rstrip("/") != PAPER_BASE_URL:
            raise RuntimeError("automated trading requires the Alpaca Paper host")
        if authorization.trade_date != trade_date or not authorization.is_complete():
            raise RuntimeError("complete same-day third-stage authorization is required")

    def entry_allowed_at(self, now_et: datetime) -> bool:
        clock = self._clock(now_et)
        return self.earliest_entry <= clock < self.entry_cutoff

    def must_cancel_entries_at(self, now_et: datetime) -> bool:
        return self._clock(now_et) >= self.cancel_entry_orders

    def must_flatten_at(self, now_et: datetime) -> bool:
        return self._clock(now_et) >= self.flatten_by

    def max_symbol_loss(self, equity: float) -> float:
        return self._loss_budget(equity, self.symbol_risk_fraction)

    def max_sector_loss(self, equity: float) -> float:
        return self._loss_budget(equity, self.sector_risk_fraction)

    def max_portfolio_loss(self, equity: float) -> float:
        return self._loss_budget(equity, self.portfolio_risk_fraction)

    def stop_new_entries_loss(self, equity: float) -> float:
        return self._loss_budget(equity, self.stop_new_entries_fraction)

    def flatten_account_loss(self, equity: float) -> float:
        return self._loss_budget(equity, self.flatten_account_fraction)

    def position_quantity(
        self,
        *,
        equity: float,
        entry_price: float,
        all_in_stop_pct: float,
        buying_power: float,
    ) -> int:
        if equity <= 0 or entry_price <= 0 or buying_power <= 0:
            raise ValueError("positive account and price values are required")
        if not 0 < all_in_stop_pct <= self.maximum_all_in_stop_pct:
            raise ValueError("all-in stop must be positive and no greater than 2%")
        risk_quantity = int(
            self.max_symbol_loss(equity) / (entry_price * all_in_stop_pct)
        )
        buying_power_quantity = int(buying_power / entry_price)
        quantity = min(risk_quantity, buying_power_quantity)
        if quantity < 1:
            raise ValueError("available Paper risk budget cannot fund one share")
        return quantity

    @staticmethod
    def _clock(now_et: datetime) -> time:
        if now_et.tzinfo is None or now_et.utcoffset() is None:
            raise ValueError("market time must be timezone-aware")
        return now_et.time().replace(tzinfo=None)

    @staticmethod
    def _loss_budget(equity: float, fraction: float) -> float:
        if equity <= 0:
            raise ValueError("equity must be positive")
        return equity * fraction
