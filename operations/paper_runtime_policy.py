"""Fail-closed operating policy for the only automated Alpaca Paper path."""

from __future__ import annotations

import re
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
    candidate_pool: tuple[str, ...]
    config_sha256: str

    def is_complete(self) -> bool:
        scalar_fields_complete = all(
            value.strip()
            for value in (
                self.selection_snapshot_id,
                self.open_confirmation_id,
                self.feishu_record_id,
                self.livermore_message_id,
                self.strategy_version,
            )
        )
        pool_complete = bool(self.candidate_pool) and len(self.candidate_pool) == len(
            set(self.candidate_pool)
        ) and all(symbol == symbol.strip().upper() for symbol in self.candidate_pool)
        hash_complete = re.fullmatch(r"[0-9a-f]{64}", self.config_sha256) is not None
        return scalar_fields_complete and pool_complete and hash_complete


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
        expected_candidate_pool: tuple[str, ...],
        expected_strategy_version: str,
    ) -> None:
        if not broker_write_enabled:
            raise RuntimeError("Paper writes are disabled")
        if trading_kill_switch:
            raise RuntimeError("Paper kill switch is active")
        if broker_base_url.rstrip("/") != PAPER_BASE_URL:
            raise RuntimeError("automated trading requires the Alpaca Paper host")
        if authorization.trade_date != trade_date or not authorization.is_complete():
            raise RuntimeError("complete same-day third-stage authorization is required")
        if authorization.candidate_pool != expected_candidate_pool:
            raise RuntimeError("third-stage candidate pool does not match Paper plans")
        if authorization.strategy_version != expected_strategy_version:
            raise RuntimeError("third-stage strategy version does not match Paper runtime")

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

    def validate_entry_risk(
        self,
        *,
        proposed_risk_fraction: float,
        symbol_open_risk: float,
        sector_open_risk: float,
        portfolio_open_risk: float,
        daily_return: float,
        sector_main_has_profit: bool,
    ) -> None:
        values = (
            proposed_risk_fraction,
            symbol_open_risk,
            sector_open_risk,
            portfolio_open_risk,
        )
        if proposed_risk_fraction <= 0 or any(value < 0 for value in values):
            raise ValueError("Paper risk fractions must be non-negative")
        if daily_return <= -self.stop_new_entries_fraction:
            raise RuntimeError("daily loss stops new Paper entries")
        if symbol_open_risk + proposed_risk_fraction > self.symbol_risk_fraction:
            raise RuntimeError("symbol Paper risk limit would be exceeded")
        if sector_open_risk > 0 and not sector_main_has_profit:
            raise RuntimeError("same-sector backup requires profit in the main symbol")
        if sector_open_risk + proposed_risk_fraction > self.sector_risk_fraction:
            raise RuntimeError("sector Paper risk limit would be exceeded")
        if portfolio_open_risk + proposed_risk_fraction > self.portfolio_risk_fraction:
            raise RuntimeError("portfolio Paper risk limit would be exceeded")

    def must_flatten_for_daily_return(self, daily_return: float) -> bool:
        return daily_return <= -self.flatten_account_fraction

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
