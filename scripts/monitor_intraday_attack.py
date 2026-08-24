"""Read-only monitor for the 2026-08-21 H30 capital plan."""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from datetime import time as clock_time
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

import polars as pl

from data_plane.providers.alpaca import _direct_credentials
from data_plane.providers.alpaca_direct import DirectAlpacaMarketDataClient
from kernel.config import load_config
from operations.local_env import load_project_env
from schedule.runtime import ProcessLock
from scripts.monitor_h30_plan import (
    _completed_fives,
    _quote,
    _session_vwap,
    _sustained_decline,
    h30_box,
)
from scripts.monitor_trade_plan import Signal, _send_vps

ROOT = Path(__file__).resolve().parents[1]
EASTERN = ZoneInfo("America/New_York")
TRADE_DATE = datetime(2026, 8, 21).date()
CHANNEL_ID = "4edcd570-603f-4c5f-a070-db88c48a5c9b"
BREAKOUT_PARTICIPATION_CAP = load_config(ROOT / "config.yaml").participation_cap
TOTAL_NOTIONAL_LIMIT: float | None = None
MAX_NAMES = 10
PER_NAME_LIMIT: float | None = None
MAX_BOX_WIDTH = 0.06
BREAKOUT_VOLUME_RATIO = 1.5
FIRE_STAGES = {
    "GO_SCOUT": (0.33, 0.25, 0.0),
    "GO_CONFIRM": (0.67, 0.60, 0.33),
    "GO_ATTACK": (1.00, 1.00, 0.67),
}


@dataclass(frozen=True)
class AttackPlan:
    symbol: str
    priority: int
    role: str
    sector_proxy: str
    gates: tuple[tuple[str, float], ...] = ()
    threshold: float | None = None
    max_entry: float | None = None
    vwap_companions: tuple[str, ...] = ()
    theme: str | None = None
    max_spread_ratio: float | None = None
    max_stop_fraction: float = 0.02
    minimum_volume_ratio: float = BREAKOUT_VOLUME_RATIO
    upstream_vwap_symbols: tuple[str, ...] = ()
    capital_limit: float | None = None
    risk_budget: float | None = None
    requires_capacity_check: bool = False

    @property
    def entry_eligible(self) -> bool:
        return self.role in {"clean", "confirm", "event", "rvol3", "etf_threshold"}


@dataclass(frozen=True)
class Candidate:
    plan: AttackPlan
    ask: float
    stop: float
    reclaim_close: float
    score: tuple[float, float, int]
    h30: float
    l30: float
    vwap: float
    breakout_volume_ratio: float
    trigger_kind: str


PLANS = (
    AttackPlan(
        "FCX", 1, "clean", "COPX", theme="metals", capital_limit=1_200_000, risk_budget=10_000
    ),
    AttackPlan(
        "HOOD", 2, "clean", "IBIT", theme="crypto", capital_limit=1_000_000, risk_budget=10_000
    ),
    AttackPlan(
        "AVGO",
        3,
        "clean",
        "SOXX",
        theme="semis",
        minimum_volume_ratio=1.8,
        upstream_vwap_symbols=("SOXX", "SMH"),
        capital_limit=1_000_000,
        risk_budget=8_000,
    ),
    AttackPlan(
        "LITE",
        4,
        "clean",
        "SOXX",
        theme="semis",
        minimum_volume_ratio=1.8,
        capital_limit=800_000,
        risk_budget=8_000,
    ),
    AttackPlan(
        "CF",
        5,
        "clean",
        "XLB",
        theme="agriculture",
        max_spread_ratio=0.0035,
        capital_limit=600_000,
        risk_budget=5_000,
        requires_capacity_check=True,
    ),
    AttackPlan(
        "ROST", 6, "confirm", "XLY", theme="consumer", capital_limit=800_000, risk_budget=8_000
    ),
    AttackPlan(
        "NEM",
        7,
        "confirm",
        "GDX",
        theme="metals",
        upstream_vwap_symbols=("GLD", "GDX"),
        capital_limit=700_000,
        risk_budget=7_000,
    ),
    AttackPlan(
        "DE",
        8,
        "clean",
        "XLB",
        theme="agriculture",
        capital_limit=700_000,
        risk_budget=6_000,
        requires_capacity_check=True,
    ),
    AttackPlan(
        "MSTR", 9, "confirm", "IBIT", theme="crypto", capital_limit=500_000, risk_budget=8_000
    ),
    AttackPlan(
        "LUNR",
        10,
        "rvol3",
        "IWM",
        theme="space",
        max_spread_ratio=0.0035,
        max_stop_fraction=0.015,
        minimum_volume_ratio=2.5,
        capital_limit=500_000,
        risk_budget=4_000,
    ),
)
ACTIVE_SYMBOLS = (
    "FCX",
    "HOOD",
    "AVGO",
    "LITE",
    "CF",
    "ROST",
    "NEM",
    "DE",
    "MSTR",
    "LUNR",
)
ACTIVE_PLANS = tuple(plan for plan in PLANS if plan.symbol in ACTIVE_SYMBOLS)
ETF_PLANS: tuple[AttackPlan, ...] = ()
WATCH_PLANS = ACTIVE_PLANS
MARKET_PROXIES = ("SPY", "QQQ")
STATUS_LABELS = {
    "opening_h30_breakout_pending": "等待首根5分钟放量收破H30",
    "secondary_box_breakout_pending": "二级箱未有效突破",
    "secondary_box_frozen_wait_5m_close": "已越过箱顶，等5分钟收盘确认",
    "clean_breakout_incomplete": "等待放量突破确认",
    "breakout_half_volume_pullback_reclaim_incomplete": "等待缩量回踩与收复",
    "secondary_box_pending": "二级箱尚未形成",
    "vwap_not_rising": "VWAP未上行",
    "index_and_sector_sustained_decline": "指数与板块持续走弱",
    "upstream_below_vwap": "上游ETF未站稳VWAP",
    "structure_stop_over_limit": "结构止损超过上限",
    "spread_too_wide": "实时点差过宽",
    "breakout_capacity_insufficient": "突破成交容量不足",
    "new_standard_entries_closed_after_1430": "14:30后禁止标准新开仓",
    "context_data_missing": "指数或板块数据未就绪",
    "upstream_quote_or_vwap_missing": "上游报价或VWAP未就绪",
}
STRONG_WATCH_BLOCKERS = {
    "opening_h30_breakout_pending",
    "secondary_box_breakout_pending",
    "secondary_box_frozen_wait_5m_close",
    "clean_breakout_incomplete",
    "breakout_half_volume_pullback_reclaim_incomplete",
}
NO_BUY_BLOCKERS = {
    "index_and_sector_sustained_decline",
    "upstream_below_vwap",
    "structure_stop_over_limit",
    "spread_too_wide",
    "breakout_capacity_insufficient",
    "new_standard_entries_closed_after_1430",
}


def box_within_atr(*, h30: float, l30: float, atr: float) -> bool:
    return atr > 0 and (h30 - l30) <= atr * 0.5 and (h30 - l30) / l30 <= MAX_BOX_WIDTH


def rolling_box(frame: pl.DataFrame, *, now_utc: datetime) -> dict[str, object] | None:
    """Thirty completed 1m bars ending before current minute."""

    end = now_utc.replace(second=0, microsecond=0)
    start = end - timedelta(minutes=30)
    rows = frame.filter(
        (pl.col("ts_utc") >= start)
        & (pl.col("ts_utc") < end)
        & (pl.col("ts_utc") + timedelta(minutes=1) <= now_utc)
    ).sort("ts_utc")
    if rows.height != 30 or rows.get_column("ts_utc").n_unique() != 30:
        return None
    volumes = []
    closes = []
    for index in range(6):
        five = rows.slice(index * 5, 5)
        volumes.append(float(five.get_column("volume").sum()))
        closes.append(float(five.row(-1, named=True)["close"]))
    return {
        "high": float(rows.get_column("high").max()),
        "low": float(rows.get_column("low").min()),
        "median_close": median(closes),
        "median_volume": median(volumes),
        "start_utc": start.isoformat(),
        "end_utc": end.isoformat(),
        "source": sorted(set(rows.get_column("source").to_list())),
    }


def secondary_box(
    frame: pl.DataFrame, *, market_open_utc: datetime, now_utc: datetime
) -> dict[str, object] | None:
    """Latest three complete post-open 5m bars define local consolidation."""

    fives = _completed_fives(frame, market_open_utc=market_open_utc, now_utc=now_utc)[6:]
    if len(fives) < 3:
        return None
    recent = fives[-3:]
    return {
        "high": max(bar["high"] for bar in recent),
        "low": min(bar["low"] for bar in recent),
        "median_volume": median(bar["volume"] for bar in recent),
        "source": ["alpaca.sip.rest.bars_or_observed_trades"],
    }


def _completed_fives_since(
    frame: pl.DataFrame,
    *,
    market_open_utc: datetime,
    now_utc: datetime,
    since_utc: datetime,
) -> list[dict[str, float]]:
    completed: list[dict[str, float]] = []
    elapsed = int((now_utc - market_open_utc).total_seconds() // 60)
    for index in range(elapsed // 5):
        start = market_open_utc + timedelta(minutes=index * 5)
        end = start + timedelta(minutes=5)
        if end <= since_utc:
            continue
        rows = frame.filter(
            (pl.col("ts_utc") >= start)
            & (pl.col("ts_utc") < end)
            & (pl.col("ts_utc") + timedelta(minutes=1) <= now_utc)
        ).sort("ts_utc")
        if rows.height == 5:
            completed.append(
                {
                    "high": float(rows.get_column("high").max()),
                    "low": float(rows.get_column("low").min()),
                    "close": float(rows.row(-1, named=True)["close"]),
                    "volume": float(rows.get_column("volume").sum()),
                }
            )
    return completed


def observed_trade_minute_bars(
    rows: tuple[tuple[str, dict[str, object]], ...],
) -> pl.DataFrame:
    """Build minute bars only from observed SIP trades; never fill empty minutes."""

    buckets: dict[tuple[str, datetime], list[tuple[datetime, float, float]]] = {}
    for symbol, raw in rows:
        timestamp = datetime.fromisoformat(str(raw["t"]).replace("Z", "+00:00"))
        price, size = float(raw["p"]), float(raw["s"])
        if price <= 0 or size <= 0:
            continue
        minute = timestamp.replace(second=0, microsecond=0)
        buckets.setdefault((symbol, minute), []).append((timestamp, price, size))
    result = []
    for (symbol, minute), trades in sorted(buckets.items()):
        trades.sort()
        prices = [trade[1] for trade in trades]
        volume = sum(trade[2] for trade in trades)
        result.append(
            {
                "symbol": symbol,
                "ts_utc": minute,
                "open": prices[0],
                "high": max(prices),
                "low": min(prices),
                "close": prices[-1],
                "volume": volume,
                "vwap": sum(trade[1] * trade[2] for trade in trades) / volume,
                "source": "alpaca.sip.rest.trades.observed_1m",
            }
        )
    return pl.DataFrame(result)


def _recover_missing_session_bars(
    bars: pl.DataFrame,
    *,
    now_utc: datetime,
    client: DirectAlpacaMarketDataClient,
) -> pl.DataFrame:
    market_open = datetime.combine(TRADE_DATE, clock_time(9, 30), tzinfo=EASTERN).astimezone(UTC)
    latest_complete_minute = now_utc.replace(second=0, microsecond=0)
    expected = {
        market_open + timedelta(minutes=index)
        for index in range(int((latest_complete_minute - market_open).total_seconds() // 60))
    }
    missing_symbols = tuple(
        plan.symbol
        for plan in ACTIVE_PLANS
        if expected
        - set(
            bars.filter(
                (pl.col("symbol") == plan.symbol)
                & (pl.col("ts_utc") >= market_open)
                & (pl.col("ts_utc") < latest_complete_minute)
            )
            .get_column("ts_utc")
            .to_list()
        )
    )
    if not missing_symbols:
        return pl.DataFrame(schema=bars.schema)
    rows = client._rows(
        "trades",
        symbols=missing_symbols,
        start_utc=market_open,
        end_utc=now_utc,
    )
    if not rows:
        return pl.DataFrame(schema=bars.schema)
    observed = observed_trade_minute_bars(rows).filter(
        (pl.col("ts_utc") >= market_open) & (pl.col("ts_utc") < latest_complete_minute)
    )
    if observed.is_empty():
        return pl.DataFrame(schema=bars.schema)
    existing = bars.select("symbol", "ts_utc").unique()
    return observed.join(existing, on=["symbol", "ts_utc"], how="anti")


def clean_breakout_ready(
    bars: list[dict[str, float]],
    *,
    h30: float,
    vwap: float,
    median_volume: float,
    minimum_volume_ratio: float = BREAKOUT_VOLUME_RATIO,
) -> tuple[float, float, float] | None:
    for bar in bars:
        ratio = bar["volume"] / median_volume if median_volume > 0 else 0.0
        if bar["close"] > max(h30, vwap) and ratio >= minimum_volume_ratio:
            return h30, bar["close"], ratio
    return None


def capacity_check(
    plan: AttackPlan,
    *,
    ask: float,
    stop: float,
    breakout_close: float,
    breakout_volume: float,
    stage: str = "GO_SCOUT",
) -> dict[str, float | bool]:
    """Bound each action's incremental notional by observed breakout liquidity."""

    if (
        plan.capital_limit is None
        or plan.risk_budget is None
        or ask <= stop
        or breakout_close <= 0
        or breakout_volume <= 0
        or stage not in FIRE_STAGES
    ):
        raise ValueError("capacity inputs are incomplete")
    capital_fraction, risk_fraction, prior_fraction = FIRE_STAGES[stage]

    def shares_for(capital_fraction: float, risk_fraction: float) -> int:
        return int(
            min(
                plan.capital_limit * capital_fraction / ask,
                plan.risk_budget * risk_fraction / (ask - stop),
            )
        )

    target_shares = shares_for(capital_fraction, risk_fraction)
    prior_shares = (
        0
        if prior_fraction == 0
        else shares_for(prior_fraction, 0.25 if stage == "GO_CONFIRM" else 0.60)
    )
    action_notional = max(0, target_shares - prior_shares) * ask
    breakout_dollar_volume = breakout_close * breakout_volume
    capacity_notional = breakout_dollar_volume * BREAKOUT_PARTICIPATION_CAP
    return {
        "action_notional": action_notional,
        "scout_notional": (target_shares * ask if stage == "GO_SCOUT" else prior_shares * ask),
        "breakout_dollar_volume": breakout_dollar_volume,
        "capacity_notional": capacity_notional,
        "participation_ratio": (
            action_notional / breakout_dollar_volume if breakout_dollar_volume else float("inf")
        ),
        "passes": action_notional <= capacity_notional,
    }


def event_pullback_ready(
    bars: list[dict[str, float]],
    *,
    h30: float,
    vwap: float,
    median_volume: float,
) -> tuple[float, float, float] | None:
    support_floor = min(h30, vwap) * 0.998
    for index in range(len(bars) - 1):
        breakout, pullback = bars[index : index + 2]
        ratio = breakout["volume"] / median_volume if median_volume > 0 else 0.0
        if (
            breakout["close"] > max(h30, vwap)
            and ratio >= BREAKOUT_VOLUME_RATIO
            and pullback["low"] >= support_floor
            and pullback["close"] >= support_floor
            and pullback["volume"] <= breakout["volume"] * 0.5
        ):
            return min(h30, pullback["low"]), pullback["close"], ratio
    return None


def confirmed_retest_ready(
    bars: list[dict[str, float]],
    *,
    h30: float,
    vwap: float,
    median_volume: float,
    minimum_volume_ratio: float = BREAKOUT_VOLUME_RATIO,
) -> tuple[float, float, float] | None:
    support_floor = min(h30, vwap) * 0.998
    for index in range(len(bars) - 2):
        breakout, pullback, rebound = bars[index : index + 3]
        ratio = breakout["volume"] / median_volume if median_volume > 0 else 0.0
        if (
            breakout["close"] > max(h30, vwap)
            and ratio >= minimum_volume_ratio
            and pullback["low"] >= support_floor
            and pullback["close"] >= support_floor
            and pullback["volume"] <= breakout["volume"] * 0.5
            and rebound["close"] > max(pullback["high"], h30, vwap)
            and rebound["low"] >= pullback["low"]
        ):
            return min(h30, pullback["low"]), rebound["close"], ratio
    return None


def _context_allows(
    bars: pl.DataFrame,
    plan: AttackPlan,
    *,
    market_open_utc: datetime,
    now_utc: datetime,
) -> bool | None:
    symbols = (*MARKET_PROXIES, plan.sector_proxy)
    values = [
        _sustained_decline(
            bars,
            symbol=symbol,
            market_open_utc=market_open_utc,
            now_utc=now_utc,
        )
        for symbol in symbols
    ]
    if any(value is None for value in values):
        return None
    market_down = bool(values[0] or values[1])
    return not (market_down and bool(values[2]))


def _candidate(
    plan: AttackPlan,
    bars: pl.DataFrame,
    quotes: pl.DataFrame,
    *,
    market_open_utc: datetime,
    now_utc: datetime,
    atr: float | None,
    prior_high: float | None,
    frozen_box: dict[str, object] | None,
    scout_notified: bool,
) -> tuple[Candidate | None, dict[str, object]]:
    status: dict[str, object] = {"role": plan.role, "eligible": False, "blocker": None}
    if not plan.entry_eligible:
        status["blocker"] = "observation_only"
        return None, status
    if plan.role == "etf_threshold":
        return _etf_candidate(plan, bars, quotes, market_open_utc=market_open_utc, now_utc=now_utc)
    context = _context_allows(bars, plan, market_open_utc=market_open_utc, now_utc=now_utc)
    status["context_allows"] = context
    if context is None:
        status["blocker"] = "context_data_missing"
        return None, status
    if not context:
        status["blocker"] = "index_and_sector_sustained_decline"
        return None, status
    frame = bars.filter(pl.col("symbol") == plan.symbol).sort("ts_utc")
    quote = _quote(quotes, plan.symbol, now_utc)
    vwap = _session_vwap(frame)
    opening_box = h30_box(frame, market_open_utc=market_open_utc, now_utc=now_utc)
    active_box = secondary_box(frame, market_open_utc=market_open_utc, now_utc=now_utc)
    if opening_box is None or quote is None or vwap is None:
        status["blocker"] = "h30_quote_or_vwap_missing"
        return None, status
    if plan.max_spread_ratio is not None:
        midpoint = (quote[0] + quote[1]) / 2
        spread_ratio = (quote[1] - quote[0]) / midpoint if midpoint > 0 else float("inf")
        if spread_ratio > plan.max_spread_ratio:
            status.update({"spread_ratio": spread_ratio, "blocker": "spread_too_wide"})
            return None, status
    upstream_vwaps: dict[str, float] = {}
    for symbol in plan.upstream_vwap_symbols:
        upstream_quote = _quote(quotes, symbol, now_utc)
        upstream_vwap = _session_vwap(bars.filter(pl.col("symbol") == symbol))
        if upstream_quote is None or upstream_vwap is None:
            status["blocker"] = "upstream_quote_or_vwap_missing"
            return None, status
        upstream_vwaps[symbol] = upstream_vwap
        if upstream_quote[0] < upstream_vwap:
            status.update({"upstream_vwaps": upstream_vwaps, "blocker": "upstream_below_vwap"})
            return None, status
    gate_prices: dict[str, float] = {}
    for symbol, minimum in plan.gates:
        gate_quote = _quote(quotes, symbol, now_utc)
        if gate_quote is None:
            status["blocker"] = "sector_gate_quote_missing"
            return None, status
        gate_prices[symbol] = gate_quote[0]
        if gate_quote[0] < minimum:
            status.update({"gates": gate_prices, "blocker": "sector_gate_below_threshold"})
            return None, status
    prior_vwap = _session_vwap(frame.filter(pl.col("ts_utc") < now_utc - timedelta(minutes=5)))
    if prior_vwap is None or vwap <= prior_vwap:
        status.update({"vwap": vwap, "prior_vwap": prior_vwap, "blocker": "vwap_not_rising"})
        return None, status
    if active_box is None:
        if plan.role not in {"clean", "rvol3"}:
            status["blocker"] = "secondary_box_pending"
            return None, status
        completed = _completed_fives(frame, market_open_utc=market_open_utc, now_utc=now_utc)[6:]
        pattern = clean_breakout_ready(
            completed,
            h30=opening_box[0],
            vwap=vwap,
            median_volume=opening_box[3],
            minimum_volume_ratio=plan.minimum_volume_ratio,
        )
        if pattern is None:
            status.update(
                {
                    "h30": opening_box[0],
                    "l30": opening_box[1],
                    "vwap": vwap,
                    "prior_vwap": prior_vwap,
                    "blocker": "opening_h30_breakout_pending",
                }
            )
            return None, status
        structural_stop, reclaim, breakout_ratio = pattern
        if (quote[1] - structural_stop) / quote[1] > plan.max_stop_fraction:
            status["blocker"] = "structure_stop_over_limit"
            return None, status
        if plan.requires_capacity_check:
            capacity = capacity_check(
                plan,
                ask=quote[1],
                stop=structural_stop,
                breakout_close=reclaim,
                breakout_volume=breakout_ratio * opening_box[3],
            )
            status.update(capacity)
            if not bool(capacity["passes"]):
                status["blocker"] = "breakout_capacity_insufficient"
                return None, status
        candidate = Candidate(
            plan=plan,
            ask=quote[1],
            stop=structural_stop,
            reclaim_close=reclaim,
            score=(reclaim / opening_box[0], breakout_ratio, -plan.priority),
            h30=opening_box[0],
            l30=opening_box[1],
            vwap=vwap,
            breakout_volume_ratio=breakout_ratio,
            trigger_kind="opening_h30_scout",
        )
        status.update(
            {
                "eligible": True,
                "blocker": None,
                "h30": opening_box[0],
                "l30": opening_box[1],
                "vwap": vwap,
                "ask": quote[1],
                "stop": structural_stop,
                "breakout_volume_ratio": breakout_ratio,
            }
        )
        return candidate, status
    if frozen_box is None:
        h30, l30, median_volume = (
            float(active_box["high"]),
            float(active_box["low"]),
            float(opening_box[3]),
        )
        box_start, box_end, box_source = (
            "secondary_last_3_complete_5m",
            "secondary_last_3_complete_5m",
            active_box["source"],
        )
    else:
        h30, l30, median_volume = (
            float(frozen_box["high"]),
            float(frozen_box["low"]),
            float(frozen_box["median_volume"]),
        )
        box_start, box_end, box_source = (
            str(frozen_box["start_utc"]),
            str(frozen_box["end_utc"]),
            frozen_box["source"],
        )
    status.update(
        {
            "h30": opening_box[0],
            "l30": opening_box[1],
            "secondary_high": h30,
            "secondary_low": l30,
            "vwap": vwap,
            "prior_vwap": prior_vwap,
            "gates": gate_prices,
            "upstream_vwaps": upstream_vwaps,
            "box_width": (h30 - l30) / l30,
            "atr14": atr,
            "box_start_utc": box_start,
            "box_end_utc": box_end,
            "box_source": box_source,
        }
    )
    if atr is None:
        status["blocker"] = "atr14_missing"
        return None, status
    if frozen_box is None:
        if quote[1] <= max(h30, vwap):
            status["blocker"] = "secondary_box_breakout_pending"
            return None, status
        status.update(
            {
                "blocker": "secondary_box_frozen_wait_5m_close",
                "freeze_box": {
                    **active_box,
                    "median_volume": opening_box[3],
                    "start_utc": "secondary_last_3_complete_5m",
                    "end_utc": "secondary_last_3_complete_5m",
                    "frozen_at_utc": now_utc.isoformat(),
                },
            }
        )
        return None, status
    frozen_at = datetime.fromisoformat(str(frozen_box["frozen_at_utc"]))
    if now_utc - frozen_at > timedelta(minutes=15):
        status["blocker"] = "frozen_box_expired"
        status["clear_frozen_box"] = True
        return None, status
    completed = _completed_fives_since(
        frame,
        market_open_utc=market_open_utc,
        now_utc=now_utc,
        since_utc=frozen_at,
    )
    if plan.role == "clean" and not scout_notified:
        pattern = clean_breakout_ready(completed, h30=h30, vwap=vwap, median_volume=median_volume)
        trigger_kind = "secondary_scout"
    else:
        pattern = confirmed_retest_ready(
            completed,
            h30=h30,
            vwap=vwap,
            median_volume=median_volume,
            minimum_volume_ratio=plan.minimum_volume_ratio,
        )
        trigger_kind = "confirmed_retest"
    if pattern is None:
        status["blocker"] = (
            "clean_breakout_incomplete"
            if plan.role == "clean" and not scout_notified
            else "breakout_half_volume_pullback_reclaim_incomplete"
        )
        return None, status
    structural_stop, reclaim, breakout_ratio = pattern
    stop = max(structural_stop, quote[1] * (1 - plan.max_stop_fraction))
    if plan.requires_capacity_check:
        capacity = capacity_check(
            plan,
            ask=quote[1],
            stop=stop,
            breakout_close=reclaim,
            breakout_volume=breakout_ratio * median_volume,
            stage="GO_CONFIRM" if trigger_kind == "confirmed_retest" else "GO_SCOUT",
        )
        status.update(capacity)
        if not bool(capacity["passes"]):
            status["blocker"] = "breakout_capacity_insufficient"
            return None, status
    candidate = Candidate(
        plan=plan,
        ask=quote[1],
        stop=stop,
        reclaim_close=reclaim,
        score=(reclaim / h30, breakout_ratio, -plan.priority),
        h30=h30,
        l30=l30,
        vwap=vwap,
        breakout_volume_ratio=breakout_ratio,
        trigger_kind=trigger_kind,
    )
    status.update(
        {
            "eligible": True,
            "blocker": None,
            "ask": quote[1],
            "stop": stop,
            "breakout_volume_ratio": breakout_ratio,
        }
    )
    if trigger_kind == "confirmed_retest":
        status["clear_frozen_box"] = True
    return candidate, status


def _etf_candidate(
    plan: AttackPlan,
    bars: pl.DataFrame,
    quotes: pl.DataFrame,
    *,
    market_open_utc: datetime,
    now_utc: datetime,
) -> tuple[Candidate | None, dict[str, object]]:
    """Fixed ETF threshold: complete 5m close, then alert only within 2% stop risk."""

    status: dict[str, object] = {"role": plan.role, "eligible": False, "blocker": None}
    if plan.threshold is None:
        status["blocker"] = "etf_threshold_missing"
        return None, status
    frame = bars.filter(pl.col("symbol") == plan.symbol).sort("ts_utc")
    quote = _quote(quotes, plan.symbol, now_utc)
    vwap = _session_vwap(frame)
    completed = _completed_fives(frame, market_open_utc=market_open_utc, now_utc=now_utc)
    if quote is None or vwap is None or len(completed) < 7:
        status["blocker"] = "etf_quote_or_5m_vwap_missing"
        return None, status
    last = completed[-1]
    gate_prices: dict[str, float] = {}
    for symbol, minimum in plan.gates:
        gate = _quote(quotes, symbol, now_utc)
        if gate is None:
            status["blocker"] = "etf_gate_quote_missing"
            return None, status
        gate_prices[symbol] = gate[0]
        if gate[0] < minimum:
            status.update({"gates": gate_prices, "blocker": "etf_gate_below_threshold"})
            return None, status
    companions: dict[str, float | None] = {}
    for symbol in plan.vwap_companions:
        companion_vwap = _session_vwap(bars.filter(pl.col("symbol") == symbol))
        companion_quote = _quote(quotes, symbol, now_utc)
        companions[symbol] = companion_vwap
        if companion_quote is None or companion_vwap is None:
            status["blocker"] = "etf_companion_quote_or_vwap_missing"
            return None, status
        if companion_quote[0] < companion_vwap:
            status.update({"companions": companions, "blocker": "etf_companion_below_vwap"})
            return None, status
    status.update(
        {
            "threshold": plan.threshold,
            "last_5m_close": last["close"],
            "vwap": vwap,
            "gates": gate_prices,
            "companions": companions,
        }
    )
    if last["close"] <= plan.threshold:
        status["blocker"] = "etf_5m_threshold_close_pending"
        return None, status
    if quote[0] < vwap:
        status["blocker"] = "etf_below_vwap"
        return None, status
    if plan.max_entry is not None and quote[1] > plan.max_entry:
        status["blocker"] = "etf_chase_cap_exceeded"
        return None, status
    stop = plan.threshold
    if (quote[1] - stop) / quote[1] > 0.02:
        status["blocker"] = "etf_stop_risk_over_2pct"
        return None, status
    candidate = Candidate(
        plan=plan,
        ask=quote[1],
        stop=stop,
        reclaim_close=last["close"],
        score=(last["close"] / stop, 0.0, -plan.priority),
        h30=stop,
        l30=stop,
        vwap=vwap,
        breakout_volume_ratio=0.0,
        trigger_kind="etf_threshold",
    )
    status.update({"eligible": True, "blocker": None, "ask": quote[1], "stop": stop})
    return candidate, status


def size_candidate(candidate: Candidate) -> dict[str, int | float | str]:
    """Plan-sized reference only; never sends an order."""

    stage = (
        "GO_SCOUT"
        if candidate.trigger_kind in {"opening_h30_scout", "secondary_scout"}
        else "GO_CONFIRM"
    )
    capital_fraction, risk_fraction, prior_fraction = FIRE_STAGES[stage]
    plan = candidate.plan
    if plan.capital_limit is None or plan.risk_budget is None or candidate.ask <= candidate.stop:
        raise ValueError("plan size inputs are incomplete")

    def shares_for(capital_fraction: float, risk_fraction: float) -> int:
        return int(
            min(
                plan.capital_limit * capital_fraction / candidate.ask,
                plan.risk_budget * risk_fraction / (candidate.ask - candidate.stop),
            )
        )

    target_shares = shares_for(capital_fraction, risk_fraction)
    prior_shares = (
        0
        if prior_fraction == 0
        else shares_for(prior_fraction, 0.25 if stage == "GO_CONFIRM" else 0.60)
    )
    return {
        "stage": stage,
        "target_shares": target_shares,
        "incremental_shares": max(0, target_shares - prior_shares),
        "target_notional": target_shares * candidate.ask,
        "target_risk": target_shares * (candidate.ask - candidate.stop),
    }


def _buy_signal(candidate: Candidate) -> Signal:
    if candidate.trigger_kind == "etf_threshold":
        message = (
            f"【巴菲特｜实盘只读｜ETF买入条件触发】\n"
            f"{candidate.plan.symbol} 完整5分钟K收于预案阈值 {candidate.h30:.3f} 上方，"
            f"现价参考 {candidate.ask:.3f}，VWAP {candidate.vwap:.3f}，"
            f"结构保护位 {candidate.stop:.3f}。\n"
            "仅为预案提醒。请确认回踩守住阈值后再执行，本系统不下单。"
        )
        return Signal(
            "buy_ready",
            candidate.plan.symbol,
            "etf_5m_threshold_close",
            message,
            f"{TRADE_DATE.isoformat()}:etf-buy:{candidate.plan.symbol}:{candidate.h30:.4f}",
        )
    size = size_candidate(candidate)
    if candidate.trigger_kind == "opening_h30_scout":
        condition = "已完成首根5分钟H30放量突破，符合侦察级条件"
    elif candidate.trigger_kind == "secondary_scout":
        condition = "已完成二级箱5分钟放量突破，符合侦察级条件"
    else:
        condition = "已完成放量突破、缩量回踩不破与再次收复，符合加仓条件"
    message = (
        f"【巴菲特｜实盘只读｜买入条件触发】\n"
        f"{candidate.plan.symbol} {condition}。\n"
        f"参考买价 {candidate.ask:.2f}，冻结箱顶 {candidate.h30:.2f}，VWAP {candidate.vwap:.2f}，"
        f"结构保护位 {candidate.stop:.2f}，突破量能 {candidate.breakout_volume_ratio:.2f} 倍。\n"
        f"{('侦察仓建立' if size['stage'] == 'GO_SCOUT' else '加仓目标')}："
        f"目标累计 {size['target_shares']} 股，"
        f"本档参考 {size['incremental_shares']} 股，"
        f"名义约 ${size['target_notional']:.0f}，风险约 ${size['target_risk']:.0f}。\n"
        + (
            "仅为参考提醒，不下单。"
            if size["stage"] == "GO_SCOUT"
            else "仅在侦察仓已真实成交时执行本档加仓；本系统不下单。"
        )
    )
    return Signal(
        "buy_ready",
        candidate.plan.symbol,
        "secondary_box_breakout_pullback_reclaim",
        message,
        f"{TRADE_DATE.isoformat()}:{size['stage'].lower()}:{candidate.plan.symbol}:{candidate.h30:.4f}",
    )


def _push(channel_id: str, signal: Signal) -> str:
    transport = Signal(
        "plan_summary", signal.symbol, signal.reason, signal.message, signal.dedupe_key
    )
    return _send_vps(channel_id, transport)


def _symbols() -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            (
                *[plan.symbol for plan in WATCH_PLANS],
                *MARKET_PROXIES,
                *[plan.sector_proxy for plan in WATCH_PLANS],
                *[symbol for plan in WATCH_PLANS for symbol, _ in plan.gates],
                *[symbol for plan in WATCH_PLANS for symbol in plan.upstream_vwap_symbols],
                *[symbol for plan in ETF_PLANS for symbol in plan.vwap_companions],
            )
        )
    )


def _latest_quotes(
    symbols: tuple[str, ...],
    *,
    client: DirectAlpacaMarketDataClient | None = None,
) -> pl.DataFrame:
    owns_client = client is None
    if client is None:
        key, secret = _direct_credentials()
        client = DirectAlpacaMarketDataClient(key_id=key, secret_key=secret)
    try:
        payload = client._request_path(
            "/v2/stocks/quotes/latest",
            label="SIP latest quotes",
            params={"symbols": ",".join(symbols), "feed": "sip"},
        )
    finally:
        if owns_client:
            client.close()
    grouped = payload.get("quotes")
    if not isinstance(grouped, dict):
        raise ValueError("latest quote response is invalid")
    rows = []
    for symbol, raw in grouped.items():
        if not isinstance(raw, dict):
            continue
        rows.append(
            {
                "symbol": str(symbol).upper(),
                "ts_utc": datetime.fromisoformat(str(raw["t"]).replace("Z", "+00:00")),
                "bid_price": float(raw["bp"]),
                "ask_price": float(raw["ap"]),
            }
        )
    return pl.DataFrame(rows)


def _fetch_bars(
    now_utc: datetime,
    *,
    client: DirectAlpacaMarketDataClient | None = None,
) -> pl.DataFrame:
    symbols = _symbols()
    start = datetime.combine(TRADE_DATE, clock_time(9, 30), tzinfo=EASTERN).astimezone(UTC)
    owns_client = client is None
    if client is None:
        key, secret = _direct_credentials()
        client = DirectAlpacaMarketDataClient(key_id=key, secret_key=secret)
    try:
        raw_rows = client._rows(
            "bars",
            symbols=symbols,
            start_utc=start,
            end_utc=now_utc,
            extra={"timeframe": "1Min", "adjustment": "split"},
        )
    finally:
        if owns_client:
            client.close()
    rows = [
        {
            "symbol": symbol,
            "ts_utc": datetime.fromisoformat(str(raw["t"]).replace("Z", "+00:00")),
            "open": float(raw["o"]),
            "high": float(raw["h"]),
            "low": float(raw["l"]),
            "close": float(raw["c"]),
            "volume": float(raw["v"]),
            "vwap": None if raw.get("vw") is None else float(raw["vw"]),
            "source": "alpaca.sip.rest.bars",
        }
        for symbol, raw in raw_rows
    ]
    if not rows:
        raise ValueError("no observed regular-session bars")
    return pl.DataFrame(rows)


def _fetch(now_utc: datetime) -> tuple[pl.DataFrame, pl.DataFrame]:
    return _fetch_bars(now_utc), _latest_quotes(_symbols())


def _daily_atr14(client: DirectAlpacaMarketDataClient, now_utc: datetime) -> dict[str, float]:
    raw_rows = client._rows(
        "bars",
        symbols=ACTIVE_SYMBOLS,
        start_utc=now_utc.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=45),
        end_utc=now_utc,
        extra={"timeframe": "1Day", "adjustment": "split"},
    )
    grouped: dict[str, list[dict[str, object]]] = {symbol: [] for symbol in ACTIVE_SYMBOLS}
    for symbol, row in raw_rows:
        observed = datetime.fromisoformat(str(row["t"]).replace("Z", "+00:00"))
        if observed.astimezone(EASTERN).date() < TRADE_DATE:
            grouped[symbol].append(row)
    result: dict[str, float] = {}
    for symbol, rows in grouped.items():
        rows = rows[-15:]
        if len(rows) < 15:
            continue
        ranges = []
        for previous, current in zip(rows, rows[1:], strict=False):
            high, low, prior_close = (
                float(current["h"]),
                float(current["l"]),
                float(previous["c"]),
            )
            ranges.append(max(high - low, abs(high - prior_close), abs(low - prior_close)))
        result[symbol] = sum(ranges) / len(ranges)
    return result


def _prior_highs(client: DirectAlpacaMarketDataClient, now_utc: datetime) -> dict[str, float]:
    rows = client._rows(
        "bars",
        symbols=ACTIVE_SYMBOLS,
        start_utc=now_utc.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=10),
        end_utc=now_utc,
        extra={"timeframe": "1Day", "adjustment": "split"},
    )
    result: dict[str, tuple[datetime, float]] = {}
    for symbol, row in rows:
        observed = datetime.fromisoformat(str(row["t"]).replace("Z", "+00:00"))
        if observed.astimezone(EASTERN).date() < TRADE_DATE:
            result[symbol] = (observed, float(row["h"]))
    return {symbol: value[1] for symbol, value in result.items()}


def _read_state(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"trade_date": TRADE_DATE.isoformat(), "notified": {}, "push_results": []}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_state(path: Path, state: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def select_one_per_theme(candidates: list[Candidate]) -> list[Candidate]:
    selected: list[Candidate] = []
    themes: set[str] = set()
    for candidate in sorted(candidates, key=lambda item: item.score, reverse=True):
        theme = candidate.plan.theme or candidate.plan.sector_proxy
        if theme not in themes:
            selected.append(candidate)
            themes.add(theme)
    return selected


def _summary_bucket(blocker: object, *, eligible: bool) -> str:
    if eligible:
        return "可买"
    if blocker in STRONG_WATCH_BLOCKERS:
        return "强观察"
    if blocker in NO_BUY_BLOCKERS:
        return "不能买"
    return "继续观察"


def _status_summary_message(
    statuses: dict[str, object], *, selected: list[Candidate], local: datetime
) -> str:
    selected_symbols = {candidate.plan.symbol for candidate in selected}
    groups: dict[str, list[str]] = {name: [] for name in ("可买", "强观察", "继续观察", "不能买")}
    for plan in WATCH_PLANS:
        raw = statuses.get(plan.symbol)
        status = raw if isinstance(raw, dict) else {}
        blocker = status.get("blocker")
        eligible = plan.symbol in selected_symbols
        group = _summary_bucket(blocker, eligible=eligible)
        detail = "已满足全部闸门" if eligible else STATUS_LABELS.get(str(blocker), str(blocker))
        groups[group].append(f"{plan.symbol}（{detail}）")
    lines = [f"【巴菲特｜实盘只读｜{local:%H:%M} 半小时盯盘】"]
    for group in ("可买", "强观察", "继续观察", "不能买"):
        values = "；".join(groups[group]) if groups[group] else "无"
        lines.append(f"{group}：{values}")
    lines.append("仅作提醒，不下单；买入、加仓、止损动作仍即时单独推送。")
    return "\n".join(lines)


def _initial_summary_due(local: datetime) -> datetime:
    if local.minute <= 30:
        return local.replace(minute=30, second=0, microsecond=0)
    return (local + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)


def _push_due_summary(
    state: dict[str, object],
    *,
    statuses: dict[str, object],
    selected: list[Candidate],
    local: datetime,
    push: bool,
) -> None:
    if not (clock_time(10, 0) <= local.time() <= clock_time(15, 55)):
        return
    raw_due = state.get("next_summary_due_et")
    due = (
        datetime.fromisoformat(raw_due) if isinstance(raw_due, str) else _initial_summary_due(local)
    )
    if local < due:
        state["next_summary_due_et"] = due.isoformat()
        return
    dedupe_key = f"{TRADE_DATE.isoformat()}:half-hour-status:{due:%H%M}"
    notified = state.setdefault("notified", {})
    push_results = state.setdefault("push_results", [])
    if not isinstance(notified, dict) or not isinstance(push_results, list):
        raise ValueError("invalid monitor state")
    if dedupe_key not in notified:
        signal = Signal(
            "plan_summary",
            "POOL",
            "half_hour_status",
            _status_summary_message(statuses, selected=selected, local=local),
            dedupe_key,
        )
        result: dict[str, object] = {
            "at_utc": local.astimezone(UTC).isoformat(),
            "dedupe_key": dedupe_key,
            "event": signal.event,
        }
        if push:
            message_id = _push(CHANNEL_ID, signal)
            result.update({"status": "sent", "message_id": message_id})
            notified[dedupe_key] = result
        else:
            result["status"] = "dry_run"
        push_results.append(result)
    state["next_summary_due_et"] = (due + timedelta(minutes=30)).isoformat()


def run_once(
    state: dict[str, object],
    *,
    now_utc: datetime,
    push: bool,
    fetcher: Callable[[datetime], tuple[pl.DataFrame, pl.DataFrame]] = _fetch,
    atr_by_symbol: dict[str, float] | None = None,
    prior_high_by_symbol: dict[str, float] | None = None,
) -> None:
    local = now_utc.astimezone(EASTERN)
    state["last_poll_utc"] = now_utc.isoformat()
    state["last_poll_et"] = local.isoformat()
    state["poll_seconds"] = 1
    state["notional_limit"] = TOTAL_NOTIONAL_LIMIT
    state["plan_source"] = [
        "C:/Users/frank/Desktop/2026-08-21_10_stock_h30_capital_plan.html",
    ]
    state["entry_assumptions"] = {
        "max_names": MAX_NAMES,
        "per_name_notional_limit": PER_NAME_LIMIT,
        "h30_definition": "09:30-10:00_ET",
        "secondary_box_complete_5m_bars": 3,
        "breakout_volume_ratio": BREAKOUT_VOLUME_RATIO,
        "event_pullback_max_breakout_volume_fraction": 0.5,
        "stop_max_fraction": 0.02,
        "soft_scout_volume_ratio": BREAKOUT_VOLUME_RATIO,
        "rvol3_volume_ratio": 2.5,
        "breakout_participation_cap": BREAKOUT_PARTICIPATION_CAP,
        "orders_enabled": False,
    }
    if local.date() != TRADE_DATE:
        state["blocker"] = "trade_plan_date_mismatch"
        return
    if local.time() < clock_time(9, 30):
        state["phase"] = "premarket_observation_only"
        return
    bars, quotes = fetcher(now_utc)
    market_open = datetime.combine(TRADE_DATE, clock_time(9, 30), tzinfo=EASTERN).astimezone(UTC)
    if local.time() < clock_time(10, 0):
        state["phase"] = "build_h30_then_observe_breakout"
        return
    state["phase"] = "entry_window"
    candidates: list[Candidate] = []
    statuses: dict[str, object] = {}
    frozen_boxes = state.setdefault("frozen_rolling_boxes", {})
    notified = state.setdefault("notified", {})
    if not isinstance(frozen_boxes, dict):
        raise ValueError("invalid frozen rolling boxes")
    if not isinstance(notified, dict):
        raise ValueError("invalid monitor state")
    for plan in WATCH_PLANS:
        candidate, status = _candidate(
            plan,
            bars,
            quotes,
            market_open_utc=market_open,
            now_utc=now_utc,
            atr=(atr_by_symbol or {}).get(plan.symbol),
            prior_high=(prior_high_by_symbol or {}).get(plan.symbol),
            frozen_box=(
                frozen_boxes.get(plan.symbol)
                if isinstance(frozen_boxes.get(plan.symbol), dict)
                else None
            ),
            scout_notified=any(
                key.startswith(f"{TRADE_DATE.isoformat()}:go_scout:{plan.symbol}:")
                for key in notified
            ),
        )
        if local.time() >= clock_time(14, 30):
            candidate = None
            status.update({"eligible": False, "blocker": "new_standard_entries_closed_after_1430"})
        statuses[plan.symbol] = status
        frozen = status.get("freeze_box")
        if isinstance(frozen, dict):
            frozen_boxes[plan.symbol] = frozen
        if status.get("clear_frozen_box"):
            frozen_boxes.pop(plan.symbol, None)
        if candidate is not None:
            candidates.append(candidate)
    state["symbols"] = statuses
    selected = select_one_per_theme(candidates)[:MAX_NAMES]
    state["selected"] = [item.plan.symbol for item in selected]
    _push_due_summary(
        state,
        statuses=statuses,
        selected=selected,
        local=local,
        push=push,
    )
    push_results = state.setdefault("push_results", [])
    if not isinstance(notified, dict) or not isinstance(push_results, list):
        raise ValueError("invalid monitor state")
    for candidate in selected:
        signal = _buy_signal(candidate)
        if signal.dedupe_key in notified:
            continue
        result: dict[str, object] = {
            "at_utc": now_utc.isoformat(),
            "dedupe_key": signal.dedupe_key,
            "event": signal.event,
        }
        if push:
            message_id = _push(CHANNEL_ID, signal)
            result.update({"status": "sent", "message_id": message_id})
            notified[signal.dedupe_key] = result
        else:
            result["status"] = "dry_run"
        push_results.append(result)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, default=ROOT / "runs/intraday-attack-2026-08-21.json")
    parser.add_argument("--lock", type=Path, default=ROOT / "runs/intraday-attack-2026-08-21.lock")
    parser.add_argument("--no-push", action="store_true")
    args = parser.parse_args()
    load_project_env(ROOT)
    key, secret = _direct_credentials()
    quote_client = DirectAlpacaMarketDataClient(key_id=key, secret_key=secret)
    atr_by_symbol = _daily_atr14(quote_client, datetime.now(UTC))
    prior_high_by_symbol = _prior_highs(quote_client, datetime.now(UTC))
    cached_bars: pl.DataFrame | None = None
    cached_minute: datetime | None = None
    trade_fallback: pl.DataFrame | None = None
    trade_fallback_minute: datetime | None = None

    def live_fetch(now_utc: datetime) -> tuple[pl.DataFrame, pl.DataFrame]:
        nonlocal cached_bars, cached_minute, trade_fallback, trade_fallback_minute
        minute = now_utc.replace(second=0, microsecond=0)
        if cached_bars is None or cached_minute != minute:
            cached_bars = _fetch_bars(now_utc, client=quote_client)
            cached_minute = minute
        local = now_utc.astimezone(EASTERN)
        if local.time() >= clock_time(10, 0) and trade_fallback_minute != minute:
            trade_fallback = _recover_missing_session_bars(
                cached_bars, now_utc=now_utc, client=quote_client
            )
            trade_fallback_minute = minute
        all_bars = cached_bars
        if trade_fallback is not None and not trade_fallback.is_empty():
            all_bars = pl.concat((cached_bars, trade_fallback), how="vertical")
        return all_bars, _latest_quotes(_symbols(), client=quote_client)

    try:
        with ProcessLock(args.lock):
            while True:
                started = time.monotonic()
                now = datetime.now(UTC)
                state = _read_state(args.state)
                try:
                    run_once(
                        state,
                        now_utc=now,
                        push=not args.no_push,
                        fetcher=live_fetch,
                        atr_by_symbol=atr_by_symbol,
                        prior_high_by_symbol=prior_high_by_symbol,
                    )
                    state.pop("last_error", None)
                except Exception as exc:
                    state["last_error"] = {
                        "type": type(exc).__name__,
                        "at_utc": now.isoformat(),
                    }
                _write_state(args.state, state)
                local = now.astimezone(EASTERN)
                if local.date() > TRADE_DATE or (
                    local.date() == TRADE_DATE and local.time() >= clock_time(15, 55)
                ):
                    return 0
                time.sleep(max(0.0, 1.0 - (time.monotonic() - started)))
    finally:
        quote_client.close()


if __name__ == "__main__":
    raise SystemExit(main())
