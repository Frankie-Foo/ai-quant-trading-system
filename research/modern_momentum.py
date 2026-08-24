"""Causal, research-only H15 momentum strategy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import polars as pl

from research.h30_challenger import _five_minute_bars, _FiveMinuteBar


@dataclass(frozen=True)
class ModernMomentumConfig:
    minimum_market_cap: float = 1_000_000_000.0
    minimum_premarket_rvol: float = 1.5
    minimum_h15_volume: int = 100_000
    minimum_gap_return: float = 0.04
    target_r: float = 3.0
    max_all_in_stop_pct: float = 0.02
    relative_spread: float = 0.001
    maximum_entry_relative_spread: float = 0.001
    market_impact_pct: float = 0.0002
    stop_slippage_reserve_pct: float = 0.005
    signal_cutoff_minutes: int = 180
    liquidation_minutes: int = 375


@dataclass(frozen=True)
class ModernMomentumTrade:
    symbol: str
    signal_ts_utc: datetime
    entry_ts_utc: datetime
    entry_px: float
    stop_level: float
    target_level: float
    exit_ts_utc: datetime
    exit_px: float
    exit_reason: str
    all_in_stop_pct: float
    h15: float
    macd: float
    premarket_rvol: float
    production_eligible: bool = False


@dataclass(frozen=True)
class ReentrySignal:
    entry_reference: float
    structural_stop: float
    signal_ts_utc: datetime


def pullback_reentry(
    fives: list[_FiveMinuteBar],
    *,
    stopped_at_utc: datetime,
    h15: float,
    asof_utc: datetime,
) -> ReentrySignal | None:
    """Require three completed 5-minute bars to recover after the first stop."""
    if stopped_at_utc.tzinfo is None or asof_utc.tzinfo is None or h15 <= 0:
        raise ValueError("timezone-aware stop/asof and positive H15 are required")
    eligible = [
        bar
        for bar in fives
        if bar.ts_utc > stopped_at_utc
        and bar.ts_utc + timedelta(minutes=5) <= asof_utc
    ]
    if len(eligible) < 3:
        return None
    washout, support, reclaim = eligible[-3:]
    if not (
        support.low > washout.low
        and support.close > h15
        and support.close > support.session_vwap
        and reclaim.low > support.low
        and reclaim.close > support.high
        and reclaim.close > h15
        and reclaim.close > reclaim.session_vwap
        and washout.session_vwap < support.session_vwap < reclaim.session_vwap
        and reclaim.volume >= support.volume * 0.8
    ):
        return None
    return ReentrySignal(reclaim.close, support.low, reclaim.ts_utc + timedelta(minutes=5))


def reentry_exit_reason(
    fives: list[_FiveMinuteBar],
    *,
    entered_at_utc: datetime,
    asof_utc: datetime,
    target_level: float,
    liquidation_utc: datetime,
) -> str | None:
    completed = [
        bar
        for bar in fives
        if bar.ts_utc >= entered_at_utc
        and bar.ts_utc + timedelta(minutes=5) <= asof_utc
    ]
    if any(bar.high >= target_level for bar in completed):
        return "target_3r"
    if asof_utc >= liquidation_utc:
        return "time_exit"
    if len(completed) >= 2:
        previous, current = completed[-2:]
        if (
            previous.close < previous.session_vwap
            and current.close < current.session_vwap
            and current.high < previous.high
            and current.low < previous.low
        ):
            return "trend_exit"
    return None


def _ema(values: list[float], span: int) -> list[float]:
    alpha = 2 / (span + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append(alpha * value + (1 - alpha) * result[-1])
    return result


def evaluate_modern_momentum(
    bars: pl.DataFrame,
    *,
    session_open_utc: datetime,
    prior_close: float,
    market_cap: float,
    premarket_rvol: float,
    config: ModernMomentumConfig,
    relative_spread: float | None = None,
) -> ModernMomentumTrade | None:
    """Replay first eligible long signal; ambiguous minute bars are stop-first."""
    if session_open_utc.tzinfo is None:
        raise ValueError("session_open_utc must be timezone-aware")
    if prior_close <= 0 or market_cap < config.minimum_market_cap:
        return None
    if premarket_rvol < config.minimum_premarket_rvol:
        return None
    ordered = bars.sort("ts_utc").filter(
        (pl.col("ts_utc") >= session_open_utc)
        & (pl.col("ts_utc") < session_open_utc + timedelta(minutes=390))
    )
    symbols = ordered.get_column("symbol").unique().to_list()
    if len(symbols) != 1 or ordered.height < 27:
        return None
    rows = list(ordered.iter_rows(named=True))
    expected = [session_open_utc + timedelta(minutes=i) for i in range(15)]
    if [row["ts_utc"] for row in rows[:15]] != expected:
        return None
    h15 = max(float(row["high"]) for row in rows[:15])
    if sum(int(row["volume"]) for row in rows[:15]) < config.minimum_h15_volume:
        return None
    closes = [float(row["close"]) for row in rows]
    fast = _ema(closes, 12)
    slow = _ema(closes, 26)
    macd = [left - right for left, right in zip(fast, slow, strict=True)]
    spread = config.relative_spread if relative_spread is None else relative_spread
    if spread < 0:
        raise ValueError("relative_spread must be nonnegative")
    if spread > config.maximum_entry_relative_spread:
        return None

    cutoff = session_open_utc + timedelta(minutes=config.signal_cutoff_minutes)
    liquidation = session_open_utc + timedelta(minutes=config.liquidation_minutes)
    for index in range(25, len(rows) - 1):
        signal = rows[index]
        signal_ts = signal["ts_utc"] + timedelta(minutes=1)
        if signal_ts > cutoff:
            break
        if not (
            closes[index] > h15
            and closes[index] / prior_close - 1 >= config.minimum_gap_return
            and macd[index] > 0
            and macd[index - 2] < macd[index - 1] < macd[index]
        ):
            continue
        entry_row = rows[index + 1]
        entry_px = float(entry_row["open"]) * (1 + spread / 2 + config.market_impact_pct)
        stop_level = min(float(row["low"]) for row in rows[index - 4 : index + 1])
        if stop_level >= entry_px:
            continue
        all_in_stop_pct = (
            (entry_px - stop_level) / entry_px + config.stop_slippage_reserve_pct
        )
        if all_in_stop_pct > config.max_all_in_stop_pct:
            continue
        target_level = entry_px + config.target_r * (entry_px - stop_level)
        cumulative_value = sum(
            float(row["close"]) * int(row["volume"]) for row in rows[: index + 1]
        )
        cumulative_volume = sum(int(row["volume"]) for row in rows[: index + 1])
        previous_macd = macd[index]
        for exit_index in range(index + 1, len(rows)):
            row = rows[exit_index]
            if row["ts_utc"] >= liquidation:
                return _trade(
                    symbols[0],
                    signal_ts,
                    entry_row["ts_utc"],
                    entry_px,
                    stop_level,
                    target_level,
                    row["ts_utc"],
                    float(row["open"]),
                    "time_exit",
                    all_in_stop_pct,
                    h15,
                    macd[index],
                    premarket_rvol,
                    spread,
                    config,
                )
            cumulative_value += float(row["close"]) * int(row["volume"])
            cumulative_volume += int(row["volume"])
            if float(row["low"]) <= stop_level:
                return _trade(
                    symbols[0],
                    signal_ts,
                    entry_row["ts_utc"],
                    entry_px,
                    stop_level,
                    target_level,
                    row["ts_utc"] + timedelta(minutes=1),
                    stop_level,
                    "stop",
                    all_in_stop_pct,
                    h15,
                    macd[index],
                    premarket_rvol,
                    spread,
                    config,
                )
            if float(row["high"]) >= target_level:
                return _trade(
                    symbols[0],
                    signal_ts,
                    entry_row["ts_utc"],
                    entry_px,
                    stop_level,
                    target_level,
                    row["ts_utc"] + timedelta(minutes=1),
                    target_level,
                    "target_3r",
                    all_in_stop_pct,
                    h15,
                    macd[index],
                    premarket_rvol,
                    spread,
                    config,
                )
            session_vwap = cumulative_value / cumulative_volume
            if (
                exit_index > index + 1
                and float(row["close"]) < session_vwap
                and macd[exit_index] <= previous_macd
            ):
                return _trade(
                    symbols[0],
                    signal_ts,
                    entry_row["ts_utc"],
                    entry_px,
                    stop_level,
                    target_level,
                    row["ts_utc"] + timedelta(minutes=1),
                    float(row["close"]),
                    "trend_exit",
                    all_in_stop_pct,
                    h15,
                    macd[index],
                    premarket_rvol,
                    spread,
                    config,
                )
            previous_macd = macd[exit_index]
        last = rows[-1]
        return _trade(
            symbols[0],
            signal_ts,
            entry_row["ts_utc"],
            entry_px,
            stop_level,
            target_level,
            last["ts_utc"] + timedelta(minutes=1),
            float(last["close"]),
            "data_end",
            all_in_stop_pct,
            h15,
            macd[index],
            premarket_rvol,
            spread,
            config,
        )
    return None


def evaluate_modern_momentum_reentry(
    bars: pl.DataFrame,
    *,
    session_open_utc: datetime,
    first_trade: ModernMomentumTrade,
    config: ModernMomentumConfig,
    relative_spread: float,
) -> ModernMomentumTrade | None:
    """Replay one smaller re-entry after a first-attempt protective stop."""
    if first_trade.exit_reason != "stop":
        return None
    ordered = bars.sort("ts_utc").filter(
        (pl.col("ts_utc") >= session_open_utc)
        & (pl.col("ts_utc") < session_open_utc + timedelta(minutes=390))
    )
    fives = _five_minute_bars(ordered, session_open_utc=session_open_utc)
    signal = None
    for bar in fives:
        asof = bar.ts_utc + timedelta(minutes=5)
        signal = pullback_reentry(
            fives,
            stopped_at_utc=first_trade.exit_ts_utc,
            h15=first_trade.h15,
            asof_utc=asof,
        )
        if signal is not None:
            break
    if signal is None:
        return None
    rows = list(ordered.iter_rows(named=True))
    entry_index = next(
        (
            index
            for index, row in enumerate(rows)
            if row["ts_utc"] == signal.signal_ts_utc
        ),
        None,
    )
    if entry_index is None or relative_spread < 0:
        return None
    entry_px = float(rows[entry_index]["open"]) * (
        1 + relative_spread / 2 + config.market_impact_pct
    )
    stop_level = max(signal.structural_stop, entry_px * 0.985)
    all_in_stop_pct = (
        (entry_px - stop_level) / entry_px + config.stop_slippage_reserve_pct
    )
    if all_in_stop_pct > config.max_all_in_stop_pct + 1e-12:
        return None
    target_level = entry_px + config.target_r * (entry_px - stop_level)
    liquidation = session_open_utc + timedelta(minutes=config.liquidation_minutes)
    for index in range(entry_index, len(rows)):
        row = rows[index]
        if row["ts_utc"] >= liquidation:
            return _trade(
                first_trade.symbol,
                signal.signal_ts_utc,
                signal.signal_ts_utc,
                entry_px,
                stop_level,
                target_level,
                row["ts_utc"],
                float(row["open"]),
                "time_exit",
                all_in_stop_pct,
                first_trade.h15,
                first_trade.macd,
                first_trade.premarket_rvol,
                relative_spread,
                config,
            )
        if float(row["low"]) <= stop_level:
            return _trade(
                first_trade.symbol,
                signal.signal_ts_utc,
                signal.signal_ts_utc,
                entry_px,
                stop_level,
                target_level,
                row["ts_utc"] + timedelta(minutes=1),
                stop_level,
                "stop",
                all_in_stop_pct,
                first_trade.h15,
                first_trade.macd,
                first_trade.premarket_rvol,
                relative_spread,
                config,
            )
        if float(row["high"]) >= target_level:
            return _trade(
                first_trade.symbol,
                signal.signal_ts_utc,
                signal.signal_ts_utc,
                entry_px,
                stop_level,
                target_level,
                row["ts_utc"] + timedelta(minutes=1),
                target_level,
                "target_3r",
                all_in_stop_pct,
                first_trade.h15,
                first_trade.macd,
                first_trade.premarket_rvol,
                relative_spread,
                config,
            )
        reason = reentry_exit_reason(
            fives,
            entered_at_utc=signal.signal_ts_utc,
            asof_utc=row["ts_utc"] + timedelta(minutes=1),
            target_level=target_level,
            liquidation_utc=liquidation,
        )
        if reason == "trend_exit" and index + 1 < len(rows):
            next_row = rows[index + 1]
            return _trade(
                first_trade.symbol,
                signal.signal_ts_utc,
                signal.signal_ts_utc,
                entry_px,
                stop_level,
                target_level,
                next_row["ts_utc"],
                float(next_row["open"]),
                reason,
                all_in_stop_pct,
                first_trade.h15,
                first_trade.macd,
                first_trade.premarket_rvol,
                relative_spread,
                config,
            )
    last = rows[-1]
    return _trade(
        first_trade.symbol,
        signal.signal_ts_utc,
        signal.signal_ts_utc,
        entry_px,
        stop_level,
        target_level,
        last["ts_utc"] + timedelta(minutes=1),
        float(last["close"]),
        "data_end",
        all_in_stop_pct,
        first_trade.h15,
        first_trade.macd,
        first_trade.premarket_rvol,
        relative_spread,
        config,
    )


def _trade(
    symbol: str,
    signal_ts: datetime,
    entry_ts: datetime,
    entry_px: float,
    stop: float,
    target: float,
    exit_ts: datetime,
    raw_exit: float,
    reason: str,
    all_in_stop_pct: float,
    h15: float,
    macd: float,
    rvol: float,
    spread: float,
    config: ModernMomentumConfig,
) -> ModernMomentumTrade:
    exit_px = raw_exit * (1 - spread / 2 - config.market_impact_pct)
    return ModernMomentumTrade(
        str(symbol),
        signal_ts,
        entry_ts,
        entry_px,
        stop,
        target,
        exit_ts,
        exit_px,
        reason,
        all_in_stop_pct,
        h15,
        macd,
        rvol,
    )
