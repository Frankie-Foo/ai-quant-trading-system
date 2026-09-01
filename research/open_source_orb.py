"""Research-only reproduction of the public ORB-Backtester rules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import polars as pl

from research.h30_challenger import _five_minute_bars, _FiveMinuteBar


@dataclass(frozen=True)
class OrbConfig:
    opening_minutes: int = 30
    target_r: float = 1.0
    exit_minutes_after_open: int = 330
    entry_slippage_pct: float = 0.001
    exit_slippage_pct: float = 0.001
    max_price_stop_pct: float | None = None
    stop_slippage_reserve_pct: float = 0.005

    def __post_init__(self) -> None:
        if self.opening_minutes <= 0 or self.opening_minutes % 5:
            raise ValueError("opening_minutes must be a positive multiple of five")
        if self.target_r <= 0:
            raise ValueError("target_r must be positive")
        if self.max_price_stop_pct is not None:
            total = self.max_price_stop_pct + self.stop_slippage_reserve_pct
            if total > 0.02 + 1e-12:
                raise ValueError("all-in stop may not exceed 2%")


@dataclass(frozen=True)
class OrbTrade:
    symbol: str
    entry_ts_utc: datetime
    entry_px: float
    stop_level: float
    target_level: float
    exit_ts_utc: datetime
    exit_px: float
    exit_reason: str
    return_pct: float
    source_variant: str
    production_eligible: bool = False


def evaluate_orb(
    bars: pl.DataFrame,
    *,
    session_open_utc: datetime,
    config: OrbConfig,
) -> OrbTrade | None:
    """Replay the first long close breakout; ambiguous bars are stop-first."""
    if session_open_utc.tzinfo is None:
        raise ValueError("session_open_utc must be timezone-aware")
    symbols = bars.get_column("symbol").unique().to_list()
    if len(symbols) != 1:
        raise ValueError("evaluate_orb requires exactly one symbol")
    fives = _five_minute_bars(bars, session_open_utc=session_open_utc)
    opening_count = config.opening_minutes // 5
    if len(fives) <= opening_count:
        return None
    opening = fives[:opening_count]
    expected = [
        session_open_utc + timedelta(minutes=5 * index)
        for index in range(opening_count)
    ]
    if [bar.ts_utc for bar in opening] != expected:
        return None
    opening_high = max(bar.high for bar in opening)
    opening_low = min(bar.low for bar in opening)
    exit_at = session_open_utc + timedelta(minutes=config.exit_minutes_after_open)

    for index in range(opening_count, len(fives)):
        signal = fives[index]
        if signal.ts_utc >= exit_at or signal.close <= opening_high:
            continue
        signal_entry = signal.close
        entry_px = signal_entry * (1 + config.entry_slippage_pct)
        source_risk = signal_entry - opening_low
        if source_risk <= 0:
            return None
        stop_level = opening_low
        source_variant = "source_exact"
        if config.max_price_stop_pct is not None:
            stop_level = max(
                opening_low,
                entry_px * (1 - config.max_price_stop_pct),
            )
            source_variant = "system_2pct_stop"
        target_risk = source_risk
        if config.max_price_stop_pct is not None:
            target_risk = entry_px - stop_level
        target_level = entry_px + config.target_r * target_risk
        for candidate in fives[index + 1 :]:
            if candidate.ts_utc >= exit_at:
                exit_px = candidate.open * (1 - config.exit_slippage_pct)
                return _trade(
                    str(symbols[0]), signal, entry_px, stop_level, target_level,
                    candidate.ts_utc, exit_px, "time_exit", source_variant,
                )
            if candidate.low <= stop_level:
                exit_px = stop_level * (1 - config.stop_slippage_reserve_pct)
                return _trade(
                    str(symbols[0]), signal, entry_px, stop_level, target_level,
                    candidate.ts_utc + timedelta(minutes=5), exit_px, "stop", source_variant,
                )
            if candidate.high >= target_level:
                exit_px = target_level * (1 - config.exit_slippage_pct)
                return _trade(
                    str(symbols[0]), signal, entry_px, stop_level, target_level,
                    candidate.ts_utc + timedelta(minutes=5), exit_px, "target", source_variant,
                )
        last = fives[-1]
        return _trade(
            str(symbols[0]), signal, entry_px, stop_level, target_level,
            last.ts_utc + timedelta(minutes=5),
            last.close * (1 - config.exit_slippage_pct),
            "data_end", source_variant,
        )
    return None


def evaluate_stock_in_play_orb(
    bars: pl.DataFrame,
    *,
    session_open_utc: datetime,
    atr_dollars: float,
    entry_slippage_pct: float = 0.001,
    exit_slippage_pct: float = 0.001,
) -> OrbTrade | None:
    """Replay the paper's long-only 5-minute ORB branch with an EOD exit."""
    if atr_dollars <= 0:
        raise ValueError("atr_dollars must be positive")
    symbols = bars.get_column("symbol").unique().to_list()
    if len(symbols) != 1:
        raise ValueError("evaluate_stock_in_play_orb requires exactly one symbol")
    ordered = bars.sort("ts_utc")
    opening = ordered.filter(
        (pl.col("ts_utc") >= session_open_utc)
        & (pl.col("ts_utc") < session_open_utc + timedelta(minutes=5))
    ).sort("ts_utc")
    expected = [session_open_utc + timedelta(minutes=index) for index in range(5)]
    if opening.get_column("ts_utc").to_list() != expected:
        return None
    opening_rows = opening.iter_rows(named=True)
    opening_list = list(opening_rows)
    opening_open = float(opening_list[0]["open"])
    opening_close = float(opening_list[-1]["close"])
    if opening_close <= opening_open:
        return None
    opening_high = max(float(row["high"]) for row in opening_list)
    after_opening = ordered.filter(
        pl.col("ts_utc") >= session_open_utc + timedelta(minutes=5)
    ).sort("ts_utc")
    after_rows = list(after_opening.iter_rows(named=True))
    for index, bar in enumerate(after_rows):
        if float(bar["high"]) < opening_high:
            continue
        signal_entry = max(opening_high, float(bar["open"]))
        entry_px = signal_entry * (1 + entry_slippage_pct)
        stop_level = signal_entry - 0.1 * atr_dollars
        for candidate in after_rows[index:]:
            if float(candidate["low"]) <= stop_level:
                exit_px = stop_level * (1 - exit_slippage_pct)
                return OrbTrade(
                    symbol=str(symbols[0]),
                    entry_ts_utc=bar["ts_utc"],
                    entry_px=entry_px,
                    stop_level=stop_level,
                    target_level=float("inf"),
                    exit_ts_utc=candidate["ts_utc"] + timedelta(minutes=1),
                    exit_px=exit_px,
                    exit_reason="atr_stop",
                    return_pct=exit_px / entry_px - 1,
                    source_variant="paper_5m_long_adapted_universe",
                )
        last = after_rows[-1]
        exit_px = float(last["close"]) * (1 - exit_slippage_pct)
        return OrbTrade(
            symbol=str(symbols[0]),
            entry_ts_utc=bar["ts_utc"],
            entry_px=entry_px,
            stop_level=stop_level,
            target_level=float("inf"),
            exit_ts_utc=last["ts_utc"] + timedelta(minutes=1),
            exit_px=exit_px,
            exit_reason="end_of_day",
            return_pct=exit_px / entry_px - 1,
            source_variant="paper_5m_long_adapted_universe",
        )
    return None


def _trade(
    symbol: str,
    signal: _FiveMinuteBar,
    entry_px: float,
    stop_level: float,
    target_level: float,
    exit_ts_utc: datetime,
    exit_px: float,
    exit_reason: str,
    source_variant: str,
) -> OrbTrade:
    entry_ts = signal.ts_utc + timedelta(minutes=5)
    return OrbTrade(
        symbol=symbol,
        entry_ts_utc=entry_ts,
        entry_px=entry_px,
        stop_level=stop_level,
        target_level=target_level,
        exit_ts_utc=exit_ts_utc,
        exit_px=exit_px,
        exit_reason=exit_reason,
        return_pct=exit_px / entry_px - 1,
        source_variant=source_variant,
    )
