"""Causal five-minute implementation of the user-supplied ETF strategy draft."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class DraftConfig:
    orb_volume_multiple: float = 1.5
    pullback_volume_multiple: float = 1.0
    max_stop_pct: float = 0.012
    near_level_pct: float = 0.002
    stop_buffer_pct: float = 0.0015
    cost_pct_per_side: float = 0.0003
    stop_slippage_pct: float = 0.0005


@dataclass(frozen=True)
class FiveBar:
    ts_utc: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    vwap: float
    ema9: float
    ema20: float
    ema50: float


@dataclass(frozen=True)
class DraftTrade:
    symbol: str
    strategy: str
    signal_ts_utc: datetime
    entry_ts_utc: datetime
    entry_px: float
    stop_level: float
    exit_ts_utc: datetime
    gross_exit_value_per_share: float
    sold_fraction: float
    exit_reason: str
    risk_per_share: float
    entry_bar_volume: float
    production_eligible: bool = False


def ema(values: list[float], span: int, seed: list[float] | None = None) -> list[float]:
    combined = [*(seed or []), *values]
    if not combined:
        return []
    alpha = 2 / (span + 1)
    result = [combined[0]]
    for value in combined[1:]:
        result.append(alpha * value + (1 - alpha) * result[-1])
    return result[-len(values) :] if values else []


def find_trades(
    symbol: str,
    bars: list[FiveBar],
    benchmark: list[FiveBar],
    *,
    session_open_utc: datetime,
    config: DraftConfig,
    allow_orb: bool = True,
    allow_pullback: bool = True,
) -> list[DraftTrade]:
    """Return causal candidates; portfolio arbitration happens after this function."""
    if len(bars) != len(benchmark) or len(bars) < 12:
        return []
    orh = max(bar.high for bar in bars[:3])
    deadline = session_open_utc + timedelta(minutes=375)
    candidates: list[DraftTrade] = []
    for index in range(10, len(bars) - 1):
        signal = bars[index]
        entry = bars[index + 1]
        if entry.ts_utc >= deadline or benchmark[index].close < benchmark[index].vwap:
            continue
        if allow_orb and index >= 20:
            average_volume = sum(bar.volume for bar in bars[index - 20 : index]) / 20
            if (
                signal.close > orh
                and signal.close > signal.vwap
                and signal.volume >= config.orb_volume_multiple * average_volume
                and entry.open <= signal.close * 1.003
            ):
                stop = min(signal.low, orh * (1 - config.stop_buffer_pct))
                trade = _simulate(
                    symbol,
                    "opening_range_breakout",
                    bars,
                    index,
                    stop,
                    partial_r=1.5,
                    target_r=2.2,
                    config=config,
                )
                if trade is not None:
                    candidates.append(trade)
        if allow_pullback and index >= 13:
            average_volume = sum(bar.volume for bar in bars[index - 10 : index]) / 10
            level = max(signal.vwap, signal.ema9)
            intersects_level = (
                signal.low <= level * (1 + config.near_level_pct)
                and signal.high >= level * (1 - config.near_level_pct)
            )
            two_below_vwap = all(
                bar.close < bar.vwap for bar in bars[index - 1 : index + 1]
            )
            if (
                signal.close > signal.open
                and signal.close > bars[index - 1].high
                and signal.close > signal.vwap
                and signal.ema20 > signal.ema50
                and signal.ema20 > bars[index - 3].ema20
                and signal.ema50 > bars[index - 3].ema50
                and intersects_level
                and not two_below_vwap
                and signal.volume > config.pullback_volume_multiple * average_volume
            ):
                trade = _simulate(
                    symbol,
                    "vwap_trend_pullback",
                    bars,
                    index,
                    signal.low * (1 - config.stop_buffer_pct),
                    partial_r=1.0,
                    target_r=2.0,
                    config=config,
                )
                if trade is not None:
                    candidates.append(trade)
    return candidates


def _simulate(
    symbol: str,
    strategy: str,
    bars: list[FiveBar],
    signal_index: int,
    stop_level: float,
    *,
    partial_r: float,
    target_r: float,
    config: DraftConfig,
) -> DraftTrade | None:
    signal = bars[signal_index]
    entry_bar = bars[signal_index + 1]
    entry_px = entry_bar.open * (1 + config.cost_pct_per_side)
    risk = entry_px - stop_level
    if risk <= 0 or risk / entry_px > config.max_stop_pct:
        return None
    active_stop = stop_level
    partial = False
    realized = 0.0
    sold = 0.0
    liquidation = bars[0].ts_utc + timedelta(minutes=385)
    for index in range(signal_index + 1, len(bars)):
        bar = bars[index]
        if bar.ts_utc >= liquidation:
            price = bar.open * (1 - config.cost_pct_per_side)
            return _trade(
                symbol, strategy, signal, entry_bar, entry_px, stop_level, bar.ts_utc,
                realized + (1 - sold) * price, 1.0, "time_exit", risk,
            )
        if bar.low <= active_stop:
            price = active_stop * (
                1 - config.cost_pct_per_side - config.stop_slippage_pct
            )
            return _trade(
                symbol, strategy, signal, entry_bar, entry_px, stop_level,
                bar.ts_utc + timedelta(minutes=5), realized + (1 - sold) * price,
                1.0, "stop", risk,
            )
        if not partial and bar.high >= entry_px + partial_r * risk:
            price = (entry_px + partial_r * risk) * (1 - config.cost_pct_per_side)
            realized += 0.5 * price
            sold = 0.5
            partial = True
            active_stop = max(active_stop, entry_bar.open)
        if bar.high >= entry_px + target_r * risk:
            price = (entry_px + target_r * risk) * (1 - config.cost_pct_per_side)
            return _trade(
                symbol, strategy, signal, entry_bar, entry_px, stop_level,
                bar.ts_utc + timedelta(minutes=5), realized + (1 - sold) * price,
                1.0, "target", risk,
            )
        trend_break = bar.close < bar.vwap or (
            partial and bar.close < bar.ema9
        )
        if trend_break and index + 1 < len(bars):
            next_bar = bars[index + 1]
            price = next_bar.open * (1 - config.cost_pct_per_side)
            return _trade(
                symbol, strategy, signal, entry_bar, entry_px, stop_level,
                next_bar.ts_utc, realized + (1 - sold) * price, 1.0,
                "trend_exit", risk,
            )
    last = bars[-1]
    price = last.close * (1 - config.cost_pct_per_side)
    return _trade(
        symbol, strategy, signal, entry_bar, entry_px, stop_level,
        last.ts_utc + timedelta(minutes=5), realized + (1 - sold) * price,
        1.0, "data_end", risk,
    )


def _trade(
    symbol: str,
    strategy: str,
    signal: FiveBar,
    entry: FiveBar,
    entry_px: float,
    stop_level: float,
    exit_ts_utc: datetime,
    gross_exit_value_per_share: float,
    sold_fraction: float,
    exit_reason: str,
    risk: float,
) -> DraftTrade:
    return DraftTrade(
        symbol=symbol,
        strategy=strategy,
        signal_ts_utc=signal.ts_utc + timedelta(minutes=5),
        entry_ts_utc=entry.ts_utc,
        entry_px=entry_px,
        stop_level=stop_level,
        exit_ts_utc=exit_ts_utc,
        gross_exit_value_per_share=gross_exit_value_per_share,
        sold_fraction=sold_fraction,
        exit_reason=exit_reason,
        risk_per_share=risk,
        entry_bar_volume=entry.volume,
    )
