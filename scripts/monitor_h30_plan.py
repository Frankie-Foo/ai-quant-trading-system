"""Read-only H30 breakout monitor; no broker dependency or order capability."""

from __future__ import annotations

import argparse
import json
import math
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

import polars as pl
from pydantic import SecretStr

from data_plane.providers.alpaca import fetch_quotes, fetch_sparse_bars_for_monitoring, stock_data_policy_from_env
from data_plane.providers.alpaca import _direct_credentials
from data_plane.providers.alpaca_direct import DirectAlpacaMarketDataClient
from execution.alpaca_paper import DirectAlpacaPaperBroker, PaperOrderRequest
from data_plane.providers.massive import fetch_ticker_details
from operations.local_env import load_project_env
from scripts.monitor_trade_plan import Signal, _send_vps
from schedule.runtime import ProcessLock

ROOT = Path(__file__).resolve().parents[1]
EASTERN = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class Plan:
    symbol: str
    max_notional: float
    scout_notional: float
    sector_proxy: str
    second_confirmation: bool = False
    replacement_for: str | None = None
    requires_retest: bool = False


PLANS = (
    Plan("HAWK", 1_800_000, 270_000, "ITA"),
    Plan("SNDK", 1_600_000, 240_000, "SMH"),
    Plan("OKTA", 1_100_000, 165_000, "IGV", second_confirmation=True),
)
FALLBACK_PLANS = (
    Plan("REZI", 1_800_000, 270_000, "ITA", replacement_for="HAWK"),
    Plan("ALAB", 1_100_000, 165_000, "SMH", second_confirmation=True, replacement_for="OKTA"),
    Plan("CAE", 1_100_000, 165_000, "ITA", requires_retest=True),
    Plan("WDC", 1_600_000, 240_000, "SMH", replacement_for="SNDK"),
)
CANDIDATE_PLANS = PLANS + FALLBACK_PLANS
SCOUT_VOLUME_RATIO = 0.8
ADD_VOLUME_RATIO = 1.5
MAX_BOX_WIDTH = 0.05
MARKET_PROXIES = ("SPY", "QQQ")
STORAGE_PEERS = ("MU", "WDC")
FALLBACK_WATCHLIST = ("REZI", "ALAB", "CAE", "WDC", "MU")
ACTIVE_SYMBOLS = ("SNDK", "WDC")
PORTFOLIO_NOTIONAL_LIMIT = 2_000_000.0
PORTFOLIO_RISK_LIMIT = 30_000.0
ADD_VOLUME_THRESHOLDS = (1.0, 1.25, 1.5)
PAPER_REENTRY_NOTIONAL = 80_000.0
PAPER_REENTRY_RISK = 5_000.0


def _daily_trend_clear(rows: list[dict[str, object]], *, session_date: datetime) -> tuple[bool, dict[str, float]]:
    """Require observed completed daily higher highs/lows and a close above SMA20."""

    completed = [
        row for row in rows
        if datetime.fromisoformat(str(row["t"]).replace("Z", "+00:00")).astimezone(EASTERN).date() < session_date.date()
    ]
    if len(completed) < 20:
        return False, {}
    latest = completed[-20:]
    closes = [float(row["c"]) for row in latest]
    highs = [float(row["h"]) for row in latest[-3:]]
    lows = [float(row["l"]) for row in latest[-3:]]
    values = {"last_close": closes[-1], "sma20": sum(closes) / len(closes)}
    return values["last_close"] > values["sma20"] and highs[0] < highs[1] < highs[2] and lows[0] < lows[1] < lows[2], values


def _write_daily_verification(path: Path, *, now_utc: datetime) -> bool:
    """Write a provenance-bound, non-estimated daily gate for today's plans."""

    local = now_utc.astimezone(EASTERN)
    symbols = tuple(plan.symbol for plan in CANDIDATE_PLANS)
    key, secret = _direct_credentials()
    client = DirectAlpacaMarketDataClient(key_id=key, secret_key=secret)
    try:
        daily_rows = client._rows(
            "bars",
            symbols=symbols,
            start_utc=(now_utc - timedelta(days=45)).replace(hour=0, minute=0, second=0, microsecond=0),
            end_utc=now_utc,
            extra={"timeframe": "1Day", "adjustment": "split"},
        )
    finally:
        client.close()
    grouped: dict[str, list[dict[str, object]]] = {symbol: [] for symbol in symbols}
    for symbol, row in daily_rows:
        grouped.setdefault(symbol, []).append(row)
    details = fetch_ticker_details(symbols, local.date())
    missing_caps = [
        symbol for symbol in symbols
        if details.filter(pl.col("symbol") == symbol).is_empty()
        or details.filter(pl.col("symbol") == symbol).row(0, named=True)["market_cap"] is None
    ]
    for symbol in missing_caps:
        details = details.filter(pl.col("symbol") != symbol).vstack(fetch_ticker_details((symbol,), local.date()))
    result: dict[str, object] = {"asof_utc": now_utc.isoformat(), "symbols": {}}
    verified = True
    for symbol in symbols:
        trend, values = _daily_trend_clear(grouped.get(symbol, []), session_date=local)
        detail = details.filter(pl.col("symbol") == symbol)
        cap = None if detail.is_empty() else detail.row(0, named=True)["market_cap"]
        cap_value = float(cap) if cap is not None else None
        passed = trend and cap_value is not None and cap_value > 1_000_000_000
        result["symbols"][symbol] = {
            "daily_trend_clear": trend,
            "market_cap_usd": cap_value,
            "market_cap_provenance": None if detail.is_empty() else detail.row(0, named=True)["provenance"],
            "daily_bar_provenance": "alpaca.sip.rest.bars:1Day",
            **values,
            "passed": passed,
        }
        verified = verified and passed
    result["verified"] = verified
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return verified


def _verified_symbols(path: Path) -> set[str]:
    try:
        symbols = json.loads(path.read_text(encoding="utf-8")).get("symbols", {})
        return {symbol for symbol, result in symbols.items() if isinstance(result, dict) and result.get("passed") is True}
    except (OSError, ValueError, TypeError):
        return set()


def _verification_covers_candidates(path: Path) -> bool:
    try:
        symbols = json.loads(path.read_text(encoding="utf-8")).get("symbols", {})
        return (
            isinstance(symbols, dict)
            and {plan.symbol for plan in CANDIDATE_PLANS}.issubset(symbols)
            and all(symbols[plan.symbol].get("market_cap_usd") is not None for plan in CANDIDATE_PLANS)
        )
    except (OSError, ValueError, TypeError):
        return False


def h30_box(
    bars: pl.DataFrame, *, market_open_utc: datetime, now_utc: datetime
) -> tuple[float, float, float, float] | None:
    """Return H30, L30, close median and volume median for six observed 5m bars."""

    buckets: list[tuple[float, float, float, float]] = []
    for index in range(6):
        start = market_open_utc + timedelta(minutes=index * 5)
        rows = bars.filter(
            (pl.col("ts_utc") >= start)
            & (pl.col("ts_utc") < start + timedelta(minutes=5))
            & (pl.col("ts_utc") + timedelta(minutes=1) <= now_utc)
        )
        if rows.height != 5:
            return None
        rows = rows.sort("ts_utc")
        buckets.append((
            float(rows.get_column("high").max()),
            float(rows.get_column("low").min()),
            float(rows.row(-1, named=True)["close"]),
            float(rows.get_column("volume").sum()),
        ))
    return (
        max(item[0] for item in buckets),
        min(item[1] for item in buckets),
        median(item[2] for item in buckets),
        median(item[3] for item in buckets),
    )


def _session_vwap(bars: pl.DataFrame) -> float | None:
    usable = bars.filter((pl.col("volume") > 0) & pl.col("vwap").is_not_null())
    if usable.is_empty():
        return None
    volume = float(usable.get_column("volume").sum())
    return None if volume <= 0 else float((usable.get_column("vwap") * usable.get_column("volume")).sum()) / volume


def _last_complete_five(bars: pl.DataFrame, *, market_open_utc: datetime, now_utc: datetime) -> tuple[float, float] | None:
    elapsed = int((now_utc - market_open_utc).total_seconds() // 60)
    index = elapsed // 5 - 1
    if index < 6:
        return None
    start = market_open_utc + timedelta(minutes=index * 5)
    rows = bars.filter((pl.col("ts_utc") >= start) & (pl.col("ts_utc") < start + timedelta(minutes=5))).sort("ts_utc")
    if rows.height != 5 or start + timedelta(minutes=5) > now_utc:
        return None
    return float(rows.row(-1, named=True)["close"]), float(rows.get_column("volume").sum())


def _scout_trigger(
    bars: pl.DataFrame,
    *,
    high: float,
    vwap: float,
    market_open_utc: datetime,
    now_utc: datetime,
) -> tuple[float, float] | None:
    """Return (scout stop, latest 1m volume) after the two-minute 1m trigger."""

    completed = bars.filter(pl.col("ts_utc") + timedelta(minutes=1) <= now_utc).sort("ts_utc")
    if completed.height < 32:
        return None
    recent = completed.tail(3)
    if recent.height != 3:
        return None
    rows = list(recent.iter_rows(named=True))
    lows = [float(row["low"]) for row in rows]
    closes = [float(row["close"]) for row in rows]
    if not (closes[-2] > high and closes[-1] > high and closes[-1] > vwap):
        return None
    if not (lows[0] <= lows[1] <= lows[2]):
        return None
    baseline = bars.filter(
        (pl.col("ts_utc") >= market_open_utc)
        & (pl.col("ts_utc") < market_open_utc + timedelta(minutes=30))
    ).get_column("volume")
    if baseline.len() < 20:
        return None
    latest_volume = float(recent.row(-1, named=True)["volume"])
    if latest_volume < float(baseline.median()) * SCOUT_VOLUME_RATIO:
        return None
    return min(lows), latest_volume


def _completed_fives(frame: pl.DataFrame, *, market_open_utc: datetime, now_utc: datetime) -> list[dict[str, float]]:
    completed: list[dict[str, float]] = []
    elapsed = int((now_utc - market_open_utc).total_seconds() // 60)
    for index in range(elapsed // 5):
        start = market_open_utc + timedelta(minutes=index * 5)
        rows = frame.filter(
            (pl.col("ts_utc") >= start)
            & (pl.col("ts_utc") < start + timedelta(minutes=5))
            & (pl.col("ts_utc") + timedelta(minutes=1) <= now_utc)
        ).sort("ts_utc")
        if rows.height == 5:
            completed.append({
                "high": float(rows.get_column("high").max()),
                "low": float(rows.get_column("low").min()),
                "close": float(rows.row(-1, named=True)["close"]),
                "volume": float(rows.get_column("volume").sum()),
            })
    return completed


def _pullback_add_ready(
    frame: pl.DataFrame,
    *,
    h30: float,
    vwap: float,
    median_volume: float,
    entry: float,
    stage: int,
    market_open_utc: datetime,
    now_utc: datetime,
) -> bool:
    """Require breakout, support-respecting pullback, and a reclaim before adding."""

    fives = _completed_fives(frame, market_open_utc=market_open_utc, now_utc=now_utc)
    if len(fives) < 3:
        return False
    stage_index = min(max(stage, 0), len(ADD_VOLUME_THRESHOLDS) - 1)
    threshold = median_volume * ADD_VOLUME_THRESHOLDS[stage_index]
    breakout, pullback, rebound = fives[-3:]
    support = max(h30, vwap)
    support_floor = min(h30, vwap) * 0.998
    if not (
        breakout["close"] > h30
        and breakout["volume"] >= threshold
        and pullback["low"] >= support_floor
        and pullback["low"] <= support * 1.005
        and rebound["close"] > support
        and rebound["close"] > entry
        and rebound["volume"] >= threshold
    ):
        return False
    if stage_index == 0:
        return True
    if not (pullback["high"] >= breakout["high"] * 0.995 and rebound["high"] > pullback["high"] and rebound["low"] >= pullback["low"]):
        return False
    if stage_index == 1:
        return True
    return (
        rebound["high"] > breakout["high"]
        and rebound["low"] > breakout["low"]
        and pullback["close"] > support
    )


def _position_signal(
    plan: Plan,
    position: dict[str, object],
    bars: pl.DataFrame,
    *,
    market_open_utc: datetime,
    now_utc: datetime,
    native_box: tuple[float, float, float, float] | None,
    allow_add: bool = True,
) -> Signal | None:
    frame = bars.filter(pl.col("symbol") == plan.symbol).sort("ts_utc")
    if frame.is_empty() or native_box is None:
        return None
    one_minute = frame.filter(pl.col("ts_utc") + timedelta(minutes=1) <= now_utc).sort("ts_utc")
    if one_minute.is_empty():
        return None
    latest_minute = one_minute.row(-1, named=True)
    fives = _completed_fives(frame, market_open_utc=market_open_utc, now_utc=now_utc)
    if not fives:
        return None
    last = fives[-1]
    vwap = _session_vwap(frame)
    entry = float(position["entry"])
    stop_value = position.get("stop")
    stop = float(stop_value) if stop_value is not None else None
    if float(latest_minute["close"]) <= native_box[1] or (stop is not None and float(latest_minute["low"]) <= stop):
        return Signal("stop_loss", plan.symbol, "scout_low_or_h30_break", "", f"exit:{plan.symbol}:low-or-h30:{float(latest_minute['close']):.4f}")
    if vwap is not None and len(frame) >= 2:
        one_min = one_minute.tail(2).get_column("close").to_list()
        if len(one_min) == 2 and float(one_min[0]) < vwap and float(one_min[1]) < vwap:
            return Signal("stop_loss", plan.symbol, "vwap_not_reclaimed", "", f"exit:{plan.symbol}:vwap:{one_min[-1]:.4f}")
    stage = int(position.get("add_stage", 0))
    if allow_add and _pullback_add_ready(
        frame,
        h30=native_box[0],
        vwap=vwap or native_box[0],
        median_volume=native_box[3],
        entry=entry,
        stage=stage,
        market_open_utc=market_open_utc,
        now_utc=now_utc,
    ):
        return Signal("add_ready", plan.symbol, f"pullback_reclaim_stage_{stage + 1}", "", f"add:{plan.symbol}:stage-{stage + 1}:{last['close']:.4f}")
    return None


def _paper_entry_request(
    signal: Signal,
    bars: pl.DataFrame,
    quotes: pl.DataFrame,
    *,
    native_box: tuple[float, float, float, float] | None,
    market_open_utc: datetime,
    now_utc: datetime,
) -> PaperOrderRequest | None:
    """Build a small, stop-protected Paper re-entry only after a fresh scout signal."""

    if signal.symbol != "SNDK" or signal.event != "buy_ready" or native_box is None:
        return None
    frame = bars.filter(pl.col("symbol") == signal.symbol)
    vwap = _session_vwap(frame)
    quote = _quote(quotes, signal.symbol, now_utc)
    if vwap is None or quote is None:
        return None
    scout = _scout_trigger(frame, high=native_box[0], vwap=vwap, market_open_utc=market_open_utc, now_utc=now_utc)
    if scout is None or quote[1] <= scout[0]:
        return None
    risk_per_share = quote[1] - scout[0]
    shares = min(math.floor(PAPER_REENTRY_NOTIONAL / quote[1]), math.floor(PAPER_REENTRY_RISK / risk_per_share))
    if shares <= 0:
        return None
    client_order_id = f"h30-paper-scout-{signal.symbol.lower()}-{signal.dedupe_key.split(':')[-1]}"
    return PaperOrderRequest(
        client_order_id=client_order_id,
        symbol=signal.symbol,
        qty=shares,
        order_type="market",
        stop_loss_price=f"{scout[0]:.4f}",
    )


def _quote(frame: pl.DataFrame, symbol: str, now_utc: datetime) -> tuple[float, float] | None:
    rows = frame.filter(pl.col("symbol") == symbol).sort("ts_utc")
    if rows.is_empty():
        return None
    row = rows.row(-1, named=True)
    seen = row["ts_utc"]
    if not isinstance(seen, datetime) or (now_utc - seen).total_seconds() > 30:
        return None
    bid, ask = float(row["bid_price"]), float(row["ask_price"])
    return (bid, ask) if bid > 0 and ask > bid else None


def _sustained_decline(
    bars: pl.DataFrame, *, symbol: str, market_open_utc: datetime, now_utc: datetime
) -> bool | None:
    """Three completed 5m lower-high/lower-low bars, all below session VWAP."""

    frame = bars.filter(pl.col("symbol") == symbol)
    vwap = _session_vwap(frame)
    if vwap is None:
        return None
    completed: list[tuple[int, float, float, float]] = []
    elapsed = int((now_utc - market_open_utc).total_seconds() // 60)
    for index in range(max(0, elapsed // 5 - 6), elapsed // 5):
        start = market_open_utc + timedelta(minutes=index * 5)
        rows = frame.filter((pl.col("ts_utc") >= start) & (pl.col("ts_utc") < start + timedelta(minutes=5))).sort("ts_utc")
        if rows.height == 5 and start + timedelta(minutes=5) <= now_utc:
            completed.append((index, float(rows.get_column("high").max()), float(rows.get_column("low").min()), float(rows.row(-1, named=True)["close"])))
    if len(completed) < 3:
        return None
    first, second, third = completed[-3:]
    return (
        first[0] + 1 == second[0] and second[0] + 1 == third[0]
        and first[1] > second[1] > third[1]
        and first[2] > second[2] > third[2]
        and third[3] < vwap
    )


def _context_risk_off(
    bars: pl.DataFrame, *, plan: Plan, market_open_utc: datetime, now_utc: datetime
) -> bool | None:
    market = [_sustained_decline(bars, symbol=symbol, market_open_utc=market_open_utc, now_utc=now_utc) for symbol in MARKET_PROXIES]
    sector = _sustained_decline(bars, symbol=plan.sector_proxy, market_open_utc=market_open_utc, now_utc=now_utc)
    if sector is None or any(value is None for value in market):
        return None
    if plan.symbol == "SNDK":
        peers = [_sustained_decline(bars, symbol=symbol, market_open_utc=market_open_utc, now_utc=now_utc) for symbol in STORAGE_PEERS]
        if all(value is None for value in peers):
            return None
        sector = sector and any(value is True for value in peers)
    return all(market) and sector


def evaluate(plan: Plan, bars: pl.DataFrame, quotes: pl.DataFrame, *, market_open_utc: datetime, now_utc: datetime, already: set[str], native_box: tuple[float, float, float, float] | None = None) -> Signal | None:
    local = now_utc.astimezone(EASTERN)
    if local.time().hour < 10 or plan.symbol in already:
        return None
    context_risk_off = _context_risk_off(bars, plan=plan, market_open_utc=market_open_utc, now_utc=now_utc)
    if context_risk_off is None or context_risk_off:
        return None
    symbol_bars = bars.filter(pl.col("symbol") == plan.symbol)
    box = native_box or h30_box(symbol_bars, market_open_utc=market_open_utc, now_utc=now_utc)
    quote = _quote(quotes, plan.symbol, now_utc)
    vwap = _session_vwap(symbol_bars)
    if box is None or quote is None or vwap is None:
        return None
    high, low, _, median_volume = box
    scout = _scout_trigger(symbol_bars, high=high, vwap=vwap, market_open_utc=market_open_utc, now_utc=now_utc)
    if (high - low) / high > MAX_BOX_WIDTH or scout is None:
        return None
    if plan.requires_retest:
        prior = _last_complete_five(symbol_bars, market_open_utc=market_open_utc, now_utc=now_utc - timedelta(minutes=5))
        if prior is None or prior[0] <= high:
            return None
    if plan.requires_retest:
        elapsed = int((now_utc - market_open_utc).total_seconds() // 60)
        start = market_open_utc + timedelta(minutes=(elapsed // 5 - 1) * 5)
        current = symbol_bars.filter((pl.col("ts_utc") >= start) & (pl.col("ts_utc") < start + timedelta(minutes=5)))
        if current.height != 5 or float(current.get_column("low").min()) < high:
            return None
    stop = scout[0]
    risk_per_share = quote[1] - stop
    shares = min(math.floor(plan.scout_notional / quote[1]), math.floor(15_000 / risk_per_share)) if risk_per_share > 0 else 0
    if shares <= 0:
        return None
    return Signal("buy_ready", plan.symbol, "h30_breakout", "", f"h30-buy:{plan.symbol}:{high:.4f}")


def _native_h30_boxes(start_utc: datetime, now_utc: datetime) -> dict[str, tuple[float, float, float, float]]:
    key, secret = _direct_credentials()
    client = DirectAlpacaMarketDataClient(key_id=key, secret_key=secret)
    try:
        rows = client._rows("bars", symbols=tuple(plan.symbol for plan in CANDIDATE_PLANS), start_utc=start_utc, end_utc=now_utc, extra={"timeframe": "5Min", "adjustment": "split"})
    finally:
        client.close()
    grouped: dict[str, list[dict[str, object]]] = {}
    for symbol, row in rows:
        grouped.setdefault(symbol, []).append(row)
    boxes: dict[str, tuple[float, float, float, float]] = {}
    for symbol, values in grouped.items():
        if len(values) < 6:
            continue
        first_six = values[:6]
        boxes[symbol] = (
            max(float(row["h"]) for row in first_six),
            min(float(row["l"]) for row in first_six),
            median(float(row["c"]) for row in first_six),
            median(float(row["v"]) for row in first_six),
        )
    return boxes


def _fetch(now_utc: datetime) -> tuple[pl.DataFrame, pl.DataFrame, dict[str, tuple[float, float, float, float]]]:
    policy = stock_data_policy_from_env()
    symbols = tuple(dict.fromkeys((*[plan.symbol for plan in CANDIDATE_PLANS], *FALLBACK_WATCHLIST, *MARKET_PROXIES, *[plan.sector_proxy for plan in CANDIDATE_PLANS], *STORAGE_PEERS)))
    open_utc = datetime.combine(now_utc.astimezone(EASTERN).date(), datetime.min.time().replace(hour=9, minute=30), tzinfo=EASTERN).astimezone(UTC)
    bars, _ = fetch_sparse_bars_for_monitoring(symbols, open_utc, now_utc, feed=policy.feed)
    quotes = fetch_quotes(symbols, now_utc - timedelta(minutes=3), now_utc, feed=policy.feed)
    return bars, quotes, _native_h30_boxes(open_utc, now_utc)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, default=ROOT / "runs/h30-monitor-2026-08-17.json")
    parser.add_argument("--lock", type=Path, default=ROOT / "runs/h30-monitor-2026-08-17.lock")
    parser.add_argument("--verification", type=Path, default=ROOT / "runs/h30-monitor-2026-08-17-verified.json")
    parser.add_argument("--paper", action="store_true", help="Submit SNDK scout signals to the isolated Paper account only.")
    args = parser.parse_args()
    load_project_env(ROOT)
    if not _verification_covers_candidates(args.verification):
        try:
            _write_daily_verification(args.verification, now_utc=datetime.now(UTC))
        except Exception:
            pass
    paper_broker = None
    if args.paper:
        key_id = os.getenv("ALPACA_PAPER_KEY_ID", "").strip()
        secret_key = os.getenv("ALPACA_PAPER_SECRET_KEY", "").strip()
        if not key_id or not secret_key:
            raise RuntimeError("Paper mode requires ALPACA_PAPER_KEY_ID and ALPACA_PAPER_SECRET_KEY")
        paper_broker = DirectAlpacaPaperBroker(
            key_id=SecretStr(key_id),
            secret_key=SecretStr(secret_key),
            writes_enabled=True,
        )
    try:
      with ProcessLock(args.lock):
        while True:
            now = datetime.now(UTC)
            local = now.astimezone(EASTERN)
            if local.time().hour >= 15 and local.time().minute >= 55:
                return 0
            started = time.monotonic()
            state = json.loads(args.state.read_text(encoding="utf-8")) if args.state.exists() else {"notified": {}}
            notified = state.setdefault("notified", {})
            try:
                bars, quotes, native_boxes = _fetch(now)
                approved = _verified_symbols(args.verification)
                if not approved:
                    state["blocker"] = "daily_trend_market_cap_verification_missing"
                else:
                    state.pop("blocker", None)
                    position_events = state.setdefault("position_events", {})
                    positions = state.get("positions", {})
                    active_positions = {
                        symbol: position for symbol, position in positions.items()
                        if symbol in ACTIVE_SYMBOLS and isinstance(position, dict) and position.get("active") is True
                    } if isinstance(positions, dict) else {}
                    total_notional = sum(float(item.get("shares", 0)) * float(item.get("entry", 0)) for item in active_positions.values())
                    risk_values = [
                        float(item["shares"]) * max(float(item["entry"]) - float(item["stop"]), 0.0)
                        for item in active_positions.values() if item.get("stop") is not None
                    ]
                    risk_known = len(risk_values) == len(active_positions)
                    total_risk = sum(risk_values) if risk_known else None
                    allow_add = total_notional < PORTFOLIO_NOTIONAL_LIMIT and risk_known and total_risk is not None and total_risk < PORTFOLIO_RISK_LIMIT
                    state["risk_limits"] = {
                        "notional_limit": PORTFOLIO_NOTIONAL_LIMIT,
                        "risk_limit": PORTFOLIO_RISK_LIMIT,
                        "current_notional": total_notional,
                        "current_risk": total_risk,
                        "add_allowed": allow_add,
                    }
                    if isinstance(positions, dict):
                        for symbol, position in positions.items():
                            if not isinstance(position, dict) or position.get("active") is not True:
                                continue
                            plan = next((item for item in CANDIDATE_PLANS if item.symbol == symbol), None)
                            if plan is None:
                                continue
                            event = _position_signal(plan, position, bars, market_open_utc=datetime.combine(local.date(), datetime.min.time().replace(hour=9, minute=30), tzinfo=EASTERN).astimezone(UTC), now_utc=now, native_box=native_boxes.get(symbol), allow_add=allow_add)
                            if event is not None and event.dedupe_key not in position_events:
                                position_events[event.dedupe_key] = {"at": now.isoformat(), "message_id": _send_vps("4edcd570-603f-4c5f-a070-db88c48a5c9b", event)}
                    primary_signals: dict[str, Signal] = {}
                    for plan in PLANS:
                        if plan.symbol not in approved or plan.symbol not in ACTIVE_SYMBOLS:
                            continue
                        signal = evaluate(plan, bars, quotes, market_open_utc=datetime.combine(local.date(), datetime.min.time().replace(hour=9, minute=30), tzinfo=EASTERN).astimezone(UTC), now_utc=now, already=set(notified), native_box=native_boxes.get(plan.symbol))
                        if signal is not None:
                            primary_signals[plan.symbol] = signal
                    signals = list(primary_signals.values())
                    for plan in FALLBACK_PLANS:
                        if plan.symbol not in approved or plan.symbol not in ACTIVE_SYMBOLS or len(signals) >= 2:
                            continue
                        if plan.replacement_for == "SNDK" and "SNDK" not in primary_signals:
                            continue
                        if plan.replacement_for in primary_signals:
                            continue
                        signal = evaluate(plan, bars, quotes, market_open_utc=datetime.combine(local.date(), datetime.min.time().replace(hour=9, minute=30), tzinfo=EASTERN).astimezone(UTC), now_utc=now, already=set(notified), native_box=native_boxes.get(plan.symbol))
                        if signal is not None:
                            signals.append(signal)
                    for signal in signals[:2]:
                        if paper_broker is not None:
                            paper_request = _paper_entry_request(
                                signal,
                                bars,
                                quotes,
                                native_box=native_boxes.get(signal.symbol),
                                market_open_utc=datetime.combine(local.date(), datetime.min.time().replace(hour=9, minute=30), tzinfo=EASTERN).astimezone(UTC),
                                now_utc=now,
                            )
                            if paper_request is not None:
                                order = paper_broker.submit_order_idempotent(paper_request)
                                state.setdefault("paper_orders", {})[paper_request.client_order_id] = order.model_dump()
                        notified[signal.symbol] = {"key": signal.dedupe_key, "at": now.isoformat(), "message_id": _send_vps("4edcd570-603f-4c5f-a070-db88c48a5c9b", signal)}
            except Exception as exc:
                state["last_error"] = type(exc).__name__
            state["last_poll_utc"] = now.isoformat()
            args.state.parent.mkdir(parents=True, exist_ok=True)
            args.state.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            time.sleep(max(0.0, 1 - (time.monotonic() - started)))
    finally:
        if paper_broker is not None:
            paper_broker.close()


if __name__ == "__main__":
    raise SystemExit(main())
