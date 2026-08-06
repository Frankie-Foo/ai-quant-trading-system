"""Deterministic, point-in-time technical monitoring primitives.

The module is deliberately advisory-only.  It has no broker dependency and cannot
submit, modify, or cancel an order.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal

import polars as pl

Action = Literal["BUY_ADD", "HOLD", "REDUCE_NEW_LOT", "EXIT_ALL", "NO_ACTION"]


@dataclass(frozen=True)
class AggregatedBar:
    ts_utc: datetime
    completed_at_utc: datetime
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    volume: int
    trade_count: int
    vwap: float | None
    source_bar_count: int


@dataclass(frozen=True)
class TimeframeSnapshot:
    timeframe: str
    completed_at_utc: datetime
    close: float
    boll_mid: float | None
    boll_upper: float | None
    boll_lower: float | None
    macd_dif: float | None
    macd_dea: float | None
    macd_hist: float | None
    kdj_k: float | None
    kdj_d: float | None
    kdj_j: float | None
    last_confirmed_top: float | None
    last_confirmed_bottom: float | None
    prior_confirmed_bottom: float | None
    green_volume_ratio: float | None
    bar_count: int


@dataclass(frozen=True)
class LongGreenExpansion:
    """Point-in-time evidence for a high-volume long-green session profile."""

    qualified: bool
    completed_at_utc: datetime
    elapsed_minutes: int
    session_open: float
    session_high: float
    session_low: float
    session_close: float
    session_return: float
    body_to_range: float
    close_location: float
    session_vwap: float | None
    close_vs_vwap: float | None
    cumulative_volume: int
    max_green_volume_ratio: float | None
    premarket_rvol: float | None
    score: float
    blockers: tuple[str, ...]


@dataclass(frozen=True)
class QuoteSnapshot:
    observed_at_utc: datetime
    bid: float
    ask: float
    age_seconds: float
    feed: str
    is_realtime: bool

    @property
    def midpoint(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread_ratio(self) -> float:
        midpoint = self.midpoint
        return (self.ask - self.bid) / midpoint if midpoint > 0 else math.inf


@dataclass(frozen=True)
class PositionPlan:
    position_shares: int
    position_average: float
    new_lot_shares: int
    new_lot_entry: float
    new_lot_protect: float
    all_exit: float
    add_shares: int

    def __post_init__(self) -> None:
        if self.position_shares < 0 or self.new_lot_shares < 0 or self.add_shares < 0:
            raise ValueError("share quantities must be non-negative")
        if self.new_lot_shares > self.position_shares:
            raise ValueError("new_lot_shares cannot exceed position_shares")
        if min(
            self.position_average,
            self.new_lot_entry,
            self.new_lot_protect,
            self.all_exit,
        ) <= 0:
            raise ValueError("price levels must be positive")
        if self.all_exit >= self.new_lot_protect:
            raise ValueError("all_exit must be below new_lot_protect")


@dataclass(frozen=True)
class TradeAdvisory:
    action: Action
    shares: int
    reasons: tuple[str, ...]
    blockers: tuple[str, ...]
    order_authorized: bool = False


@dataclass
class _BarAccumulator:
    ts_utc: datetime
    completed_at_utc: datetime
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    volume: int
    trade_count: int
    weighted_vwap: float
    vwap_volume: int
    source_bar_count: int

    def add(
        self,
        *,
        high: float,
        low: float,
        close: float,
        volume: int,
        trade_count: int,
        vwap: float | None,
    ) -> None:
        self.high = max(self.high, high)
        self.low = min(self.low, low)
        self.close = close
        self.volume += volume
        self.trade_count += trade_count
        self.source_bar_count += 1
        if vwap is not None and math.isfinite(vwap) and volume > 0:
            self.weighted_vwap += vwap * volume
            self.vwap_volume += volume

    def freeze(self) -> AggregatedBar:
        vwap = self.weighted_vwap / self.vwap_volume if self.vwap_volume else None
        return AggregatedBar(
            ts_utc=self.ts_utc,
            completed_at_utc=self.completed_at_utc,
            trade_date=self.trade_date,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
            trade_count=self.trade_count,
            vwap=vwap,
            source_bar_count=self.source_bar_count,
        )


def _as_utc(value: datetime, *, name: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _nullable_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise TypeError("numeric value is invalid")
    number = float(value)
    return number if math.isfinite(number) else None


def resample_completed_bars(
    frame: pl.DataFrame,
    schedule: pl.DataFrame,
    *,
    interval_minutes: int,
    as_of_utc: datetime,
) -> tuple[AggregatedBar, ...]:
    """Aggregate regular-session bars and exclude the current incomplete bucket."""

    if interval_minutes <= 0:
        raise ValueError("interval_minutes must be positive")
    as_of = _as_utc(as_of_utc, name="as_of_utc")
    required_bars = {
        "ts_utc",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "trade_count",
        "vwap",
    }
    required_schedule = {"trade_date", "market_open_utc", "market_close_utc"}
    if missing := required_bars - set(frame.columns):
        raise ValueError(f"bars missing columns: {sorted(missing)}")
    if missing := required_schedule - set(schedule.columns):
        raise ValueError(f"schedule missing columns: {sorted(missing)}")

    sessions: list[tuple[date, datetime, datetime]] = []
    for row in schedule.select(sorted(required_schedule)).iter_rows(named=True):
        sessions.append(
            (
                row["trade_date"],
                _as_utc(row["market_open_utc"], name="market_open_utc"),
                _as_utc(row["market_close_utc"], name="market_close_utc"),
            )
        )

    buckets: dict[tuple[date, datetime], _BarAccumulator] = {}
    selected = frame.select(sorted(required_bars)).sort("ts_utc")
    for row in selected.iter_rows(named=True):
        timestamp = _as_utc(row["ts_utc"], name="ts_utc")
        session = next(
            (
                item
                for item in sessions
                if item[1] <= timestamp < item[2] and timestamp < as_of
            ),
            None,
        )
        if session is None:
            continue
        trade_date, market_open, market_close = session
        minute_offset = int((timestamp - market_open).total_seconds() // 60)
        bucket_start = market_open + timedelta(
            minutes=(minute_offset // interval_minutes) * interval_minutes
        )
        bucket_end = min(
            market_close,
            bucket_start + timedelta(minutes=interval_minutes),
        )
        if bucket_end > as_of:
            continue

        high = float(row["high"])
        low = float(row["low"])
        close = float(row["close"])
        volume = int(row["volume"])
        trade_count = int(row["trade_count"])
        vwap = _nullable_float(row["vwap"])
        key = (trade_date, bucket_start)
        accumulator = buckets.get(key)
        if accumulator is None:
            weighted_vwap = vwap * volume if vwap is not None and volume > 0 else 0.0
            buckets[key] = _BarAccumulator(
                ts_utc=bucket_start,
                completed_at_utc=bucket_end,
                trade_date=trade_date,
                open=float(row["open"]),
                high=high,
                low=low,
                close=close,
                volume=volume,
                trade_count=trade_count,
                weighted_vwap=weighted_vwap,
                vwap_volume=volume if vwap is not None and volume > 0 else 0,
                source_bar_count=1,
            )
        else:
            accumulator.add(
                high=high,
                low=low,
                close=close,
                volume=volume,
                trade_count=trade_count,
                vwap=vwap,
            )
    return tuple(
        buckets[key].freeze()
        for key in sorted(buckets, key=lambda item: (item[0], item[1]))
    )


def _ema(values: Sequence[float], span: int) -> list[float]:
    if not values:
        return []
    alpha = 2 / (span + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append((alpha * value) + ((1 - alpha) * result[-1]))
    return result


def _macd(values: Sequence[float]) -> tuple[float, float, float] | None:
    if len(values) < 34:
        return None
    fast = _ema(values, 12)
    slow = _ema(values, 26)
    dif_series = [left - right for left, right in zip(fast, slow, strict=True)]
    dea_series = _ema(dif_series, 9)
    dif = dif_series[-1]
    dea = dea_series[-1]
    return dif, dea, (dif - dea) * 2


def _boll(values: Sequence[float]) -> tuple[float, float, float] | None:
    if len(values) < 20:
        return None
    window = values[-20:]
    middle = statistics.fmean(window)
    standard_deviation = statistics.pstdev(window)
    return middle, middle + (2 * standard_deviation), middle - (2 * standard_deviation)


def _kdj(bars: Sequence[AggregatedBar]) -> tuple[float, float, float] | None:
    if len(bars) < 9:
        return None
    k_value = 50.0
    d_value = 50.0
    for index in range(8, len(bars)):
        window = bars[index - 8 : index + 1]
        lowest = min(item.low for item in window)
        highest = max(item.high for item in window)
        rsv = (
            50.0
            if math.isclose(highest, lowest)
            else ((bars[index].close - lowest) / (highest - lowest)) * 100
        )
        k_value = ((2 * k_value) + rsv) / 3
        d_value = ((2 * d_value) + k_value) / 3
    return k_value, d_value, (3 * k_value) - (2 * d_value)


def _fractals(
    bars: Sequence[AggregatedBar],
) -> tuple[float | None, float | None, float | None]:
    tops: list[float] = []
    bottoms: list[float] = []
    for index in range(1, len(bars) - 1):
        previous = bars[index - 1]
        current = bars[index]
        following = bars[index + 1]
        if current.high > previous.high and current.high > following.high:
            tops.append(current.high)
        if current.low < previous.low and current.low < following.low:
            bottoms.append(current.low)
    last_top = tops[-1] if tops else None
    last_bottom = bottoms[-1] if bottoms else None
    prior_bottom = bottoms[-2] if len(bottoms) >= 2 else None
    return last_top, last_bottom, prior_bottom


def _confirmed_green_volume_ratio(bars: Sequence[AggregatedBar]) -> float | None:
    best: float | None = None
    start = max(20, len(bars) - 3)
    for index in range(start, len(bars)):
        current = bars[index]
        if current.close <= current.open:
            continue
        baseline = statistics.median(item.volume for item in bars[index - 20 : index])
        if baseline <= 0:
            continue
        ratio = current.volume / baseline
        best = ratio if best is None else max(best, ratio)
    return best


def _max_green_volume_ratio(bars: Sequence[AggregatedBar]) -> float | None:
    best: float | None = None
    for index in range(20, len(bars)):
        current = bars[index]
        if current.close <= current.open:
            continue
        baseline = statistics.median(item.volume for item in bars[index - 20 : index])
        if baseline <= 0:
            continue
        ratio = current.volume / baseline
        best = ratio if best is None else max(best, ratio)
    return best


def build_timeframe_snapshot(
    bars: Sequence[AggregatedBar],
    *,
    timeframe: str,
) -> TimeframeSnapshot | None:
    if not bars:
        return None
    closes = [item.close for item in bars]
    boll = _boll(closes)
    macd = _macd(closes)
    kdj = _kdj(bars)
    top, bottom, prior_bottom = _fractals(bars)
    latest = bars[-1]
    return TimeframeSnapshot(
        timeframe=timeframe,
        completed_at_utc=latest.completed_at_utc,
        close=latest.close,
        boll_mid=boll[0] if boll else None,
        boll_upper=boll[1] if boll else None,
        boll_lower=boll[2] if boll else None,
        macd_dif=macd[0] if macd else None,
        macd_dea=macd[1] if macd else None,
        macd_hist=macd[2] if macd else None,
        kdj_k=kdj[0] if kdj else None,
        kdj_d=kdj[1] if kdj else None,
        kdj_j=kdj[2] if kdj else None,
        last_confirmed_top=top,
        last_confirmed_bottom=bottom,
        prior_confirmed_bottom=prior_bottom,
        green_volume_ratio=_confirmed_green_volume_ratio(bars),
        bar_count=len(bars),
    )


def current_session_vwap(
    bars: Sequence[AggregatedBar],
    *,
    trade_date: date,
) -> float | None:
    selected = [
        item
        for item in bars
        if item.trade_date == trade_date and item.vwap is not None and item.volume > 0
    ]
    total_volume = sum(item.volume for item in selected)
    if not total_volume:
        return None
    weighted_total = 0.0
    for item in selected:
        if item.vwap is None:
            continue
        weighted_total += item.vwap * item.volume
    return weighted_total / total_volume


def current_session_fibonacci(
    bars: Sequence[AggregatedBar],
    *,
    trade_date: date,
) -> dict[str, float]:
    selected = [item for item in bars if item.trade_date == trade_date]
    if not selected:
        return {}
    high = max(item.high for item in selected)
    low = min(item.low for item in selected)
    distance = high - low
    return {
        "low": low,
        "23.6": high - (distance * 0.236),
        "38.2": high - (distance * 0.382),
        "50.0": high - (distance * 0.5),
        "61.8": high - (distance * 0.618),
        "78.6": high - (distance * 0.786),
        "high": high,
    }


def build_long_green_expansion(
    bars: Sequence[AggregatedBar],
    *,
    trade_date: date,
    min_session_return: float = 0.04,
    min_body_to_range: float = 0.60,
    min_close_location: float = 0.80,
    min_green_volume_ratio: float = 1.50,
    premarket_rvol: float | None = None,
    min_premarket_rvol: float = 3.0,
) -> LongGreenExpansion | None:
    """Describe a long-green expansion using only completed bars available at cutoff."""

    for name, value in (
        ("min_session_return", min_session_return),
        ("min_body_to_range", min_body_to_range),
        ("min_close_location", min_close_location),
        ("min_green_volume_ratio", min_green_volume_ratio),
        ("min_premarket_rvol", min_premarket_rvol),
    ):
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{name} must be finite and non-negative")
    if premarket_rvol is not None and (
        not math.isfinite(premarket_rvol) or premarket_rvol < 0
    ):
        raise ValueError("premarket_rvol must be finite and non-negative")
    selected = [item for item in bars if item.trade_date == trade_date]
    if not selected:
        return None
    selected.sort(key=lambda item: item.ts_utc)
    first = selected[0]
    latest = selected[-1]
    session_open = first.open
    session_high = max(item.high for item in selected)
    session_low = min(item.low for item in selected)
    session_close = latest.close
    session_range = session_high - session_low
    session_return = session_close / session_open - 1
    body_to_range = (
        max(0.0, session_close - session_open) / session_range
        if session_range > 0
        else 0.0
    )
    close_location = (
        (session_close - session_low) / session_range if session_range > 0 else 0.5
    )
    vwap = current_session_vwap(selected, trade_date=trade_date)
    close_vs_vwap = (
        session_close / vwap - 1 if vwap is not None and vwap > 0 else None
    )
    green_volume_ratio = _max_green_volume_ratio(selected)
    elapsed_minutes = int(
        (latest.completed_at_utc - first.ts_utc).total_seconds() // 60
    )

    blockers: list[str] = []
    if len(selected) < 21:
        blockers.append("insufficient_volume_history")
    if session_return < min_session_return:
        blockers.append("session_return_below_min")
    if body_to_range < min_body_to_range:
        blockers.append("body_to_range_below_min")
    if close_location < min_close_location:
        blockers.append("close_location_below_min")
    if close_vs_vwap is None or close_vs_vwap <= 0:
        blockers.append("not_above_session_vwap")
    intraday_volume_confirmed = (
        green_volume_ratio is not None
        and green_volume_ratio >= min_green_volume_ratio
    )
    premarket_volume_confirmed = (
        premarket_rvol is not None and premarket_rvol > min_premarket_rvol
    )
    if not intraday_volume_confirmed and not premarket_volume_confirmed:
        blockers.append("green_volume_expansion_below_min")

    return_score = min(max(session_return / 0.10, 0.0), 1.0) * 35
    body_score = min(max(body_to_range, 0.0), 1.0) * 25
    location_score = min(max(close_location, 0.0), 1.0) * 15
    intraday_volume_strength = (
        0.0
        if green_volume_ratio is None
        else min(max(green_volume_ratio / 3.0, 0.0), 1.0)
    )
    premarket_volume_strength = (
        0.0
        if premarket_rvol is None
        else min(max(premarket_rvol / 10.0, 0.0), 1.0)
    )
    volume_score = max(intraday_volume_strength, premarket_volume_strength) * 15
    vwap_score = (
        0.0
        if close_vs_vwap is None
        else min(max(close_vs_vwap / 0.05, 0.0), 1.0) * 10
    )
    score = return_score + body_score + location_score + volume_score + vwap_score
    return LongGreenExpansion(
        qualified=not blockers,
        completed_at_utc=latest.completed_at_utc,
        elapsed_minutes=elapsed_minutes,
        session_open=session_open,
        session_high=session_high,
        session_low=session_low,
        session_close=session_close,
        session_return=session_return,
        body_to_range=body_to_range,
        close_location=close_location,
        session_vwap=vwap,
        close_vs_vwap=close_vs_vwap,
        cumulative_volume=sum(item.volume for item in selected),
        max_green_volume_ratio=green_volume_ratio,
        premarket_rvol=premarket_rvol,
        score=score,
        blockers=tuple(blockers),
    )


def _technical_blockers(
    *,
    quote: QuoteSnapshot,
    one_minute: TimeframeSnapshot | None,
    five_minute: TimeframeSnapshot | None,
    fifteen_minute: TimeframeSnapshot | None,
    long_green_expansion: LongGreenExpansion | None,
    session_vwap: float | None,
    market_is_open: bool,
) -> list[str]:
    blockers: list[str] = []
    if not market_is_open:
        blockers.append("regular_session_closed")
    if not quote.is_realtime or quote.feed != "sip":
        blockers.append("not_realtime_sip")
    if quote.age_seconds < 0 or quote.age_seconds > 90:
        blockers.append("quote_stale")
    if quote.bid <= 0 or quote.ask <= quote.bid:
        blockers.append("invalid_quote")
    if quote.spread_ratio > 0.003:
        blockers.append("spread_too_wide")
    if session_vwap is None or quote.midpoint <= session_vwap:
        blockers.append("below_session_vwap")
    if long_green_expansion is None or not long_green_expansion.qualified:
        blockers.append("long_green_expansion_not_confirmed")

    for label, snapshot in (
        ("1m", one_minute),
        ("5m", five_minute),
        ("15m", fifteen_minute),
    ):
        if (
            snapshot is None
            or snapshot.boll_mid is None
            or snapshot.macd_hist is None
            or snapshot.kdj_k is None
            or snapshot.kdj_d is None
        ):
            blockers.append(f"{label}_indicators_unavailable")

    if (
        fifteen_minute is not None
        and fifteen_minute.boll_mid is not None
        and fifteen_minute.macd_hist is not None
        and (
            fifteen_minute.close <= fifteen_minute.boll_mid
            or fifteen_minute.macd_hist <= 0
        )
    ):
        blockers.append("15m_trend_not_confirmed")
    if (
        five_minute is not None
        and five_minute.boll_mid is not None
        and five_minute.macd_hist is not None
        and (
            five_minute.close <= five_minute.boll_mid
            or five_minute.macd_hist <= 0
        )
    ):
        blockers.append("5m_structure_not_confirmed")
    if (
        one_minute is not None
        and one_minute.macd_hist is not None
        and one_minute.kdj_k is not None
        and one_minute.kdj_d is not None
        and (
            one_minute.macd_hist <= 0
            or one_minute.kdj_k <= one_minute.kdj_d
            or (
                one_minute.boll_upper is not None
                and one_minute.close > one_minute.boll_upper * 1.002
            )
        )
    ):
        blockers.append("1m_trigger_not_confirmed")
    if (
        one_minute is None
        or one_minute.green_volume_ratio is None
        or one_minute.green_volume_ratio < 1.5
    ):
        blockers.append("no_confirmed_green_volume")
    return blockers


def build_trade_advisory(
    *,
    quote: QuoteSnapshot,
    one_minute: TimeframeSnapshot | None,
    five_minute: TimeframeSnapshot | None,
    fifteen_minute: TimeframeSnapshot | None,
    long_green_expansion: LongGreenExpansion | None,
    session_vwap: float | None,
    market_is_open: bool,
    plan: PositionPlan,
) -> TradeAdvisory:
    """Build an advisory signal; every returned object explicitly denies order authority."""

    quote_usable = (
        market_is_open
        and quote.is_realtime
        and quote.feed == "sip"
        and 0 <= quote.age_seconds <= 90
        and quote.bid > 0
        and quote.ask > quote.bid
    )
    if not quote_usable:
        return TradeAdvisory(
            action="NO_ACTION",
            shares=0,
            reasons=("行情不新鲜或不在常规交易时段，禁止据此行动",),
            blockers=tuple(
                _technical_blockers(
                    quote=quote,
                    one_minute=one_minute,
                    five_minute=five_minute,
                    fifteen_minute=fifteen_minute,
                    long_green_expansion=long_green_expansion,
                    session_vwap=session_vwap,
                    market_is_open=market_is_open,
                )
            ),
        )

    midpoint = quote.midpoint
    if plan.position_shares > 0 and midpoint <= plan.all_exit:
        return TradeAdvisory(
            action="EXIT_ALL",
            shares=plan.position_shares,
            reasons=(f"中间价跌破全仓保护位 {plan.all_exit:.2f}",),
            blockers=(),
        )
    if (
        plan.new_lot_shares > 0
        and plan.new_lot_protect > plan.new_lot_entry
        and midpoint <= plan.new_lot_protect
    ):
        return TradeAdvisory(
            action="REDUCE_NEW_LOT",
            shares=plan.new_lot_shares,
            reasons=(f"中间价跌破新增仓保护位 {plan.new_lot_protect:.2f}",),
            blockers=(),
        )

    blockers = _technical_blockers(
        quote=quote,
        one_minute=one_minute,
        five_minute=five_minute,
        fifteen_minute=fifteen_minute,
        long_green_expansion=long_green_expansion,
        session_vwap=session_vwap,
        market_is_open=market_is_open,
    )
    if not blockers and plan.add_shares > 0:
        return TradeAdvisory(
            action="BUY_ADD",
            shares=plan.add_shares,
            reasons=(
                "15分钟趋势、5分钟结构与1分钟触发同向",
                "最近3根1分钟K存在至少1.5倍放量阳线",
                "价格位于日内VWAP上方且SIP价差合格",
            ),
            blockers=(),
        )
    return TradeAdvisory(
        action="HOLD",
        shares=0,
        reasons=("持有现仓，不追价；等待全部确认条件同时成立",),
        blockers=tuple(blockers),
    )
