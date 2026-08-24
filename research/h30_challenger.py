"""Causal H30 breakout/pullback challenger; research only."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import median
from typing import Any

import polars as pl


@dataclass(frozen=True)
class H30Config:
    max_narrow_box_pct: float = 0.05
    breakout_volume_ratio: float = 1.0
    support_tolerance_pct: float = 0.002
    entry_slippage_pct: float = 0.001
    exit_slippage_pct: float = 0.001
    price_stop_pct: float = 0.015
    stop_slippage_reserve_pct: float = 0.005
    allow_trend_continuation: bool = False
    entry_cutoff_minutes: int | None = None

    def __post_init__(self) -> None:
        if not math.isclose(
            self.price_stop_pct + self.stop_slippage_reserve_pct,
            0.02,
            abs_tol=1e-12,
        ):
            raise ValueError("price stop plus stop-slippage reserve must equal 2%")
        if self.entry_cutoff_minutes is not None and self.entry_cutoff_minutes <= 30:
            raise ValueError("entry cutoff must be after H30")


@dataclass(frozen=True)
class TradeLeg:
    entry_ts_utc: datetime
    entry_px: float
    exit_ts_utc: datetime
    exit_px: float
    exit_reason: str
    return_pct: float
    risk_fraction: float


@dataclass(frozen=True)
class H30PathResult:
    symbol: str | None
    status: str
    reason: str
    branch: str | None
    entry_route: str | None
    h30: float | None
    l30: float | None
    ema_score: int
    risk_fraction: float
    entry_ts_utc: datetime | None
    entry_px: float | None
    legs: tuple[TradeLeg, ...]
    provenance: str


@dataclass(frozen=True)
class H30ChallengerDecision:
    status: str
    reasons: tuple[str, ...]
    production_eligible: bool


def assess_h30_challenger(
    *,
    baseline_net_pnl: float,
    challenger_net_pnl: float,
    challenger_profit_factor: float | None,
    baseline_max_drawdown: float,
    challenger_max_drawdown: float,
    trade_legs: int,
    fold_wins: int,
    nbbo_cost_complete: bool,
) -> H30ChallengerDecision:
    """Apply frozen research gates; never authorize production."""
    reasons: list[str] = []
    if trade_legs < 20:
        reasons.append("fewer_than_20_trade_legs")
    if challenger_net_pnl <= baseline_net_pnl or challenger_net_pnl <= 0:
        reasons.append("no_positive_net_uplift")
    if challenger_profit_factor is None or challenger_profit_factor < 1.1:
        reasons.append("profit_factor_below_1_1")
    if challenger_max_drawdown < baseline_max_drawdown:
        reasons.append("drawdown_worse_than_baseline")
    if fold_wins < 4:
        reasons.append("fewer_than_four_fold_wins")
    if not nbbo_cost_complete:
        reasons.append("historical_nbbo_costs_missing")
    return H30ChallengerDecision(
        status="rejected" if reasons else "sandbox_passed",
        reasons=tuple(reasons),
        production_eligible=False,
    )


def sector_proxy_from_sic(sic_code: str) -> str | None:
    """Map provider SIC to a liquid sector proxy using a frozen broad taxonomy."""
    if not sic_code.isdigit():
        return None
    code = int(sic_code)
    if code in {1311, 1381, 1382, 1389, 2911}:
        return "XLE"
    if 2000 <= code <= 2199 or 5400 <= code <= 5499:
        return "XLP"
    if 2830 <= code <= 2839 or 3840 <= code <= 3859 or 8000 <= code <= 8099:
        return "XLV"
    if 3670 <= code <= 3679:
        return "SMH"
    if 3570 <= code <= 3579 or 7370 <= code <= 7379:
        return "XLK"
    if 4800 <= code <= 4899 or 7310 <= code <= 7319 or 7800 <= code <= 7849:
        return "XLC"
    if 4900 <= code <= 4999:
        return "XLU"
    if 6000 <= code <= 6499:
        return "XLF"
    if 6500 <= code <= 6799:
        return "XLRE"
    if 2200 <= code <= 2399 or 2500 <= code <= 2599 or 3710 <= code <= 3719:
        return "XLY"
    if 5200 <= code <= 5999 or 7000 <= code <= 7299 or 7900 <= code <= 7999:
        return "XLY"
    if 1000 <= code <= 1499 or 2400 <= code <= 3499:
        return "XLB"
    if 3500 <= code <= 3999 or 4000 <= code <= 4799 or 5000 <= code <= 5199:
        return "XLI"
    if 7300 <= code <= 7399:
        return "XLI"
    return None


@dataclass(frozen=True)
class _FiveMinuteBar:
    ts_utc: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    bar_vwap: float
    session_vwap: float
    ema9: float
    ema20: float


def _number(value: object, name: str) -> float:
    if not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    return float(value)


def _ema(values: list[float], span: int) -> list[float]:
    alpha = 2.0 / (span + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append(alpha * value + (1 - alpha) * result[-1])
    return result


def _five_minute_bars(
    bars: pl.DataFrame, *, session_open_utc: datetime
) -> list[_FiveMinuteBar]:
    ordered = bars.sort("ts_utc")
    raw: list[dict[str, Any]] = []
    cumulative_value = 0.0
    cumulative_volume = 0.0
    for index in range(78):
        start = session_open_utc + timedelta(minutes=index * 5)
        bucket = ordered.filter(
            (pl.col("ts_utc") >= start)
            & (pl.col("ts_utc") < start + timedelta(minutes=5))
        ).sort("ts_utc")
        expected = [start + timedelta(minutes=minute) for minute in range(5)]
        if bucket.get_column("ts_utc").to_list() != expected:
            break
        volume = float(bucket.get_column("volume").sum())
        if volume <= 0:
            break
        bucket_value = float(
            (bucket.get_column("vwap") * bucket.get_column("volume")).sum()
        )
        cumulative_value += bucket_value
        cumulative_volume += volume
        if cumulative_volume <= 0:
            continue
        raw.append(
            {
                "ts_utc": start,
                "open": float(bucket.row(0, named=True)["open"]),
                "high": _number(bucket.get_column("high").max(), "bucket high"),
                "low": _number(bucket.get_column("low").min(), "bucket low"),
                "close": float(bucket.row(-1, named=True)["close"]),
                "volume": volume,
                "bar_vwap": bucket_value / volume,
                "session_vwap": cumulative_value / cumulative_volume,
            }
        )
    if not raw:
        return []
    closes = [float(row["close"]) for row in raw]
    ema9 = _ema(closes, 9)
    ema20 = _ema(closes, 20)
    return [
        _FiveMinuteBar(
            ts_utc=row["ts_utc"],
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row["volume"]),
            bar_vwap=float(row["bar_vwap"]),
            session_vwap=float(row["session_vwap"]),
            ema9=ema9[index],
            ema20=ema20[index],
        )
        for index, row in enumerate(raw)
        if isinstance(row["ts_utc"], datetime)
    ]


def _ema_score(
    fives: list[_FiveMinuteBar], *, confirm_index: int, pullback_index: int
) -> int:
    confirm = fives[confirm_index]
    previous = fives[confirm_index - 1]
    pullback = fives[pullback_index]
    score = int(confirm.ema9 > confirm.ema20)
    score += int(confirm.ema9 > previous.ema9 and confirm.ema20 > previous.ema20)
    score += int(confirm.close > confirm.ema20 and confirm.close > confirm.session_vwap)
    near_ema = pullback.low <= max(pullback.ema9, pullback.ema20) * 1.005
    score += 2 * int(near_ema and confirm.volume > pullback.volume)
    return score


def _risk_fraction(score: int) -> float:
    if score <= 1:
        return 0.25
    if score == 2:
        return 0.5
    if score <= 4:
        return 0.75
    return 1.0


def _find_entry(
    fives: list[_FiveMinuteBar],
    *,
    start_index: int,
    h30: float,
    branch: str,
    h30_median_volume: float,
    cfg: H30Config,
    minimum_ema_score: int = 0,
) -> tuple[int, float, int, float, str] | None:
    breakout_index: int | None = None
    pullback_index: int | None = None
    for index in range(max(6, start_index), len(fives) - 1):
        bar = fives[index]
        entry = fives[index + 1]
        if (
            cfg.entry_cutoff_minutes is not None
            and entry.ts_utc
            >= fives[0].ts_utc + timedelta(minutes=cfg.entry_cutoff_minutes)
        ):
            break
        if cfg.allow_trend_continuation and index >= 8:
            recent = fives[index - 2 : index + 1]
            higher = recent[0].high < recent[1].high < recent[2].high
            higher = higher and recent[0].low < recent[1].low < recent[2].low
            accepted = all(
                item.close > h30 and item.close > item.session_vwap for item in recent
            )
            volume_ok = bar.volume >= h30_median_volume * cfg.breakout_volume_ratio
            if higher and accepted and volume_ok:
                score = _ema_score(
                    fives, confirm_index=index, pullback_index=index - 1
                )
                if score >= minimum_ema_score:
                    return (
                        index + 1,
                        entry.bar_vwap * (1 + cfg.entry_slippage_pct),
                        score,
                        _risk_fraction(score),
                        "trend_continuation",
                    )
        if breakout_index is None:
            if (
                bar.close > h30
                and bar.close > bar.session_vwap
                and bar.volume >= h30_median_volume * cfg.breakout_volume_ratio
            ):
                breakout_index = index
            continue
        breakout = fives[breakout_index]
        if pullback_index is None:
            support = h30 if branch == "narrow" else min(h30, bar.session_vwap)
            if bar.close < support * (1 - cfg.support_tolerance_pct):
                breakout_index = None
                continue
            if (
                bar.low <= max(h30, bar.session_vwap) * 1.005
                and bar.volume < breakout.volume
            ):
                pullback_index = index
            continue
        pullback = fives[pullback_index]
        if bar.close < h30 * (1 - cfg.support_tolerance_pct):
            breakout_index = None
            pullback_index = None
            continue
        if (
            bar.close > pullback.high
            and bar.close > bar.session_vwap
            and bar.volume > pullback.volume
        ):
            score = _ema_score(fives, confirm_index=index, pullback_index=pullback_index)
            if score < minimum_ema_score:
                continue
            return (
                index + 1,
                entry.bar_vwap * (1 + cfg.entry_slippage_pct),
                score,
                _risk_fraction(score),
                "retest_reclaim",
            )
    return None


def _exit(
    fives: list[_FiveMinuteBar],
    *,
    entry_index: int,
    entry_px: float,
    h30: float,
    cfg: H30Config,
) -> tuple[int, float, str]:
    stop_px = entry_px * (1 - cfg.price_stop_pct)
    below_vwap = 0
    for index in range(entry_index, len(fives)):
        bar = fives[index]
        if bar.low <= stop_px:
            return index, entry_px * 0.98, "fixed_stop_including_slippage"
        if bar.close < h30:
            return index, bar.close * (1 - cfg.exit_slippage_pct), "closed_back_inside_h30"
        below_vwap = below_vwap + 1 if bar.close < bar.session_vwap else 0
        if below_vwap >= 2:
            return index, bar.close * (1 - cfg.exit_slippage_pct), "vwap_reclaim_failed"
        if index >= entry_index + 2:
            recent = fives[index - 2 : index + 1]
            lower = recent[0].high > recent[1].high > recent[2].high
            lower = lower and recent[0].low > recent[1].low > recent[2].low
            if lower and bar.close < bar.session_vwap:
                return index, bar.close * (1 - cfg.exit_slippage_pct), "lower_high_lower_low"
    last = fives[-1]
    return len(fives) - 1, last.close * (1 - cfg.exit_slippage_pct), "time_stop"


def evaluate_h30_path(
    bars: pl.DataFrame,
    *,
    session_open_utc: datetime,
    cfg: H30Config | None = None,
) -> H30PathResult:
    """Evaluate one candidate day without changing any production strategy."""
    cfg = cfg or H30Config()
    required = {"symbol", "ts_utc", "open", "high", "low", "close", "volume", "vwap"}
    missing = required - set(bars.columns)
    if missing:
        raise ValueError(f"bars missing required columns: {sorted(missing)}")
    if session_open_utc.tzinfo is None:
        raise ValueError("session_open_utc must be timezone-aware")
    symbols = bars.get_column("symbol").unique().to_list()
    if len(symbols) > 1:
        raise ValueError("evaluate_h30_path accepts one symbol at a time")
    symbol = str(symbols[0]) if symbols else None
    provenance = "research.h30_challenger.v1|entry=next_5m_vwap|production=false"
    fives = _five_minute_bars(bars, session_open_utc=session_open_utc)
    expected_h30 = [session_open_utc + timedelta(minutes=index * 5) for index in range(6)]
    if [bar.ts_utc for bar in fives[:6]] != expected_h30:
        return H30PathResult(
            symbol,
            "blocked",
            "h30_incomplete",
            None,
            None,
            None,
            None,
            0,
            0,
            None,
            None,
            (),
            provenance,
        )
    opening = fives[:6]
    h30 = max(bar.high for bar in opening)
    l30 = min(bar.low for bar in opening)
    branch = "narrow" if (h30 - l30) / h30 <= cfg.max_narrow_box_pct else "wide"
    first = _find_entry(
        fives,
        start_index=6,
        h30=h30,
        branch=branch,
        h30_median_volume=median(bar.volume for bar in opening),
        cfg=cfg,
    )
    if first is None:
        return H30PathResult(
            symbol,
            "no_trade",
            "setup_not_confirmed",
            branch,
            None,
            h30,
            l30,
            0,
            0,
            None,
            None,
            (),
            provenance,
        )
    entry_index, entry_px, score, fraction, entry_route = first
    exit_index, exit_px, exit_reason = _exit(
        fives, entry_index=entry_index, entry_px=entry_px, h30=h30, cfg=cfg
    )
    legs = [
        TradeLeg(
            fives[entry_index].ts_utc,
            entry_px,
            fives[exit_index].ts_utc + timedelta(minutes=5),
            exit_px,
            exit_reason,
            exit_px / entry_px - 1,
            fraction,
        )
    ]
    if exit_reason == "fixed_stop_including_slippage":
        second = _find_entry(
            fives,
            start_index=exit_index + 1,
            h30=h30,
            branch=branch,
            h30_median_volume=median(bar.volume for bar in opening),
            cfg=cfg,
            minimum_ema_score=3,
        )
        if second is not None:
            second_index, second_px, second_score, second_fraction, _ = second
            second_fraction = min(first[3] / 2, 1.0 - first[3])
            if second_fraction <= 0:
                second = None
        if second is not None:
            second_index, second_px, second_score, _, _ = second
            second_exit_index, second_exit_px, second_reason = _exit(
                fives,
                entry_index=second_index,
                entry_px=second_px,
                h30=h30,
                cfg=cfg,
            )
            legs.append(
                TradeLeg(
                    fives[second_index].ts_utc,
                    second_px,
                    fives[second_exit_index].ts_utc + timedelta(minutes=5),
                    second_exit_px,
                    second_reason,
                    second_exit_px / second_px - 1,
                    second_fraction,
                )
            )
            score = max(score, second_score)
    return H30PathResult(
        symbol,
        "traded",
        "confirmed_breakout_retest",
        branch,
        entry_route,
        h30,
        l30,
        score,
        fraction,
        fives[entry_index].ts_utc,
        entry_px,
        tuple(legs),
        provenance,
    )
