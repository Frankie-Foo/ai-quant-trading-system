"""Frozen pullback-acceptance strategy; research only."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import median

import polars as pl

from research.h30_challenger import TradeLeg, _five_minute_bars, _FiveMinuteBar


@dataclass(frozen=True)
class PullbackAcceptanceConfig:
    breakout_volume_ratio: float = 1.0
    pullback_volume_ratio: float = 0.75
    reclaim_volume_ratio: float = 1.25
    support_tolerance_pct: float = 0.003
    minimum_close_location: float = 0.65
    price_stop_pct: float = 0.015
    stop_slippage_reserve_pct: float = 0.005
    entry_slippage_pct: float = 0.001
    exit_slippage_pct: float = 0.001
    entry_cutoff_minutes: int = 210
    take_profit_r: float | None = None
    allow_two_bar_acceptance: bool = False

    def __post_init__(self) -> None:
        if not math.isclose(
            self.price_stop_pct + self.stop_slippage_reserve_pct,
            0.02,
            abs_tol=1e-12,
        ):
            raise ValueError("price stop plus stop-slippage reserve must equal 2%")
        if self.take_profit_r is not None and self.take_profit_r <= 0:
            raise ValueError("take-profit R must be positive")


@dataclass(frozen=True)
class PullbackAcceptanceResult:
    symbol: str | None
    status: str
    reason: str
    h30: float | None
    l30: float | None
    breakout_ts_utc: datetime | None
    pullback_ts_utc: datetime | None
    entry_ts_utc: datetime | None
    entry_px: float | None
    pullback_volume_ratio: float | None
    vwap_slope: float | None
    leg: TradeLeg | None
    provenance: str


def _exit(
    fives: list[_FiveMinuteBar],
    *,
    entry_index: int,
    entry_px: float,
    pullback_low: float,
    config: PullbackAcceptanceConfig,
) -> tuple[int, float, str]:
    fixed_stop = entry_px * (1 - config.price_stop_pct)
    take_profit = (
        entry_px * (1 + 0.02 * config.take_profit_r)
        if config.take_profit_r is not None
        else None
    )
    structural_stop = pullback_low * (1 - config.support_tolerance_pct)
    below_vwap = 0
    for index in range(entry_index, len(fives)):
        bar = fives[index]
        if bar.low <= fixed_stop:
            return index, entry_px * 0.98, "two_percent_all_in_stop"
        if take_profit is not None and bar.high >= take_profit:
            return index, take_profit, "take_profit"
        if bar.close < structural_stop:
            return (
                index,
                bar.close * (1 - config.exit_slippage_pct),
                "pullback_low_failed",
            )
        below_vwap = below_vwap + 1 if bar.close < bar.session_vwap else 0
        if below_vwap >= 2:
            return index, bar.close * (1 - config.exit_slippage_pct), "vwap_acceptance_failed"
        if index >= entry_index + 2:
            recent = fives[index - 2 : index + 1]
            if (
                recent[0].high > recent[1].high > recent[2].high
                and recent[0].low > recent[1].low > recent[2].low
            ):
                return index, bar.close * (1 - config.exit_slippage_pct), "lower_high_lower_low"
    last = fives[-1]
    return len(fives) - 1, last.close * (1 - config.exit_slippage_pct), "time_stop"


def _trade_result(
    *,
    symbol: str | None,
    fives: list[_FiveMinuteBar],
    entry_index: int,
    breakout_index: int,
    support_index: int,
    h30: float,
    l30: float,
    volume_ratio: float,
    vwap_slope: float,
    reason: str,
    config: PullbackAcceptanceConfig,
    provenance: str,
) -> PullbackAcceptanceResult:
    entry = fives[entry_index]
    support = fives[support_index]
    entry_px = entry.bar_vwap * (1 + config.entry_slippage_pct)
    exit_index, exit_px, exit_reason = _exit(
        fives,
        entry_index=entry_index,
        entry_px=entry_px,
        pullback_low=support.low,
        config=config,
    )
    leg = TradeLeg(
        entry.ts_utc,
        entry_px,
        fives[exit_index].ts_utc + timedelta(minutes=5),
        exit_px,
        exit_reason,
        exit_px / entry_px - 1,
        0.5,
    )
    return PullbackAcceptanceResult(
        symbol,
        "traded",
        reason,
        h30,
        l30,
        fives[breakout_index].ts_utc,
        support.ts_utc,
        entry.ts_utc,
        entry_px,
        volume_ratio,
        vwap_slope,
        leg,
        provenance,
    )


def evaluate_pullback_acceptance(
    bars: pl.DataFrame,
    *,
    session_open_utc: datetime,
    config: PullbackAcceptanceConfig | None = None,
) -> PullbackAcceptanceResult:
    """Require breakout, reduced-volume support test, then accepted reclaim."""
    config = config or PullbackAcceptanceConfig()
    if session_open_utc.tzinfo is None:
        raise ValueError("session_open_utc must be timezone-aware")
    required = {"symbol", "ts_utc", "open", "high", "low", "close", "volume", "vwap"}
    missing = required - set(bars.columns)
    if missing:
        raise ValueError(f"bars missing required columns: {sorted(missing)}")
    symbols = bars.get_column("symbol").unique().to_list()
    if len(symbols) > 1:
        raise ValueError("evaluate_pullback_acceptance accepts one symbol")
    symbol = str(symbols[0]) if symbols else None
    provenance = "research.pullback_acceptance.v1|next_5m_vwap|production=false"
    fives = _five_minute_bars(bars, session_open_utc=session_open_utc)
    expected = [session_open_utc + timedelta(minutes=5 * index) for index in range(6)]
    if [bar.ts_utc for bar in fives[:6]] != expected:
        return PullbackAcceptanceResult(
            symbol, "blocked", "h30_incomplete", None, None, None, None, None,
            None, None, None, None, provenance,
        )

    opening = fives[:6]
    h30 = max(bar.high for bar in opening)
    l30 = min(bar.low for bar in opening)
    opening_volume = median(bar.volume for bar in opening)
    breakout_index: int | None = None
    pullback_index: int | None = None
    for index in range(6, len(fives) - 1):
        bar = fives[index]
        entry = fives[index + 1]
        if entry.ts_utc >= session_open_utc + timedelta(minutes=config.entry_cutoff_minutes):
            break
        if breakout_index is None:
            if (
                bar.close > h30
                and bar.close > bar.session_vwap
                and bar.volume >= opening_volume * config.breakout_volume_ratio
            ):
                breakout_index = index
            continue
        breakout = fives[breakout_index]
        if pullback_index is None:
            if bar.close < h30 * (1 - config.support_tolerance_pct):
                breakout_index = None
                continue
            previous = fives[index - 1]
            continuation_slope = bar.session_vwap - fives[max(0, index - 3)].session_vwap
            if (
                config.allow_two_bar_acceptance
                and index > breakout_index
                and previous.close > h30
                and previous.close > previous.session_vwap
                and bar.close > h30
                and bar.close > bar.session_vwap
                and bar.high > previous.high
                and bar.low > previous.low
                and continuation_slope > 0
            ):
                return _trade_result(
                    symbol=symbol,
                    fives=fives,
                    entry_index=index + 1,
                    breakout_index=breakout_index,
                    support_index=index - 1,
                    h30=h30,
                    l30=l30,
                    volume_ratio=bar.volume / previous.volume,
                    vwap_slope=continuation_slope,
                    reason="two_bar_higher_price_accepted",
                    config=config,
                    provenance=provenance,
                )
            support = max(h30, bar.session_vwap)
            if (
                bar.low <= support * (1 + config.support_tolerance_pct)
                and bar.volume <= breakout.volume * config.pullback_volume_ratio
                and bar.close >= h30
            ):
                pullback_index = index
            continue
        pullback = fives[pullback_index]
        if bar.close < h30 * (1 - config.support_tolerance_pct):
            breakout_index = None
            pullback_index = None
            continue
        bar_range = bar.high - bar.low
        close_location = (bar.close - bar.low) / bar_range if bar_range > 0 else 0.0
        vwap_slope = bar.session_vwap - fives[max(0, index - 3)].session_vwap
        if (
            bar.close > pullback.high
            and bar.close > bar.session_vwap
            and bar.volume >= pullback.volume * config.reclaim_volume_ratio
            and close_location >= config.minimum_close_location
            and vwap_slope > 0
        ):
            return _trade_result(
                symbol=symbol,
                fives=fives,
                entry_index=index + 1,
                breakout_index=breakout_index,
                support_index=pullback_index,
                h30=h30,
                l30=l30,
                volume_ratio=pullback.volume / breakout.volume,
                vwap_slope=vwap_slope,
                reason="higher_price_accepted",
                config=config,
                provenance=provenance,
            )
    if breakout_index is None:
        reason = "breakout_not_confirmed"
    elif pullback_index is None:
        reason = "pullback_not_confirmed"
    else:
        reason = "acceptance_not_confirmed"
    return PullbackAcceptanceResult(
        symbol,
        "no_trade",
        reason,
        h30,
        l30,
        fives[breakout_index].ts_utc if breakout_index is not None else None,
        fives[pullback_index].ts_utc if pullback_index is not None else None,
        None,
        None,
        None,
        None,
        None,
        provenance,
    )
