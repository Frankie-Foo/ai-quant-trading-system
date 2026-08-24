"""Execute modern H15 momentum signals on Alpaca Paper only."""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import polars as pl
from pydantic import SecretStr

from data_plane.calendar import build_xnys_schedule
from data_plane.providers.alpaca import fetch_bars, fetch_quotes
from execution.alpaca_paper import (
    BrokerOrder,
    DirectAlpacaPaperBroker,
    FreshNbboQuote,
    PaperCloseRequest,
    build_protected_entry,
)
from operations.autonomous_selection_handoff import load_open_confirmation
from operations.feishu_base import FeishuBaseEventClient, InvestmentTable
from operations.livermore_push import LivermorePushClient, configured_identity
from operations.local_env import load_project_env, project_data_root
from operations.paper_runtime_policy import PaperRuntimePolicy
from research.h30_challenger import _five_minute_bars
from research.modern_momentum import (
    ModernMomentumConfig,
    evaluate_modern_momentum,
    pullback_reentry,
    reentry_exit_reason,
)
from scripts.monitor_modern_momentum_forward import _latest_pool
from scripts.run_modern_momentum_backtest import _entry_spread

ROOT = Path(__file__).resolve().parents[1]
MAX_DAILY_ENTRIES = 3
TERMINAL = frozenset({"filled", "canceled", "expired", "rejected"})
ATTEMPT_WEIGHTS = {1: 0.6, 2: 0.4}
STRATEGY_VERSION = "modern-h15.v1"


def _attempt(position: dict[str, object]) -> int:
    value = position.get("attempt")
    if not isinstance(value, int) or value not in ATTEMPT_WEIGHTS:
        raise ValueError("Paper position attempt is invalid")
    return value


def order_id(trade_date: str, symbol: str, action: str, *, attempt: int) -> str:
    if attempt not in ATTEMPT_WEIGHTS:
        raise ValueError("Paper attempt must be 1 or 2")
    return f"mm-{trade_date.replace('-', '')}-{symbol}-{action}-{attempt}"


def risk_fraction(*, hard_catalyst: bool) -> float:
    return 0.005 if hard_catalyst else 0.0025


def attempt_risk_fraction(base_fraction: float, *, attempt: int) -> float:
    if base_fraction <= 0 or attempt not in ATTEMPT_WEIGHTS:
        raise ValueError("valid base fraction and Paper attempt are required")
    return base_fraction * ATTEMPT_WEIGHTS[attempt]


def position_size(
    *,
    entry_price: float,
    all_in_stop_pct: float,
    equity: float,
    buying_power: float,
    risk_fraction: float,
    remaining_slots: int,
) -> int:
    if (
        entry_price <= 0
        or not 0 < all_in_stop_pct <= 0.02
        or equity <= 0
        or buying_power <= 0
        or not 0 < risk_fraction < 1
        or remaining_slots <= 0
    ):
        raise ValueError("valid account, entry, stop, and slot inputs are required")
    quantity = min(
        int((equity * risk_fraction) / (entry_price * all_in_stop_pct)),
        int((buying_power / remaining_slots) / entry_price),
    )
    if quantity < 1:
        raise ValueError("available Paper buying power cannot fund one share")
    return quantity


def _save(path: Path, state: dict[str, object]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    temporary.replace(path)


def _push_client() -> LivermorePushClient:
    app_id, channel_id = configured_identity(os.environ)
    return LivermorePushClient(
        app_id=app_id,
        app_secret=SecretStr(os.getenv("VPS_LIVERMORE_APP_SECRET", "")),
        channel_id=channel_id,
    )


def _broker(*, writes_enabled: bool) -> DirectAlpacaPaperBroker:
    return DirectAlpacaPaperBroker(
        key_id=SecretStr(os.getenv("ALPACA_PAPER_KEY_ID", "")),
        secret_key=SecretStr(os.getenv("ALPACA_PAPER_SECRET_KEY", "")),
        writes_enabled=writes_enabled,
    )


def _filled(order: BrokerOrder) -> bool:
    return order.status.lower() == "filled" and Decimal(order.filled_qty) > 0


def _latest_sip_nbbo(symbol: str, observed_at_utc: datetime) -> FreshNbboQuote:
    frame = fetch_quotes(
        (symbol,),
        observed_at_utc - timedelta(seconds=10),
        observed_at_utc + timedelta(microseconds=1),
        feed="sip",
    ).sort("ts_utc")
    if frame.is_empty():
        raise RuntimeError("immediate Alpaca SIP NBBO is unavailable")
    row = frame.tail(1).row(0, named=True)
    bid = row.get("bid_price")
    ask = row.get("ask_price")
    asof = row.get("ts_utc")
    feed = row.get("feed")
    if not isinstance(bid, (int, float)) or not isinstance(ask, (int, float)):
        raise RuntimeError("immediate Alpaca SIP NBBO prices are unavailable")
    if not isinstance(asof, datetime) or not isinstance(feed, str):
        raise RuntimeError("immediate Alpaca SIP NBBO identity is unavailable")
    return FreshNbboQuote(
        symbol=symbol,
        bid=Decimal(str(bid)),
        ask=Decimal(str(ask)),
        asof_utc=asof,
        feed=feed,
    )


def _bracket_child(order: BrokerOrder, order_type: str) -> BrokerOrder:
    matches = tuple(
        leg
        for leg in order.legs
        if (leg.order_type or "").lower() == order_type and (leg.side or "").lower() == "sell"
    )
    if len(matches) != 1:
        raise RuntimeError(f"protected Paper entry is missing its {order_type} child")
    return matches[0]


def _record_fill(
    base: FeishuBaseEventClient | None,
    *,
    client_order_id: str,
    symbol: str,
    direction: str,
    order: BrokerOrder,
    reason: str,
) -> None:
    if base is None or order.filled_avg_price is None:
        return
    quantity = int(Decimal(order.filled_qty))
    price = Decimal(order.filled_avg_price)
    base.record_event(
        InvestmentTable.TRADE,
        f"paper:{client_order_id}:filled",
        {
            "运行ID": f"paper:{client_order_id}:filled",
            "成交时间": datetime.now(UTC),
            "股票代码": symbol,
            "股票名称": symbol,
            "方向": direction,
            "订单状态": "已成交",
            "数量": quantity,
            "成交价格": str(price),
            "成交金额": str(price * quantity),
            "模拟账户": "paper",
            "持仓状态": "持仓中" if direction == "买入" else "已平仓",
            "触发来源": "modern_h15_momentum_v1",
            "下一动作": "继续盯盘" if direction == "买入" else "进入收盘复盘",
            "数据源状态": "alpaca.paper.direct|production=false",
            "执行摘要": f"{reason}；订单={order.id}",
        },
    )


def _record_fill_best_effort(
    base: FeishuBaseEventClient | None,
    *,
    client_order_id: str,
    symbol: str,
    direction: str,
    order: BrokerOrder,
    reason: str,
) -> str | None:
    try:
        _record_fill(
            base,
            client_order_id=client_order_id,
            symbol=symbol,
            direction=direction,
            order=order,
            reason=reason,
        )
    except Exception as exc:
        return type(exc).__name__
    return None


def _push_fill(
    push: LivermorePushClient,
    *,
    symbol: str,
    direction: str,
    order: BrokerOrder,
    reason: str,
) -> str:
    quantity = int(Decimal(order.filled_qty))
    price = order.filled_avg_price or "N/A"
    body = (
        f"【现代H15动量｜Paper{direction}】{symbol}\n"
        f"成交价：${price}；数量：{quantity}；原因：{reason}。\n"
        "仅Alpaca Paper模拟盘，禁止真实交易。"
    )
    if "\ufffd" in body or "??" in body:
        raise ValueError("Paper push contains invalid UTF-8 text")
    return push.push(body)


def main() -> None:
    load_project_env(ROOT)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True, type=date.fromisoformat)
    parser.add_argument("--arm-paper", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--confirmation-path", type=Path)
    args = parser.parse_args()
    data_root = project_data_root(ROOT)
    pool = _latest_pool(data_root, args.trade_date)
    session = build_xnys_schedule(args.trade_date, args.trade_date).row(0, named=True)
    opened = session["market_open_utc"]
    start_at = opened + timedelta(minutes=26)
    stop_at = opened + timedelta(minutes=377)
    symbols = tuple(pool.get_column("symbol").to_list())
    confirmation_path = args.confirmation_path or (
        ROOT
        / "runs"
        / "autonomous"
        / args.trade_date.isoformat()
        / "open_confirmation.json"
    )
    confirmation = load_open_confirmation(confirmation_path)
    broker_write_enabled = os.getenv("BROKER_WRITE_ENABLED", "").strip().lower() == "true"
    kill_switch = os.getenv("TRADING_KILL_SWITCH", "true").strip().lower() != "false"
    broker = _broker(
        writes_enabled=bool(args.arm_paper and broker_write_enabled and not kill_switch)
    )
    if not args.check:
        PaperRuntimePolicy().validate_arming(
            trade_date=args.trade_date,
            broker_write_enabled=broker_write_enabled and bool(args.arm_paper),
            trading_kill_switch=kill_switch,
            broker_base_url=broker.base_url,
            authorization=confirmation.authorization,
            expected_candidate_pool=symbols,
            expected_strategy_version=STRATEGY_VERSION,
        )
    account = broker.get_account()
    if account.status != "ACTIVE" or account.account_blocked or account.trading_blocked:
        raise RuntimeError("Alpaca Paper account is not tradable")
    if args.check:
        print(
            json.dumps(
                {
                    "status": "ready",
                    "broker": broker.broker_identity,
                    "base_url": broker.base_url,
                    "symbols": symbols,
                    "start_at_utc": start_at,
                    "stop_at_utc": stop_at,
                    "paper_writes_enabled": broker.writes_enabled,
                    "live_trading_enabled": False,
                    "sizing": "account_equity_and_buying_power",
                },
                default=str,
            )
        )
        broker.close()
        return
    if not args.arm_paper:
        raise RuntimeError("Paper writes require --arm-paper")

    run_dir = ROOT / "runs" / "modern-momentum" / args.trade_date.isoformat()
    run_dir.mkdir(parents=True, exist_ok=True)
    state_path = run_dir / "paper-state.json"
    state: dict[str, object] = {
        "trade_date": args.trade_date.isoformat(),
        "symbols": symbols,
        "positions": {},
        "attempts": {},
        "reentry_after_utc": {},
        "completed_symbols": [],
        "events": [],
        "message_ids": [],
        "broker": broker.broker_identity,
        "paper_writes_enabled": True,
        "live_trading_enabled": False,
        "sizing_policy": {
            "hard_catalyst_equity_fraction": risk_fraction(hard_catalyst=True),
            "other_catalyst_equity_fraction": risk_fraction(hard_catalyst=False),
            "buying_power_allocation": "equal_across_remaining_entry_slots",
            "fixed_dollar_cap": None,
            "attempt_risk_weights": ATTEMPT_WEIGHTS,
        },
        "status": "waiting",
    }
    config = ModernMomentumConfig()
    prior_closes = {
        str(row["symbol"]): float(row["price"])
        for row in pool.iter_rows(named=True)
        if isinstance(row["price"], (int, float))
    }
    market_caps = {
        str(row["symbol"]): float(row["forward_market_cap"]) for row in pool.iter_rows(named=True)
    }
    rvols = {str(row["symbol"]): float(row["rvol"]) for row in pool.iter_rows(named=True)}
    hard_catalysts = {
        str(row["symbol"]): bool(row["hard_catalyst"]) for row in pool.iter_rows(named=True)
    }
    positions: dict[str, dict[str, object]] = {}
    completed: set[str] = set()
    entries_started: set[str] = set()
    attempts: dict[str, int] = {}
    reentry_after: dict[str, datetime] = {}
    events: list[dict[str, object]] = []
    message_ids: list[str] = []
    last_minute: datetime | None = None
    push = _push_client()
    base = FeishuBaseEventClient.from_environment()
    try:
        while datetime.now(UTC) < stop_at:
            try:
                for symbol, position in list(positions.items()):
                    phase = str(position["phase"])
                    if phase == "entry_pending":
                        entry = broker.get_order_by_client_id(str(position["entry_client_id"]))
                        if entry is None or not _filled(entry):
                            continue
                        attempt = _attempt(position)
                        quantity = int(Decimal(entry.filled_qty))
                        filled_price = float(entry.filled_avg_price or 0)
                        stop = _bracket_child(entry, "stop")
                        target = _bracket_child(entry, "limit")
                        raw_stop_level = position["stop_level"]
                        if not isinstance(raw_stop_level, (int, float)):
                            raise ValueError("Paper stop level is invalid")
                        stop_level = float(raw_stop_level)
                        position.update(
                            {
                                "phase": "active",
                                "shares": quantity,
                                "entry_px": filled_price,
                                "stop_level": stop_level,
                                "target_level": filled_price + 3 * (filled_price - stop_level),
                                "stop_client_id": stop.client_order_id,
                                "target_client_id": target.client_order_id,
                            }
                        )
                        reason = (
                            "H15突破、4%强度、MACD增强及量能确认"
                            if attempt == 1
                            else "首次止损后重新站回H15与VWAP，回踩承接并放量突破"
                        )
                        message_ids.append(
                            _push_fill(
                                push,
                                symbol=symbol,
                                direction="买入",
                                order=entry,
                                reason=reason,
                            )
                        )
                        feishu_error = _record_fill_best_effort(
                            base,
                            client_order_id=str(position["entry_client_id"]),
                            symbol=symbol,
                            direction="买入",
                            order=entry,
                            reason=reason,
                        )
                        if feishu_error is not None:
                            events.append(
                                {
                                    "type": "feishu_write_failed",
                                    "symbol": symbol,
                                    "attempt": attempt,
                                    "direction": "买入",
                                    "error": feishu_error,
                                }
                            )
                        events.append(
                            {
                                "type": "paper_buy_filled",
                                "symbol": symbol,
                                "attempt": attempt,
                                "order": entry.id,
                            }
                        )
                    elif phase == "active":
                        exit_orders = (
                            (
                                "stop",
                                broker.get_order_by_client_id(
                                    str(position["stop_client_id"])
                                ),
                            ),
                            (
                                "target",
                                broker.get_order_by_client_id(
                                    str(position["target_client_id"])
                                ),
                            ),
                        )
                        filled_exit = next(
                            (
                                (kind, order)
                                for kind, order in exit_orders
                                if order is not None and _filled(order)
                            ),
                            None,
                        )
                        if filled_exit is None:
                            continue
                        exit_kind, current_exit = filled_exit
                        attempt = _attempt(position)
                        bracket_exit_reason = (
                            f"第{attempt}次入场保护止损"
                            if exit_kind == "stop"
                            else f"第{attempt}次入场3R止盈"
                        )
                        message_ids.append(
                            _push_fill(
                                push,
                                symbol=symbol,
                                direction="卖出",
                                order=current_exit,
                                reason=bracket_exit_reason,
                            )
                        )
                        feishu_error = _record_fill_best_effort(
                            base,
                            client_order_id=current_exit.client_order_id,
                            symbol=symbol,
                            direction="卖出",
                            order=current_exit,
                            reason=bracket_exit_reason,
                        )
                        if feishu_error is not None:
                            events.append(
                                {
                                    "type": "feishu_write_failed",
                                    "symbol": symbol,
                                    "attempt": attempt,
                                    "direction": "卖出",
                                    "error": feishu_error,
                                }
                            )
                        events.append(
                            {
                                "type": f"paper_{exit_kind}_filled",
                                "symbol": symbol,
                                "attempt": attempt,
                                "order": current_exit.id,
                            }
                        )
                        positions.pop(symbol)
                        if attempt == 1 and exit_kind == "stop":
                            reentry_after[symbol] = datetime.now(UTC)
                        else:
                            completed.add(symbol)
                    elif phase == "exit_pending":
                        close = broker.get_order_by_client_id(str(position["exit_client_id"]))
                        if close is None or not _filled(close):
                            continue
                        reason = str(position["exit_reason"])
                        message_ids.append(
                            _push_fill(
                                push,
                                symbol=symbol,
                                direction="卖出",
                                order=close,
                                reason=reason,
                            )
                        )
                        feishu_error = _record_fill_best_effort(
                            base,
                            client_order_id=str(position["exit_client_id"]),
                            symbol=symbol,
                            direction="卖出",
                            order=close,
                            reason=reason,
                        )
                        if feishu_error is not None:
                            events.append(
                                {
                                    "type": "feishu_write_failed",
                                    "symbol": symbol,
                                    "attempt": _attempt(position),
                                    "direction": "卖出",
                                    "error": feishu_error,
                                }
                            )
                        events.append(
                            {"type": "paper_sell_filled", "symbol": symbol, "order": close.id}
                        )
                        positions.pop(symbol)
                        completed.add(symbol)

                now = datetime.now(UTC)
                if now < start_at:
                    _save(state_path, state)
                    time.sleep(1)
                    continue
                complete_minute = now.replace(second=0, microsecond=0)
                if complete_minute == last_minute:
                    time.sleep(1)
                    continue
                last_minute = complete_minute
                bars = fetch_bars(symbols, opened, complete_minute)
                for symbol in symbols:
                    if symbol in completed or symbol not in prior_closes:
                        continue
                    symbol_bars = bars.filter(pl.col("symbol") == symbol)
                    if symbol_bars.is_empty():
                        continue
                    preliminary = evaluate_modern_momentum(
                        symbol_bars,
                        session_open_utc=opened,
                        prior_close=prior_closes[symbol],
                        market_cap=market_caps[symbol],
                        premarket_rvol=rvols[symbol],
                        config=config,
                    )
                    if preliminary is None:
                        continue
                    spread = _entry_spread(symbol, preliminary)
                    trade = evaluate_modern_momentum(
                        symbol_bars,
                        session_open_utc=opened,
                        prior_close=prior_closes[symbol],
                        market_cap=market_caps[symbol],
                        premarket_rvol=rvols[symbol],
                        config=config,
                        relative_spread=spread,
                    )
                    if trade is None:
                        continue
                    candidate_position = positions.get(symbol)
                    if candidate_position is None:
                        previous_attempt = attempts.get(symbol, 0)
                        attempt = previous_attempt + 1
                        if attempt > 2 or (
                            attempt == 1 and len(entries_started) >= MAX_DAILY_ENTRIES
                        ):
                            continue
                        entry_price = trade.entry_px
                        stop_level = trade.stop_level
                        signal_ts_utc = trade.signal_ts_utc
                        if attempt == 2:
                            stopped_at = reentry_after.get(symbol)
                            if stopped_at is None:
                                continue
                            reentry = pullback_reentry(
                                _five_minute_bars(
                                    symbol_bars,
                                    session_open_utc=opened,
                                ),
                                stopped_at_utc=stopped_at,
                                h15=trade.h15,
                                asof_utc=complete_minute,
                            )
                            if reentry is None:
                                continue
                            entry_price = reentry.entry_reference
                            stop_level = max(
                                reentry.structural_stop,
                                entry_price * 0.985,
                            )
                            signal_ts_utc = reentry.signal_ts_utc
                        quote_observed_at = datetime.now(UTC)
                        quote = _latest_sip_nbbo(symbol, quote_observed_at)
                        actual_all_in_stop_pct = (
                            float((quote.ask - Decimal(str(stop_level))) / quote.ask)
                            + config.stop_slippage_reserve_pct
                        )
                        current_account = broker.get_account()
                        remaining_slots = max(1, MAX_DAILY_ENTRIES - len(positions))
                        base_fraction = risk_fraction(
                            hard_catalyst=hard_catalysts[symbol]
                        )
                        allocation_fraction = attempt_risk_fraction(
                            base_fraction,
                            attempt=attempt,
                        )
                        quantity = position_size(
                            entry_price=float(quote.ask),
                            all_in_stop_pct=actual_all_in_stop_pct,
                            equity=float(current_account.equity),
                            buying_power=float(current_account.buying_power),
                            risk_fraction=allocation_fraction,
                            remaining_slots=remaining_slots,
                        )
                        entry_client_id = order_id(
                            args.trade_date.isoformat(),
                            symbol,
                            "entry",
                            attempt=attempt,
                        )
                        protected_entry = build_protected_entry(
                            client_order_id=entry_client_id,
                            symbol=symbol,
                            qty=quantity,
                            signal_reference=Decimal(str(entry_price)),
                            structural_stop=Decimal(str(stop_level)),
                            quote=quote,
                            observed_at_utc=datetime.now(UTC),
                            stop_slippage_reserve=Decimal(
                                str(config.stop_slippage_reserve_pct)
                            ),
                        )
                        entry = broker.submit_protected_entry_idempotent(
                            protected_entry
                        )
                        if entry.status.lower() in {"rejected", "canceled", "expired"}:
                            raise RuntimeError("Alpaca Paper entry was rejected")
                        positions[symbol] = {
                            "phase": "entry_pending",
                            "attempt": attempt,
                            "entry_client_id": entry_client_id,
                            "entry_order_id": entry.id,
                            "signal_ts_utc": signal_ts_utc,
                            "stop_level": float(protected_entry.stop_loss_price),
                            "target_level": float(protected_entry.take_profit_price),
                            "sizing_equity": current_account.equity,
                            "sizing_buying_power": current_account.buying_power,
                            "risk_fraction": allocation_fraction,
                            "all_in_stop_pct": actual_all_in_stop_pct,
                        }
                        attempts[symbol] = attempt
                        if attempt == 1:
                            entries_started.add(symbol)
                        events.append(
                            {
                                "type": "paper_buy_submitted",
                                "symbol": symbol,
                                "attempt": attempt,
                                "order": entry.id,
                            }
                        )
                        continue
                    if str(candidate_position["phase"]) != "active":
                        continue
                    exit_reason: str | None = trade.exit_reason
                    if _attempt(candidate_position) == 2:
                        target_level = candidate_position["target_level"]
                        entered_at = candidate_position["signal_ts_utc"]
                        if not isinstance(target_level, (int, float)) or not isinstance(
                            entered_at, datetime
                        ):
                            raise ValueError("second Paper entry state is invalid")
                        exit_reason = reentry_exit_reason(
                            _five_minute_bars(symbol_bars, session_open_utc=opened),
                            entered_at_utc=entered_at,
                            asof_utc=complete_minute,
                            target_level=float(target_level),
                            liquidation_utc=stop_at - timedelta(minutes=2),
                        )
                        if exit_reason is None:
                            continue
                    elif exit_reason in {"data_end", "stop"}:
                        continue
                    for child_key in ("stop_client_id", "target_client_id"):
                        child_order = broker.get_order_by_client_id(
                            str(candidate_position[child_key])
                        )
                        if child_order is not None and child_order.status.lower() not in TERMINAL:
                            broker.cancel_order(child_order.id)
                    paper_positions = {item.symbol: item for item in broker.list_positions()}
                    paper_position = paper_positions.get(symbol)
                    if paper_position is None:
                        continue
                    quantity = int(Decimal(paper_position.qty))
                    exit_client_id = order_id(
                        args.trade_date.isoformat(),
                        symbol,
                        "exit",
                        attempt=_attempt(candidate_position),
                    )
                    close = broker.submit_close_order_idempotent(
                        PaperCloseRequest(
                            client_order_id=exit_client_id,
                            symbol=symbol,
                            qty=quantity,
                        )
                    )
                    candidate_position.update(
                        {
                            "phase": "exit_pending",
                            "exit_client_id": exit_client_id,
                            "exit_order_id": close.id,
                            "exit_reason": exit_reason,
                        }
                    )
                    events.append(
                        {"type": "paper_sell_submitted", "symbol": symbol, "order": close.id}
                    )
                state.update(
                    {
                        "positions": positions,
                        "attempts": attempts,
                        "reentry_after_utc": reentry_after,
                        "completed_symbols": sorted(completed),
                        "events": events,
                        "message_ids": message_ids,
                        "status": "running",
                        "last_complete_minute_utc": complete_minute,
                    }
                )
                _save(state_path, state)
                time.sleep(1)
            except Exception as exc:
                state.update(
                    {
                        "status": "degraded",
                        "last_error_type": type(exc).__name__,
                        "positions": positions,
                        "events": events,
                    }
                )
                _save(state_path, state)
                time.sleep(5)
        state["status"] = "complete"
        _save(state_path, state)
    finally:
        push.close()
        broker.close()


if __name__ == "__main__":
    main()
