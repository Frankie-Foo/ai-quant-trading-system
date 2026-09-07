"""Causal, research-only H15 momentum strategy."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from math import isfinite
from typing import Any
from zoneinfo import ZoneInfo

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
    maximum_entry_relative_spread: float = 0.0025
    market_impact_pct: float = 0.0002
    stop_slippage_reserve_pct: float = 0.005
    signal_cutoff_minutes: int = 330
    liquidation_minutes: int = 380


def modern_strategy_manifest(config: ModernMomentumConfig | None = None) -> dict[str, Any]:
    """Describe the effective deterministic strategy, independent of legacy policy labels."""
    effective = asdict(config or ModernMomentumConfig())
    encoded = json.dumps(effective, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return {
        "schema_version": "modern_strategy_manifest.v1",
        "strategy_version": "modern-h15-current-signal.v4",
        "effective_config": effective,
        "config_sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        "first_entry_bar_minutes": 1,
        "reentry_bar_minutes": 5,
    }


@dataclass(frozen=True)
class ModernMomentumSignal:
    signal_ts_utc: datetime
    entry_reference: float
    stop_level: float
    h15: float
    macd: float
    premarket_rvol: float
    all_in_stop_pct: float


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


def modern_entry_allowed(
    *,
    session_open_utc: datetime,
    asof_utc: datetime,
    config: ModernMomentumConfig,
    relative_spread: float | None = None,
) -> bool:
    """Shared first/reentry time and spread gate; cutoff itself is not actionable."""
    if session_open_utc.tzinfo is None or asof_utc.tzinfo is None:
        raise ValueError("session_open_utc and asof_utc must be timezone-aware")
    spread = config.relative_spread if relative_spread is None else relative_spread
    if not isfinite(spread) or spread < 0:
        raise ValueError("relative_spread must be finite and nonnegative")
    return (
        session_open_utc
        <= asof_utc
        < session_open_utc + timedelta(minutes=config.signal_cutoff_minutes)
        and spread <= config.maximum_entry_relative_spread
    )


def pullback_reentry(
    fives: list[_FiveMinuteBar],
    *,
    stopped_at_utc: datetime,
    h15: float,
    asof_utc: datetime,
    session_open_utc: datetime | None = None,
    config: ModernMomentumConfig | None = None,
    relative_spread: float | None = None,
) -> ReentrySignal | None:
    """Require three completed 5-minute bars to recover after the first stop."""
    if stopped_at_utc.tzinfo is None or asof_utc.tzinfo is None or h15 <= 0:
        raise ValueError("timezone-aware stop/asof and positive H15 are required")
    config = config or ModernMomentumConfig()
    # Legacy callers omit the open; infer the regular US session in its market timezone.
    opened = session_open_utc or asof_utc.astimezone(ZoneInfo("America/New_York")).replace(
        hour=9, minute=30, second=0, microsecond=0
    )
    if not modern_entry_allowed(
        session_open_utc=opened,
        asof_utc=asof_utc,
        config=config,
        relative_spread=relative_spread,
    ):
        return None
    eligible = [
        bar
        for bar in fives
        if bar.ts_utc > stopped_at_utc and bar.ts_utc + timedelta(minutes=5) <= asof_utc
    ]
    if len(eligible) < 3:
        return None
    washout, support, reclaim = eligible[-3:]
    completed_at = reclaim.ts_utc + timedelta(minutes=5)
    if asof_utc - completed_at >= timedelta(minutes=5):
        return None
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
    spread = config.relative_spread if relative_spread is None else relative_spread
    entry = reclaim.close * (1 + spread / 2 + config.market_impact_pct)
    stop = max(support.low, entry * 0.985)
    all_in = (entry - stop) / entry + config.stop_slippage_reserve_pct
    if stop >= entry or all_in > config.max_all_in_stop_pct + 1e-12:
        return None
    return ReentrySignal(reclaim.close, support.low, completed_at)


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
        if bar.ts_utc >= entered_at_utc and bar.ts_utc + timedelta(minutes=5) <= asof_utc
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


def actual_position_exit_reason(
    bars: pl.DataFrame,
    *,
    session_open_utc: datetime,
    entered_at_utc: datetime,
    asof_utc: datetime,
    target_level: float,
    liquidation_utc: datetime,
    attempt: int,
) -> str | None:
    """Evaluate an actual holding, never a replayed entry/stop or historical exit.

    First-attempt trend exits retain the replay's completed-minute VWAP/MACD rule.
    Second-attempt trend exits require a newly completed 5-minute pair. Stops belong
    to broker reconciliation; target crossings before the actual entry are ignored.
    """
    if any(
        value.tzinfo is None
        for value in (
            session_open_utc,
            entered_at_utc,
            asof_utc,
            liquidation_utc,
        )
    ):
        raise ValueError("position exit timestamps must be timezone-aware")
    if attempt not in (1, 2) or not isfinite(target_level) or target_level <= 0:
        raise ValueError("attempt must be 1 or 2 and target_level finite and positive")
    if asof_utc < entered_at_utc:
        return None
    if asof_utc >= liquidation_utc:
        return "time_exit"
    if bars.is_empty():
        return None
    complete_minute = asof_utc.replace(second=0, microsecond=0)
    completed = bars.filter(
        (pl.col("ts_utc") >= session_open_utc)
        & (pl.col("ts_utc") + timedelta(minutes=1) <= complete_minute)
    ).sort("ts_utc")
    if completed.is_empty():
        return None
    rows = list(completed.iter_rows(named=True))
    current = rows[-1]
    if current["ts_utc"] + timedelta(minutes=1) != complete_minute:
        return None
    # A partial entry-minute high may predate the fill; do not infer that target hit.
    if current["ts_utc"] >= entered_at_utc and float(current["high"]) >= target_level:
        return "target_3r"
    if attempt == 2:
        if "vwap" not in completed.columns:
            return None
        fives = _five_minute_bars(completed, session_open_utc=session_open_utc)
        if not fives or fives[-1].ts_utc + timedelta(minutes=5) != complete_minute:
            return None
        return reentry_exit_reason(
            fives,
            entered_at_utc=entered_at_utc,
            asof_utc=asof_utc,
            # Current-minute target was checked above; never replay past target highs.
            target_level=float("inf"),
            liquidation_utc=liquidation_utc,
        )
    if len(rows) < 2 or rows[-2]["ts_utc"] + timedelta(minutes=1) <= entered_at_utc:
        return None
    volume = sum(float(row["volume"]) for row in rows)
    if volume <= 0:
        return None
    vwap = sum(float(row["close"]) * float(row["volume"]) for row in rows) / volume
    closes = [float(row["close"]) for row in rows]
    fast, slow = _ema(closes, 12), _ema(closes, 26)
    if closes[-1] < vwap and fast[-1] - slow[-1] <= fast[-2] - slow[-2]:
        return "trend_exit"
    return None


def _signal_inputs(
    bars: pl.DataFrame,
    *,
    session_open_utc: datetime,
    prior_close: float,
    market_cap: float,
    premarket_rvol: float,
    config: ModernMomentumConfig,
) -> tuple[list[dict[str, Any]], list[float], float] | None:
    if session_open_utc.tzinfo is None:
        raise ValueError("session_open_utc must be timezone-aware")
    if not all(isfinite(value) for value in (prior_close, market_cap, premarket_rvol)):
        return None
    if prior_close <= 0 or market_cap < config.minimum_market_cap:
        return None
    if premarket_rvol < config.minimum_premarket_rvol or bars.is_empty():
        return None
    ordered = bars.sort("ts_utc").filter(
        (pl.col("ts_utc") >= session_open_utc)
        & (pl.col("ts_utc") < session_open_utc + timedelta(minutes=390))
    )
    if ordered.get_column("symbol").n_unique() != 1 or ordered.height < 26:
        return None
    rows = list(ordered.iter_rows(named=True))
    expected = [session_open_utc + timedelta(minutes=i) for i in range(15)]
    if [row["ts_utc"] for row in rows[:15]] != expected:
        return None
    if any(
        row[field] is None or not isfinite(float(row[field]))
        for row in rows
        for field in ("open", "high", "low", "close", "volume")
    ):
        return None
    if sum(int(row["volume"]) for row in rows[:15]) < config.minimum_h15_volume:
        return None
    closes = [float(row["close"]) for row in rows]
    fast, slow = _ema(closes, 12), _ema(closes, 26)
    macd = [left - right for left, right in zip(fast, slow, strict=True)]
    return rows, macd, max(float(row["high"]) for row in rows[:15])


def _first_entry_signal(
    rows: list[dict[str, Any]],
    macd: list[float],
    index: int,
    *,
    h15: float,
    prior_close: float,
    premarket_rvol: float,
    session_open_utc: datetime,
    config: ModernMomentumConfig,
    relative_spread: float | None,
) -> ModernMomentumSignal | None:
    """First entry uses completed 1-minute bars; only reentry uses 5-minute bars."""
    signal_ts = rows[index]["ts_utc"] + timedelta(minutes=1)
    spread = config.relative_spread if relative_spread is None else relative_spread
    if not modern_entry_allowed(
        session_open_utc=session_open_utc,
        asof_utc=signal_ts,
        config=config,
        relative_spread=spread,
    ):
        return None
    close = float(rows[index]["close"])
    if not (
        close > h15
        and close / prior_close - 1 >= config.minimum_gap_return
        and macd[index] > 0
        and macd[index - 2] < macd[index - 1] < macd[index]
    ):
        return None
    stop = min(float(row["low"]) for row in rows[index - 4 : index + 1])
    estimated_entry = close * (1 + spread / 2 + config.market_impact_pct)
    all_in = (estimated_entry - stop) / estimated_entry + config.stop_slippage_reserve_pct
    if stop <= 0 or stop >= estimated_entry or all_in > config.max_all_in_stop_pct:
        return None
    return ModernMomentumSignal(signal_ts, close, stop, h15, macd[index], premarket_rvol, all_in)


def latest_modern_momentum_signal(
    bars: pl.DataFrame,
    *,
    session_open_utc: datetime,
    prior_close: float,
    market_cap: float,
    premarket_rvol: float,
    config: ModernMomentumConfig,
    asof_utc: datetime,
    relative_spread: float | None = None,
) -> ModernMomentumSignal | None:
    """Evaluate only the current completed minute, without fills or historical trades.

    Bar timestamps are minute starts. An older last bar is stale; the current
    incomplete minute and all future bars are excluded. Entry reference is the
    completed close, while all-in risk includes estimated spread/impact costs.
    """
    if asof_utc.tzinfo is None or session_open_utc.tzinfo is None:
        raise ValueError("session_open_utc and asof_utc must be timezone-aware")
    if bars.is_empty():
        return None
    complete_minute = asof_utc.replace(second=0, microsecond=0)
    prepared = _signal_inputs(
        bars.filter(pl.col("ts_utc") + timedelta(minutes=1) <= complete_minute),
        session_open_utc=session_open_utc,
        prior_close=prior_close,
        market_cap=market_cap,
        premarket_rvol=premarket_rvol,
        config=config,
    )
    if prepared is None:
        return None
    rows, macd, h15 = prepared
    if rows[-1]["ts_utc"] + timedelta(minutes=1) != complete_minute:
        return None
    return _first_entry_signal(
        rows,
        macd,
        len(rows) - 1,
        h15=h15,
        prior_close=prior_close,
        premarket_rvol=premarket_rvol,
        session_open_utc=session_open_utc,
        config=config,
        relative_spread=relative_spread,
    )


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
    prepared = _signal_inputs(
        bars,
        session_open_utc=session_open_utc,
        prior_close=prior_close,
        market_cap=market_cap,
        premarket_rvol=premarket_rvol,
        config=config,
    )
    if prepared is None:
        return None
    rows, macd, h15 = prepared
    symbols = [rows[0]["symbol"]]
    spread = config.relative_spread if relative_spread is None else relative_spread

    cutoff = session_open_utc + timedelta(minutes=config.signal_cutoff_minutes)
    liquidation = session_open_utc + timedelta(minutes=config.liquidation_minutes)
    for index in range(25, len(rows) - 1):
        signal_ts = rows[index]["ts_utc"] + timedelta(minutes=1)
        if signal_ts >= cutoff:
            break
        signal = _first_entry_signal(
            rows,
            macd,
            index,
            h15=h15,
            prior_close=prior_close,
            premarket_rvol=premarket_rvol,
            session_open_utc=session_open_utc,
            config=config,
            relative_spread=spread,
        )
        if signal is None:
            continue
        # Eligibility uses the completed close; execution is independently next-bar.
        entry_row = rows[index + 1]
        if entry_row["ts_utc"] != signal_ts:
            continue
        entry_px = float(entry_row["open"]) * (1 + spread / 2 + config.market_impact_pct)
        stop_level = signal.stop_level
        if entry_px <= 0 or stop_level >= entry_px:
            continue
        all_in_stop_pct = (entry_px - stop_level) / entry_px + config.stop_slippage_reserve_pct
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
            session_open_utc=session_open_utc,
            config=config,
            relative_spread=relative_spread,
        )
        if signal is not None:
            break
    if signal is None:
        return None
    rows = list(ordered.iter_rows(named=True))
    entry_index = next(
        (index for index, row in enumerate(rows) if row["ts_utc"] == signal.signal_ts_utc),
        None,
    )
    if entry_index is None or relative_spread < 0:
        return None
    entry_px = float(rows[entry_index]["open"]) * (
        1 + relative_spread / 2 + config.market_impact_pct
    )
    stop_level = max(signal.structural_stop, entry_px * 0.985)
    if entry_px <= 0 or stop_level >= entry_px:
        return None
    all_in_stop_pct = (entry_px - stop_level) / entry_px + config.stop_slippage_reserve_pct
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
