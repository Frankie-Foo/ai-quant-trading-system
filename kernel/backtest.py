"""Deterministic conservative cost functions for the bar-driven backtest."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime

import polars as pl

from kernel.config import Config
from kernel.exits import make_exits
from kernel.labels import BarrierEvent, triple_barrier
from kernel.signals import OrbSignal, orb5
from kernel.sizing import SizingResult, size_position


@dataclass(frozen=True)
class CostBreakdown:
    commission: float
    sec_fee: float
    finra_taf: float
    spread: float
    impact: float
    stop_slippage: float

    @property
    def total(self) -> float:
        return (
            self.commission
            + self.sec_fee
            + self.finra_taf
            + self.spread
            + self.impact
            + self.stop_slippage
        )


@dataclass(frozen=True)
class BacktestTrade:
    symbol: str
    signal: OrbSignal
    sizing: SizingResult
    barrier: BarrierEvent
    costs: CostBreakdown
    gross_pnl: float
    net_pnl: float
    net_return_on_notional: float
    provenance: str


def round_trip_costs(
    *,
    shares: int,
    entry_px: float,
    exit_px: float,
    cs_spread: float,
    participation: float,
    atr_pct: float,
    atr: float,
    stopped: bool,
    cfg: Config,
) -> CostBreakdown:
    """Compute conservative two-leg costs; SEC/TAF apply to the sell leg only."""
    if shares <= 0:
        raise ValueError("shares must be positive")
    numeric = (entry_px, exit_px, cs_spread, participation, atr_pct, atr)
    if not all(math.isfinite(value) for value in numeric):
        raise ValueError("cost inputs must be finite")
    if entry_px <= 0 or exit_px <= 0 or atr <= 0:
        raise ValueError("prices and ATR must be positive")
    if cs_spread < 0 or not 0 <= participation <= 1 or atr_pct < 0:
        raise ValueError("spread, participation, or ATR percent is invalid")
    buy_notional = shares * entry_px
    sell_notional = shares * exit_px
    commission_per_leg = max(shares * cfg.costs.commission_per_share, cfg.costs.commission_min)
    spread = (
        shares
        * cs_spread
        * (entry_px + exit_px)
        * cfg.costs.spread_capture
    )
    impact_rate = cfg.costs.impact_k * math.sqrt(participation) * atr_pct
    return CostBreakdown(
        commission=2 * commission_per_leg,
        sec_fee=sell_notional / 1_000_000 * cfg.costs.sec_fee_per_million_sold,
        finra_taf=shares * cfg.costs.finra_taf_per_share_sold,
        spread=spread,
        impact=(buy_notional + sell_notional) * impact_rate,
        stop_slippage=(shares * cfg.costs.stop_slippage_atr * atr if stopped else 0.0),
    )


def backtest_orb_trade(
    bars: pl.DataFrame,
    *,
    symbol: str,
    trade_date: date,
    session_open_utc: datetime,
    session_close_utc: datetime,
    is_half_day: bool,
    rvol: float,
    atr14: float,
    adv_usd: float,
    tier: str,
    confidence: float,
    cs_spread: float,
    cfg: Config,
) -> BacktestTrade | None:
    """Replay one eligible symbol/day from ORB trigger through conservative costs."""
    if session_close_utc <= session_open_utc:
        raise ValueError("session close must be after open")
    signal = orb5(
        bars,
        session_open_utc=session_open_utc,
        asof_utc=session_close_utc,
        rvol=rvol,
        min_rvol=cfg.universe.min_rvol,
    )
    if not signal.triggered:
        return None
    if signal.entry_ts_utc is None or signal.entry_px is None:
        raise AssertionError("triggered signal is missing entry data")
    sizing = size_position(
        symbol=symbol,
        price=signal.entry_px,
        atr14=atr14,
        adv_usd=adv_usd,
        tier=tier,
        confidence=confidence,
        cfg=cfg,
    )
    if sizing.shares <= 0:
        return None
    exit_plan = make_exits(
        signal.entry_px,
        atr14,
        trade_date=trade_date,
        is_half_day=is_half_day,
        cfg=cfg,
    )
    barrier = triple_barrier(
        bars,
        entry_ts=signal.entry_ts_utc,
        entry_px=signal.entry_px,
        tp_px=exit_plan.tp_px,
        sl_px=exit_plan.sl_px,
        time_stop=min(exit_plan.time_stop_utc, session_close_utc),
    )
    entry_volume = bars.filter(pl.col("ts_utc") == signal.entry_ts_utc).get_column(
        "volume"
    )
    if entry_volume.is_empty() or int(entry_volume[0]) <= 0:
        raise ValueError("entry bar volume is unavailable")
    participation = min(1.0, sizing.shares / int(entry_volume[0]))
    costs = round_trip_costs(
        shares=sizing.shares,
        entry_px=signal.entry_px,
        exit_px=barrier.exit_px,
        cs_spread=cs_spread,
        participation=participation,
        atr_pct=atr14 / signal.entry_px,
        atr=atr14,
        stopped=barrier.which == "sl",
        cfg=cfg,
    )
    gross_pnl = sizing.shares * (barrier.exit_px - signal.entry_px)
    net_pnl = gross_pnl - costs.total
    return BacktestTrade(
        symbol=symbol,
        signal=signal,
        sizing=sizing,
        barrier=barrier,
        costs=costs,
        gross_pnl=gross_pnl,
        net_pnl=net_pnl,
        net_return_on_notional=net_pnl / sizing.final_notional,
        provenance=(
            f"kernel.backtest.orb5@{trade_date.isoformat()}|"
            "entry=next_bar_vwap|costs=two_leg|stop_slippage=0.5atr"
        ),
    )
