"""Production adapters for SIP facts and read-only broker reconciliation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Protocol

import polars as pl

from data_plane.calendar import build_xnys_schedule
from execution.alpaca_paper import PaperPosition
from kernel.adaptive_trade_plan import BaselineTradePlan, RealtimePlanFacts
from kernel.features.order_flow import order_flow_features
from kernel.technical_monitor import (
    AggregatedBar,
    TimeframeSnapshot,
    build_timeframe_snapshot,
    current_session_vwap,
    resample_completed_bars,
)
from operations.adaptive_plan_coordinator import BrokerPositionObservation


class SipFactStorePort(Protocol):
    def bars_for_symbol(
        self,
        symbol: str,
        *,
        start_utc: datetime,
        end_utc: datetime,
    ) -> pl.DataFrame: ...

    def quotes_for_symbol(
        self,
        symbol: str,
        *,
        start_utc: datetime,
        end_utc: datetime,
    ) -> pl.DataFrame: ...

    def trades_for_symbol(
        self,
        symbol: str,
        *,
        start_utc: datetime,
        end_utc: datetime,
    ) -> pl.DataFrame: ...


class PaperPositionReadPort(Protocol):
    def list_positions(self) -> tuple[PaperPosition, ...]: ...


@dataclass(frozen=True)
class PlanEvidence:
    benchmark_symbol: str
    sector_symbol: str
    catalyst_score: float | None
    provenance: str

    def __post_init__(self) -> None:
        for name, value in (
            ("benchmark_symbol", self.benchmark_symbol),
            ("sector_symbol", self.sector_symbol),
        ):
            if value != value.strip().upper() or not value:
                raise ValueError(f"{name} must be normalized uppercase")
        if (
            self.catalyst_score is not None
            and (
                not math.isfinite(self.catalyst_score)
                or not 0 <= self.catalyst_score <= 1
            )
        ):
            raise ValueError("catalyst_score must be in [0, 1] when present")
        if not self.provenance.strip():
            raise ValueError("plan evidence provenance is required")


def _session_return(
    bars: tuple[AggregatedBar, ...],
    *,
    trade_date: date,
) -> float | None:
    selected = [item for item in bars if item.trade_date == trade_date]
    if not selected or selected[0].open <= 0:
        return None
    return selected[-1].close / selected[0].open - 1


def _trend_confirmed(snapshot: TimeframeSnapshot | None) -> bool:
    return bool(
        snapshot is not None
        and snapshot.boll_mid is not None
        and snapshot.macd_hist is not None
        and snapshot.close > snapshot.boll_mid
        and snapshot.macd_hist > 0
    )


def _one_minute_trigger(snapshot: TimeframeSnapshot | None) -> bool:
    return bool(
        snapshot is not None
        and snapshot.macd_hist is not None
        and snapshot.kdj_k is not None
        and snapshot.kdj_d is not None
        and snapshot.macd_hist > 0
        and snapshot.kdj_k > snapshot.kdj_d
        and (
            snapshot.boll_upper is None
            or snapshot.close <= snapshot.boll_upper * 1.002
        )
    )


class SipStoreMarketFactsAdapter:
    """Build causal multi-timeframe facts from the restart-safe SIP event store."""

    def __init__(
        self,
        *,
        store: SipFactStorePort,
        evidence: dict[str, PlanEvidence],
        order_flow_window: timedelta = timedelta(minutes=5),
        history_days: int = 10,
    ):
        if not timedelta(0) < order_flow_window <= timedelta(hours=1):
            raise ValueError("order_flow_window must be in (0, 1h]")
        if history_days < 7:
            raise ValueError("history_days must be at least 7")
        self.store = store
        self.evidence = dict(evidence)
        self.order_flow_window = order_flow_window
        self.history_days = history_days

    def read(
        self,
        plan: BaselineTradePlan,
        *,
        observed_at_utc: datetime,
    ) -> RealtimePlanFacts:
        if (
            observed_at_utc.tzinfo is None
            or observed_at_utc.utcoffset() != timedelta(0)
        ):
            raise ValueError("observed_at_utc must be timezone-aware UTC")
        plan_evidence = self.evidence.get(plan.plan_id)
        if plan_evidence is None:
            raise KeyError(f"missing evidence for adaptive plan: {plan.plan_id}")
        schedule = build_xnys_schedule(
            plan.trade_date - timedelta(days=self.history_days),
            plan.trade_date,
        )
        if schedule.is_empty():
            raise ValueError("XNYS schedule is unavailable")
        first_open = schedule.get_column("market_open_utc")[0]
        if not isinstance(first_open, datetime):
            raise ValueError("XNYS schedule has an invalid market open")
        query_end = observed_at_utc + timedelta(microseconds=1)

        symbol_bars = self._bars(
            plan.symbol,
            start_utc=first_open,
            end_utc=query_end,
        )
        benchmark_bars = self._bars(
            plan_evidence.benchmark_symbol,
            start_utc=first_open,
            end_utc=query_end,
        )
        sector_bars = self._bars(
            plan_evidence.sector_symbol,
            start_utc=first_open,
            end_utc=query_end,
        )
        symbol_one = resample_completed_bars(
            symbol_bars,
            schedule,
            interval_minutes=1,
            as_of_utc=observed_at_utc,
        )
        symbol_five = resample_completed_bars(
            symbol_bars,
            schedule,
            interval_minutes=5,
            as_of_utc=observed_at_utc,
        )
        symbol_fifteen = resample_completed_bars(
            symbol_bars,
            schedule,
            interval_minutes=15,
            as_of_utc=observed_at_utc,
        )
        benchmark_one = resample_completed_bars(
            benchmark_bars,
            schedule,
            interval_minutes=1,
            as_of_utc=observed_at_utc,
        )
        benchmark_five = resample_completed_bars(
            benchmark_bars,
            schedule,
            interval_minutes=5,
            as_of_utc=observed_at_utc,
        )
        benchmark_fifteen = resample_completed_bars(
            benchmark_bars,
            schedule,
            interval_minutes=15,
            as_of_utc=observed_at_utc,
        )
        sector_one = resample_completed_bars(
            sector_bars,
            schedule,
            interval_minutes=1,
            as_of_utc=observed_at_utc,
        )

        one = build_timeframe_snapshot(symbol_one, timeframe="1m")
        five = build_timeframe_snapshot(symbol_five, timeframe="5m")
        fifteen = build_timeframe_snapshot(symbol_fifteen, timeframe="15m")
        benchmark_5m = build_timeframe_snapshot(benchmark_five, timeframe="5m")
        benchmark_15m = build_timeframe_snapshot(
            benchmark_fifteen,
            timeframe="15m",
        )
        session_vwap = current_session_vwap(
            symbol_one,
            trade_date=plan.trade_date,
        )
        benchmark_vwap = current_session_vwap(
            benchmark_one,
            trade_date=plan.trade_date,
        )
        sector_vwap = current_session_vwap(
            sector_one,
            trade_date=plan.trade_date,
        )

        quote_start = observed_at_utc - timedelta(minutes=3)
        quotes = self.store.quotes_for_symbol(
            plan.symbol,
            start_utc=quote_start,
            end_utc=query_end,
        ).sort("ts_utc")
        if quotes.is_empty():
            raise ValueError("no SIP quote is available for plan symbol")
        quote = quotes.row(-1, named=True)
        quote_ts = quote["ts_utc"]
        if not isinstance(quote_ts, datetime):
            raise ValueError("SIP quote timestamp is invalid")
        bid = float(quote["bid_price"])
        ask = float(quote["ask_price"])
        midpoint = (bid + ask) / 2

        order_start = observed_at_utc - self.order_flow_window
        trades = self.store.trades_for_symbol(
            plan.symbol,
            start_utc=order_start,
            end_utc=query_end,
        )
        flow_quotes = self.store.quotes_for_symbol(
            plan.symbol,
            start_utc=order_start,
            end_utc=query_end,
        )
        flow = order_flow_features(
            trades,
            flow_quotes,
            symbols=(plan.symbol,),
            asof_utc=observed_at_utc,
            window=self.order_flow_window,
            provenance=plan_evidence.provenance,
        ).row(0, named=True)
        raw_imbalance = flow["order_imbalance"]
        order_imbalance = (
            float(raw_imbalance)
            if isinstance(raw_imbalance, (int, float))
            and not isinstance(raw_imbalance, bool)
            else None
        )

        symbol_return = _session_return(symbol_one, trade_date=plan.trade_date)
        benchmark_return = _session_return(
            benchmark_one,
            trade_date=plan.trade_date,
        )
        relative_strength = (
            None
            if symbol_return is None or benchmark_return is None
            else symbol_return - benchmark_return
        )
        benchmark_last = benchmark_one[-1].close if benchmark_one else None
        sector_last = sector_one[-1].close if sector_one else None
        benchmark_above_vwap = bool(
            benchmark_last is not None
            and benchmark_vwap is not None
            and benchmark_last > benchmark_vwap
        )
        sector_above_vwap = bool(
            sector_last is not None
            and sector_vwap is not None
            and sector_last > sector_vwap
        )
        market_risk_off = (
            not benchmark_above_vwap
            and not _trend_confirmed(benchmark_5m)
            and not _trend_confirmed(benchmark_15m)
        )
        proposed_stop = (
            five.last_confirmed_bottom
            if (
                five is not None
                and five.last_confirmed_bottom is not None
                and plan.hard_stop < five.last_confirmed_bottom < bid
            )
            else None
        )
        indicators_available = all(
            snapshot is not None
            and snapshot.boll_mid is not None
            and snapshot.macd_hist is not None
            and snapshot.kdj_k is not None
            and snapshot.kdj_d is not None
            for snapshot in (one, five, fifteen)
        )
        data_complete = bool(
            indicators_available
            and session_vwap is not None
            and benchmark_vwap is not None
            and sector_vwap is not None
            and one is not None
            and 0
            <= (observed_at_utc - one.completed_at_utc).total_seconds()
            <= 180
        )
        return RealtimePlanFacts(
            observed_at_utc=observed_at_utc,
            quote_ts_utc=quote_ts,
            bid=bid,
            ask=ask,
            last_price=midpoint,
            session_vwap=session_vwap,
            completed_one_minute_bar_utc=(
                None if one is None else one.completed_at_utc
            ),
            one_minute_trigger=_one_minute_trigger(one),
            five_minute_confirmed=_trend_confirmed(five),
            fifteen_minute_confirmed=_trend_confirmed(fifteen),
            green_volume_ratio=None if one is None else one.green_volume_ratio,
            relative_strength=relative_strength,
            benchmark_above_vwap=benchmark_above_vwap,
            sector_above_vwap=sector_above_vwap,
            market_risk_off=market_risk_off,
            order_flow_imbalance=order_imbalance,
            catalyst_score=plan_evidence.catalyst_score,
            data_complete=data_complete,
            proposed_structural_stop=proposed_stop,
            first_target_filled=False,
        )

    def _bars(
        self,
        symbol: str,
        *,
        start_utc: datetime,
        end_utc: datetime,
    ) -> pl.DataFrame:
        return self.store.bars_for_symbol(
            symbol,
            start_utc=start_utc,
            end_utc=end_utc,
        )


class CloudBrokerPositionAdapter:
    """Read positions from the isolated Paper adapter; never submit an order."""

    def __init__(self, broker: PaperPositionReadPort):
        self.broker = broker

    def position(
        self,
        symbol: str,
        *,
        observed_at_utc: datetime,
    ) -> BrokerPositionObservation | None:
        normalized = symbol.strip().upper()
        matches = [
            item
            for item in self.broker.list_positions()
            if item.symbol == normalized
            and item.side.lower() == "long"
            and self._decimal(item.qty, name="position quantity") > 0
        ]
        if not matches:
            return None
        if len(matches) != 1:
            raise ValueError("broker returned duplicate positions for one symbol")
        item = matches[0]
        quantity = self._decimal(item.qty, name="position quantity")
        if quantity != quantity.to_integral_value():
            raise ValueError("fractional positions are not supported by this plan")
        if item.avg_entry_price is None:
            raise ValueError("broker position is missing average entry price")
        average_entry = self._decimal(
            item.avg_entry_price,
            name="position average entry",
        )
        return BrokerPositionObservation(
            symbol=normalized,
            shares=int(quantity),
            average_entry=float(average_entry),
            observed_at_utc=observed_at_utc,
        )

    @staticmethod
    def _decimal(value: str, *, name: str) -> Decimal:
        try:
            parsed = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError(f"{name} is invalid") from exc
        if not parsed.is_finite() or parsed <= 0:
            raise ValueError(f"{name} must be finite and positive")
        return parsed
