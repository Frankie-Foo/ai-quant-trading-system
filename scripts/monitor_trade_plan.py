"""Poll the live SIP feed and push governed trade-plan state changes to VPS IM.

The monitor is advisory-only. It never calls a broker order endpoint and never
assumes that a notification was filled. Stop-loss monitoring becomes active
only after an actual position is recorded in the configured position file.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock_time
from pathlib import Path
from statistics import median
from typing import Any, cast
from zoneinfo import ZoneInfo

import httpx
import polars as pl
from dotenv import load_dotenv

from data_plane.http import DownloadError
from data_plane.providers.alpaca import (
    fetch_quotes,
    fetch_sparse_bars_for_monitoring,
    stock_data_policy_from_env,
)
from schedule.runtime import ProcessLock

ROOT = Path(__file__).resolve().parents[1]
EASTERN = ZoneInfo("America/New_York")
BEIJING = ZoneInfo("Asia/Shanghai")
DEFAULT_CONFIG = ROOT / "config" / "trade_plan_2026-07-27.json"
DEFAULT_STATE = ROOT / "runs" / "trade-plan-monitor-state.json"
DEFAULT_POSITION = ROOT / "runs" / "trade-plan-position.json"
DEFAULT_LOG = ROOT / "runs" / "trade-plan-monitor.jsonl"
DEFAULT_LOCK = ROOT / "runs" / "trade-plan-monitor.lock"
LIVERMORE_APP_ID = "vbot_ROHePX5GpUs1cr9I"
LIVERMORE_PUSH_URL = "https://vps-service.vertu.cn/v1/im/user-robots/push"


@dataclass(frozen=True)
class SymbolPlan:
    symbol: str
    priority: int
    premarket_high: float
    premarket_vwap: float
    support_low: float
    support_high: float
    reclaim_price: float
    pullback_stop: float
    breakout_stop: float
    max_spread_ratio: float
    max_chase_ratio: float
    max_risk: float
    max_notional: float
    minimum_opening_dollar_volume: float


@dataclass(frozen=True)
class MonitorConfig:
    trade_date: date
    poll_seconds: int
    entry_window_end_et: clock_time
    exit_decision_time_bjt: clock_time
    force_exit_time_bjt: clock_time
    market_close_time_et: clock_time
    channel_id: str
    account_value: float
    daily_loss_limit: float
    plans: tuple[SymbolPlan, ...]


@dataclass(frozen=True)
class Position:
    symbol: str
    entry: float | None
    shares: int
    stop: float


@dataclass(frozen=True)
class Quote:
    symbol: str
    observed_at_utc: datetime
    bid: float
    ask: float

    @property
    def midpoint(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread_ratio(self) -> float:
        midpoint = self.midpoint
        return math.inf if midpoint <= 0 else (self.ask - self.bid) / midpoint


@dataclass(frozen=True)
class Signal:
    event: str
    symbol: str
    reason: str
    message: str
    dedupe_key: str


def _parse_time(value: str) -> clock_time:
    return clock_time.fromisoformat(value)


def load_config(path: Path) -> MonitorConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))
    plans = tuple(
        SymbolPlan(
            symbol=str(item["symbol"]).upper(),
            priority=int(item["priority"]),
            premarket_high=float(item["premarket_high"]),
            premarket_vwap=float(item["premarket_vwap"]),
            support_low=float(item["support_low"]),
            support_high=float(item["support_high"]),
            reclaim_price=float(item["reclaim_price"]),
            pullback_stop=float(item["pullback_stop"]),
            breakout_stop=float(item["breakout_stop"]),
            max_spread_ratio=float(item["max_spread_ratio"]),
            max_chase_ratio=float(item["max_chase_ratio"]),
            max_risk=float(item["max_risk"]),
            max_notional=float(item["max_notional"]),
            minimum_opening_dollar_volume=float(
                item["minimum_opening_dollar_volume"]
            ),
        )
        for item in raw["plans"]
    )
    config = MonitorConfig(
        trade_date=date.fromisoformat(str(raw["trade_date"])),
        poll_seconds=int(raw["poll_seconds"]),
        entry_window_end_et=_parse_time(str(raw["entry_window_end_et"])),
        exit_decision_time_bjt=_parse_time(
            str(raw.get("exit_decision_time_bjt", "00:00:00"))
        ),
        force_exit_time_bjt=_parse_time(
            str(raw.get("force_exit_time_bjt", "01:00:00"))
        ),
        market_close_time_et=_parse_time(
            str(raw.get("market_close_time_et", "16:00:00"))
        ),
        channel_id=str(raw["channel_id"]),
        account_value=float(raw["account_value"]),
        daily_loss_limit=float(raw["daily_loss_limit"]),
        plans=tuple(sorted(plans, key=lambda item: item.priority)),
    )
    if config.poll_seconds < 5:
        raise ValueError("poll_seconds must be at least 5")
    if not config.plans:
        raise ValueError("at least one symbol plan is required")
    return config


def _read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def _append_log(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, default=str) + "\n")


def load_position(path: Path) -> Position | None:
    if not path.exists():
        return None
    raw = _read_json(path, {})
    if raw.get("active") is not True:
        return None
    position = Position(
        symbol=str(raw["symbol"]).upper(),
        entry=float(raw["entry"]) if raw.get("entry") is not None else None,
        shares=int(raw["shares"]),
        stop=float(raw["stop"]),
    )
    if (
        position.shares <= 0
        or position.stop <= 0
        or (
            position.entry is not None
            and (position.entry <= 0 or position.stop >= position.entry)
        )
    ):
        raise ValueError("active position has invalid entry/shares/stop")
    return position


def _latest_quotes(frame: pl.DataFrame) -> dict[str, Quote]:
    output: dict[str, Quote] = {}
    if frame.is_empty():
        return output
    for row in frame.sort("ts_utc").group_by("symbol").tail(1).iter_rows(named=True):
        timestamp = row["ts_utc"]
        if not isinstance(timestamp, datetime):
            continue
        output[str(row["symbol"])] = Quote(
            symbol=str(row["symbol"]),
            observed_at_utc=timestamp.astimezone(UTC),
            bid=float(row["bid_price"]),
            ask=float(row["ask_price"]),
        )
    return output


def _completed_regular_bars(
    bars: pl.DataFrame,
    *,
    symbol: str,
    market_open_utc: datetime,
    now_utc: datetime,
) -> pl.DataFrame:
    return (
        bars.filter(
            (pl.col("symbol") == symbol)
            & (pl.col("ts_utc") >= market_open_utc)
            & (pl.col("ts_utc") + timedelta(minutes=1) <= now_utc)
        )
        .unique(subset=["symbol", "ts_utc"], keep="last")
        .sort("ts_utc")
    )


def _session_vwap(bars: pl.DataFrame) -> float | None:
    if bars.is_empty():
        return None
    usable = bars.filter(
        pl.col("vwap").is_not_null()
        & pl.col("vwap").is_finite()
        & (pl.col("volume") > 0)
    )
    if usable.is_empty():
        return None
    volume = float(usable.get_column("volume").sum())
    if volume <= 0:
        return None
    weighted_sum = cast(
        float,
        (usable.get_column("vwap") * usable.get_column("volume")).sum(),
    )
    return weighted_sum / volume


def _volume_confirmed(bars: pl.DataFrame) -> bool:
    if bars.height < 6:
        return False
    latest = bars.row(-1, named=True)
    prior = bars.slice(bars.height - 6, 5).get_column("volume").to_list()
    baseline = median(float(value) for value in prior)
    return (
        float(latest["close"]) > float(latest["open"])
        and float(latest["volume"]) >= baseline
    )


def _opening_range(
    bars: pl.DataFrame, market_open_utc: datetime
) -> tuple[float, float, float, float] | None:
    opening = bars.filter(
        (pl.col("ts_utc") >= market_open_utc)
        & (pl.col("ts_utc") < market_open_utc + timedelta(minutes=5))
    ).sort("ts_utc")
    if opening.height != 5:
        return None
    return (
        cast(float, opening.get_column("high").max()),
        cast(float, opening.get_column("low").min()),
        float(opening.row(-1, named=True)["close"]),
        float(
            (opening.get_column("close") * opening.get_column("volume")).sum()
        ),
    )


def _position_size(plan: SymbolPlan, entry: float, stop: float) -> int:
    risk_per_share = entry - stop
    if risk_per_share <= 0 or entry <= 0:
        return 0
    risk_shares = math.floor(plan.max_risk / risk_per_share)
    notional_shares = math.floor(plan.max_notional / entry)
    return max(0, min(risk_shares, notional_shares))


def build_position_plan_message(
    position: Position,
    *,
    account_value: float,
    targets: tuple[tuple[float, int], ...],
    exit_decision_time_bjt: clock_time = clock_time(0, 0),
    force_exit_time_bjt: clock_time = clock_time(1, 0),
) -> str:
    if position.entry is None or position.entry <= 0:
        raise ValueError("actual entry price is required")
    if account_value <= 0:
        raise ValueError("account value must be positive")
    if not targets or sum(shares for _, shares in targets) != position.shares:
        raise ValueError("target shares must cover the active position")
    if any(price <= position.entry or shares <= 0 for price, shares in targets):
        raise ValueError("targets must be above entry with positive shares")

    entry = position.entry
    stop_return = (position.stop / entry - 1) * 100
    target_returns = tuple((price / entry - 1) * 100 for price, _ in targets)
    weighted_return = sum(
        target_return * shares
        for target_return, (_, shares) in zip(target_returns, targets, strict=True)
    ) / position.shares
    reward_risk = weighted_return / abs(stop_return)
    allocation = entry * position.shares / account_value * 100

    target_lines = "\n".join(
        f"- {price:.2f}：相对成本 {target_return:+.2f}%，卖出{shares}股"
        for (price, shares), target_return in zip(
            targets,
            target_returns,
            strict=True,
        )
    )
    return (
        f"【利弗莫尔｜{position.symbol}持仓执行预案】\n"
        f"实际持仓：{position.symbol} {position.shares}股\n"
        f"成交均价：{entry:.2f}\n"
        f"账户仓位占比：约{allocation:.2f}%\n"
        "退出计划：\n"
        f"- {position.stop:.2f}：相对成本 {stop_return:+.2f}%，全部止损\n"
        f"{target_lines}\n"
        f"- 分批止盈全部成交：加权收益 {weighted_return:+.2f}%，"
        f"风险收益比约{reward_risk:.2f}\n"
        f"- 第一档止盈成交后，保护位上移至{entry:.2f}\n"
        "- 不追涨，不向下补仓\n"
        f"- 北京时间{exit_decision_time_bjt.strftime('%H:%M')}做强弱决策："
        "弱则立即清仓，强才允许继续持有\n"
        f"- 北京时间{force_exit_time_bjt.strftime('%H:%M')}"
        "无条件清空全部剩余仓位，绝不过夜\n"
        "15秒行情监控只负责提示“继续持有/止盈/止损/放弃”，不自动下单。"
    )


def _beijing_session_deadline(
    config: MonitorConfig,
    deadline_time: clock_time,
) -> datetime:
    market_open_bjt = _market_open(config).astimezone(BEIJING)
    deadline = datetime.combine(
        market_open_bjt.date(),
        deadline_time,
        tzinfo=BEIJING,
    )
    if deadline <= market_open_bjt:
        deadline += timedelta(days=1)
    return deadline


def evaluate_position_time_exit(
    position: Position,
    config: MonitorConfig,
    bars: pl.DataFrame,
    quote: Quote | None,
    *,
    now_utc: datetime,
) -> Signal | None:
    local = now_utc.astimezone(EASTERN)
    decision_at = _beijing_session_deadline(
        config,
        config.exit_decision_time_bjt,
    )
    force_at = _beijing_session_deadline(config, config.force_exit_time_bjt)
    close_at = datetime.combine(
        config.trade_date,
        config.market_close_time_et,
        tzinfo=EASTERN,
    )
    day_key = config.trade_date.isoformat()
    if local >= close_at:
        return Signal(
            event="overnight_violation",
            symbol=position.symbol,
            reason="position_still_active_after_market_close",
            message=(
                "【利弗莫尔｜日内规则违规】\n"
                f"{position.symbol} 在美东16:00后仍被登记为持仓。\n"
                "如实际仍有仓位，使用支持盘后交易的限价单退出；"
                "如已平仓，立即把监控状态登记为flat。"
            ),
            dedupe_key=f"overnight:{position.symbol}:{day_key}",
        )
    if local >= force_at:
        return Signal(
            event="force_exit",
            symbol=position.symbol,
            reason="day_trade_force_exit_time",
            message=(
                "【利弗莫尔｜立即清仓】\n"
                f"已到北京时间{config.force_exit_time_bjt.strftime('%H:%M')}，"
                f"{position.symbol} 不论盈亏都必须清空剩余仓位。\n"
                "先撤销未成交止盈单，再主动卖出剩余股数；"
                "确认成交后撤销保护单并登记flat。绝不过夜。"
            ),
            dedupe_key=f"force-exit:{position.symbol}:{day_key}",
        )
    if now_utc >= decision_at.astimezone(UTC):
        required_columns = {"symbol", "ts_utc", "close", "volume", "vwap"}
        regular = (
            _completed_regular_bars(
                bars,
                symbol=position.symbol,
                market_open_utc=_market_open(config),
                now_utc=now_utc,
            )
            if required_columns.issubset(bars.columns)
            else pl.DataFrame()
        )
        session_vwap = _session_vwap(regular)
        quote_is_fresh = (
            quote is not None
            and (now_utc - quote.observed_at_utc).total_seconds() <= 30
        )
        latest_close = 0.0
        last_three_above_vwap = False
        if session_vwap is not None and regular.height >= 3:
            latest_close = float(regular.get_column("close")[-1])
            last_three_above_vwap = all(
                float(value) > session_vwap
                for value in regular.tail(3).get_column("close").to_list()
            )
        strong = False
        if (
            quote_is_fresh
            and quote is not None
            and position.entry is not None
            and session_vwap is not None
        ):
            strong = (
                quote.bid > position.entry
                and latest_close > session_vwap
                and last_three_above_vwap
            )
        if strong:
            return Signal(
                event="hold_to_force_exit",
                symbol=position.symbol,
                reason="midnight_strength_confirmed",
                message=(
                    "【利弗莫尔｜北京时间00:00强弱决策】\n"
                    f"{position.symbol} 仍高于成本与盘中VWAP，"
                    "最近3个完成分钟收盘均在VWAP上方。\n"
                    f"允许继续持有，但北京时间"
                    f"{config.force_exit_time_bjt.strftime('%H:%M')}"
                    "无条件清仓，绝不过夜。"
                ),
                dedupe_key=f"hold-to-exit:{position.symbol}:{day_key}",
            )
        return Signal(
            event="exit_now",
            symbol=position.symbol,
            reason="midnight_strength_not_confirmed",
            message=(
                "【利弗莫尔｜北京时间00:00清仓】\n"
                f"{position.symbol} 未通过强势持有条件，立即清空剩余仓位。\n"
                "条件要求：报价新鲜、价格高于成本与盘中VWAP，"
                "且最近3个完成分钟收盘均在VWAP上方。"
            ),
            dedupe_key=f"exit-now:{position.symbol}:{day_key}",
        )
    return None


def evaluate_position_stop(
    position: Position,
    quote: Quote | None,
    *,
    now_utc: datetime,
) -> Signal | None:
    if quote is None:
        return None
    age = (now_utc - quote.observed_at_utc).total_seconds()
    if age > 30 or quote.bid > position.stop:
        return None
    position_detail = f"持仓 {position.shares} 股"
    risk_detail = "实际成交均价尚未补录，暂不能计算止损百分比"
    if position.entry is not None:
        stop_return = (position.stop / position.entry - 1) * 100
        position_detail += f"，成本 {position.entry:.2f}"
        risk_detail = f"相对成交价变动 {stop_return:.2f}%"
    message = (
        "【量化监控｜触发止损】\n"
        f"{position.symbol} 买一价 {quote.bid:.2f} 已到达保护位 "
        f"{position.stop:.2f}。\n"
        f"{position_detail}，{risk_detail}。\n"
        "按预案执行退出，不向下补仓。"
    )
    return Signal(
        event="stop_loss",
        symbol=position.symbol,
        reason="bid_at_or_below_stop",
        message=message,
        dedupe_key=f"stop:{position.symbol}:{position.entry or 'unknown'}:{position.stop}",
    )


def evaluate_symbol(
    plan: SymbolPlan,
    bars: pl.DataFrame,
    quote: Quote | None,
    *,
    market_open_utc: datetime,
    now_utc: datetime,
) -> Signal | None:
    opening_end = market_open_utc + timedelta(minutes=5)
    if now_utc < opening_end or quote is None:
        return None
    quote_age = (now_utc - quote.observed_at_utc).total_seconds()
    if quote_age > 30 or quote.spread_ratio > plan.max_spread_ratio:
        return None

    regular = _completed_regular_bars(
        bars,
        symbol=plan.symbol,
        market_open_utc=market_open_utc,
        now_utc=now_utc,
    )
    opening = _opening_range(regular, market_open_utc)
    if opening is None:
        return None
    opening_high, _, opening_close, opening_dollar_volume = opening

    if opening_close < plan.support_low:
        return Signal(
            event="abandon",
            symbol=plan.symbol,
            reason="opening_range_closed_below_support",
            message=(
                "【量化监控｜放弃买入】\n"
                f"{plan.symbol} 首个5分钟收盘 {opening_close:.2f}，"
                f"低于结构支撑 {plan.support_low:.2f}，今日取消买入计划。"
            ),
            dedupe_key=f"abandon:{plan.symbol}:opening_support",
        )

    if regular.height >= 10:
        last_ten = regular.tail(10)
        if all(
            float(value) < plan.premarket_vwap
            for value in last_ten.get_column("close").to_list()
        ):
            return Signal(
                event="abandon",
                symbol=plan.symbol,
                reason="ten_minutes_below_premarket_vwap",
                message=(
                    "【量化监控｜放弃买入】\n"
                    f"{plan.symbol} 已连续10个完成分钟收在盘前均价 "
                    f"{plan.premarket_vwap:.2f} 下方，资金承接不足。"
                ),
                dedupe_key=f"abandon:{plan.symbol}:ten_below_vwap",
            )

    latest = regular.row(-1, named=True)
    latest_close = float(latest["close"])
    session_vwap = _session_vwap(regular)
    if (
        session_vwap is None
        or latest_close <= session_vwap
        or not _volume_confirmed(regular)
        or opening_dollar_volume < plan.minimum_opening_dollar_volume
    ):
        return None

    pullback_seen = regular.filter(
        (pl.col("low") >= plan.support_low)
        & (pl.col("low") <= plan.support_high)
    ).height > 0
    pullback_ready = (
        pullback_seen
        and latest_close >= plan.reclaim_price
        and latest_close <= plan.premarket_high * (1 + plan.max_chase_ratio)
    )

    trigger = max(plan.premarket_high, opening_high)
    after_opening = regular.filter(pl.col("ts_utc") >= opening_end)
    breakout_rows = after_opening.filter(pl.col("high") > trigger)
    retest_ready = False
    if not breakout_rows.is_empty():
        first_breakout = breakout_rows.get_column("ts_utc").min()
        retests = after_opening.filter(
            (pl.col("ts_utc") > first_breakout)
            & (pl.col("low") <= trigger * 1.002)
            & (pl.col("low") >= trigger * 0.995)
            & (pl.col("close") >= trigger)
        )
        retest_ready = (
            not retests.is_empty()
            and latest_close >= trigger
            and latest_close <= trigger * (1 + plan.max_chase_ratio)
        )

    if not pullback_ready and not retest_ready:
        return None

    strategy = "回踩支撑重新转强" if pullback_ready else "突破后回踩确认"
    stop = plan.pullback_stop if pullback_ready else plan.breakout_stop
    shares = _position_size(plan, latest_close, stop)
    if shares <= 0:
        return None
    stop_return = (stop / latest_close - 1) * 100
    message = (
        "【量化监控｜可以买入】\n"
        f"{plan.symbol} 出现“{strategy}”信号。\n"
        f"完成分钟收盘 {latest_close:.2f}，实时均价 {session_vwap:.2f}，"
        f"买卖价差 {quote.spread_ratio * 100:.2f}% 。\n"
        f"10万美元账户：最多 {shares} 股，限价成交，保护位 {stop:.2f}，"
        f"相对入场价风险约 {stop_return:.2f}% 。\n"
        "这是条件信号，不是自动下单；成交后需登记实际持仓才能监控止损。"
    )
    return Signal(
        event="buy_ready",
        symbol=plan.symbol,
        reason="pullback_reclaim" if pullback_ready else "breakout_retest",
        message=message,
        dedupe_key=f"buy:{plan.symbol}:{strategy}",
    )


def _entry_window_expired(config: MonitorConfig, now_utc: datetime) -> bool:
    local = now_utc.astimezone(EASTERN)
    end = datetime.combine(
        config.trade_date,
        config.entry_window_end_et,
        tzinfo=EASTERN,
    )
    return local >= end


def _send_vps(
    channel_id: str,
    signal: Signal,
    *,
    client: httpx.Client | None = None,
) -> str:
    """Push as the Livermore robot and reject ambiguous sender identity."""

    app_secret = os.getenv("VPS_LIVERMORE_APP_SECRET", "").strip()
    if not app_secret:
        raise RuntimeError("VPS_LIVERMORE_APP_SECRET is not configured")
    app_id = os.getenv("VPS_LIVERMORE_APP_ID", LIVERMORE_APP_ID).strip()
    if not app_id:
        raise RuntimeError("VPS_LIVERMORE_APP_ID is not configured")

    owns_client = client is None
    http_client = client or httpx.Client(timeout=20)
    try:
        request_body = json.dumps(
            {"channel_id": channel_id, "body": signal.message},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        response = http_client.post(
            LIVERMORE_PUSH_URL,
            headers={
                "content-type": "application/json; charset=utf-8",
                "x-vertu-bot-app-id": app_id,
                "x-vertu-bot-app-secret": app_secret,
            },
            content=request_body,
        )
        response.raise_for_status()
        payload = response.json()
    finally:
        if owns_client:
            http_client.close()

    if not isinstance(payload, dict):
        raise RuntimeError("Livermore push returned an invalid response")
    message = payload.get("message")
    if not isinstance(message, dict):
        raise RuntimeError("Livermore push did not return a message")
    message_id = message.get("id")
    if not isinstance(message_id, str) or not message_id:
        raise RuntimeError("Livermore push did not return a message id")
    if message.get("sender_type") != "bot":
        raise RuntimeError("Livermore push sender identity was not a bot")
    return message_id


def _fetch_market(
    config: MonitorConfig,
    now_utc: datetime,
) -> tuple[pl.DataFrame, dict[str, Quote]]:
    policy = stock_data_policy_from_env()
    symbols = tuple(plan.symbol for plan in config.plans)
    premarket_open = datetime.combine(
        config.trade_date,
        clock_time(4, 0),
        tzinfo=EASTERN,
    ).astimezone(UTC)
    bars, _ = fetch_sparse_bars_for_monitoring(
        symbols,
        premarket_open,
        now_utc,
        feed=policy.feed,
    )
    quotes = fetch_quotes(
        symbols,
        now_utc - timedelta(minutes=3),
        now_utc,
        feed=policy.feed,
    )
    return bars, _latest_quotes(quotes)


def _market_open(config: MonitorConfig) -> datetime:
    return datetime.combine(
        config.trade_date,
        clock_time(9, 30),
        tzinfo=EASTERN,
    ).astimezone(UTC)


def run_once(
    config: MonitorConfig,
    *,
    state_path: Path,
    position_path: Path,
    log_path: Path,
    push: bool,
    now_utc: datetime | None = None,
) -> tuple[Signal, ...]:
    observed_at = now_utc or datetime.now(UTC)
    state = _read_json(
        state_path,
        {
            "trade_date": config.trade_date.isoformat(),
            "notified": {},
            "abandoned_symbols": [],
        },
    )
    notified = state.setdefault("notified", {})
    if not isinstance(notified, dict):
        raise ValueError("monitor state notified field must be an object")

    position = load_position(position_path)
    market_error: str | None = None
    try:
        bars, quotes = _fetch_market(config, observed_at)
    except DownloadError as exc:
        bars = pl.DataFrame()
        quotes = {}
        market_error = f"{type(exc).__name__}: {exc}"

    time_exit_signal = (
        evaluate_position_time_exit(
            position,
            config,
            bars,
            quotes.get(position.symbol),
            now_utc=observed_at,
        )
        if position is not None
        else None
    )

    signals: list[Signal] = []
    if position is not None:
        stop_signal = evaluate_position_stop(
            position,
            quotes.get(position.symbol),
            now_utc=observed_at,
        )
        if stop_signal is not None:
            signals.append(stop_signal)
        elif time_exit_signal is not None:
            signals.append(time_exit_signal)
    elif _entry_window_expired(config, observed_at):
        signals.append(
            Signal(
                event="abandon",
                symbol="ALL",
                reason="entry_window_expired",
                message=(
                    "【量化监控｜放弃买入】\n"
                    "已到美东时间11:30，BX、KKR均未形成合格买点，"
                    "今日停止开新仓。"
                ),
                dedupe_key=f"abandon:ALL:{config.trade_date.isoformat()}",
            )
        )
    else:
        if market_error is None:
            market_open = _market_open(config)
            for plan in config.plans:
                signal = evaluate_symbol(
                    plan,
                    bars,
                    quotes.get(plan.symbol),
                    market_open_utc=market_open,
                    now_utc=observed_at,
                )
                if signal is None:
                    continue
                signals.append(signal)
                if signal.event == "buy_ready":
                    break

    delivered: list[Signal] = []
    for signal in signals:
        if signal.dedupe_key in notified:
            continue
        message_id = "dry-run"
        if push:
            message_id = _send_vps(config.channel_id, signal)
        notified[signal.dedupe_key] = {
            "event": signal.event,
            "symbol": signal.symbol,
            "reason": signal.reason,
            "message_id": message_id,
            "pushed_at_utc": observed_at.isoformat(),
        }
        delivered.append(signal)

    state["last_poll_utc"] = observed_at.isoformat()
    if quotes:
        state["last_quote"] = {
            symbol: {
                "bid": quote.bid,
                "ask": quote.ask,
                "observed_at_utc": quote.observed_at_utc.isoformat(),
            }
            for symbol, quote in quotes.items()
        }
    _write_json(state_path, state)
    _append_log(
        log_path,
        {
            "event": "poll",
            "observed_at_utc": observed_at,
            "symbols": [plan.symbol for plan in config.plans],
            "position_active": position is not None,
            "market_error": market_error,
            "signals": [
                {
                    "event": signal.event,
                    "symbol": signal.symbol,
                    "reason": signal.reason,
                    "delivered": signal in delivered,
                }
                for signal in signals
            ],
        },
    )
    return tuple(delivered)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--state-path", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--position-path", type=Path, default=DEFAULT_POSITION)
    parser.add_argument("--log-path", type=Path, default=DEFAULT_LOG)
    parser.add_argument("--lock-path", type=Path, default=DEFAULT_LOCK)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--no-push", action="store_true")
    return parser


def main() -> int:
    load_dotenv(ROOT / ".env")
    args = _parser().parse_args()
    config = load_config(args.config)
    with ProcessLock(args.lock_path):
        while True:
            started = time.monotonic()
            delivered = run_once(
                config,
                state_path=args.state_path,
                position_path=args.position_path,
                log_path=args.log_path,
                push=not args.no_push,
            )
            for signal in delivered:
                print(
                    json.dumps(
                        {
                            "event": signal.event,
                            "symbol": signal.symbol,
                            "reason": signal.reason,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            if args.once:
                return 0
            elapsed = time.monotonic() - started
            time.sleep(max(0.0, config.poll_seconds - elapsed))


if __name__ == "__main__":
    raise SystemExit(main())
