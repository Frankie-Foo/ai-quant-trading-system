"""Execute modern H15 momentum signals on Alpaca Paper only."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import time
import traceback
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from functools import partial
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
    ProtectedPaperEntryRequest,
    build_protected_entry,
)
from operations.autonomous_selection_handoff import load_open_confirmation
from operations.feishu_base import FeishuBaseEventClient, InvestmentTable
from operations.livermore_push import LivermorePushClient, configured_identity
from operations.local_env import load_project_env, project_data_root
from operations.paper_release import remaining_entry_notional
from operations.paper_release import validate_smoke_notional as validate_smoke_notional
from operations.paper_run_evidence import capture_startup
from operations.paper_runtime_policy import PaperRuntimePolicy
from operations.paper_state import OutboxClaim, PaperStateStore, read_prior_day_states
from operations.runtime_alerts import RuntimeAlertManager, bounded_retry
from research.h30_challenger import _five_minute_bars
from research.modern_momentum import (
    ModernMomentumConfig,
    actual_position_exit_reason,
    latest_modern_momentum_signal,
    modern_strategy_manifest,
    pullback_reentry,
)
from scripts.monitor_modern_momentum_forward import _latest_pool

ROOT = Path(__file__).resolve().parents[1]
MAX_DAILY_ENTRIES = 3
TERMINAL = frozenset({"filled", "canceled", "expired", "rejected"})
KNOWN_ORDER_STATUSES = TERMINAL | frozenset(
    {
        "new",
        "partially_filled",
        "done_for_day",
        "replaced",
        "pending_cancel",
        "pending_replace",
        "accepted",
        "pending_new",
        "accepted_for_bidding",
        "stopped",
        "suspended",
        "calculated",
        "held",
    }
)
PROTECTIVE_ORDER_STATUSES = frozenset({"new", "accepted", "accepted_for_bidding"})
ATTEMPT_WEIGHTS = {1: 0.6, 2: 0.4}
STRATEGY_VERSION = "modern-h15.v1"


class CandidateRejected(ValueError):
    """Expected, symbol-local entry refusal; never a global runtime fault."""


class PreSubmitRejected(CandidateRejected):
    """Final callback refused this entry before any broker POST."""


def approved_strategy_matches(plan: dict[str, object]) -> bool:
    """The approved plan must describe this implementation, not an old policy alias."""
    return plan.get("modern_strategy_manifest") == modern_strategy_manifest()


def session_control_times(
    market_open_utc: datetime,
    market_close_utc: datetime,
) -> tuple[datetime, datetime, datetime]:
    entry_cutoff = min(
        market_open_utc + timedelta(minutes=330),
        market_close_utc - timedelta(hours=1),
    )
    return (
        entry_cutoff,
        market_close_utc - timedelta(minutes=15),
        market_close_utc - timedelta(minutes=10),
    )


def _attempt(position: dict[str, object]) -> int:
    value = position.get("attempt")
    if not isinstance(value, int) or value not in ATTEMPT_WEIGHTS:
        raise ValueError("Paper position attempt is invalid")
    return value


def _position_risk(position: dict[str, object]) -> float:
    value = position.get("risk_fraction")
    if not isinstance(value, (int, float)) or value < 0:
        raise ValueError("persisted Paper position risk is invalid")
    return float(value)


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
    max_notional: float | None = None,
) -> int:
    if (
        not all(
            math.isfinite(value)
            for value in (
                entry_price,
                all_in_stop_pct,
                equity,
                buying_power,
                risk_fraction,
            )
        )
        or entry_price <= 0
        or not 0 < all_in_stop_pct <= 0.02
        or equity <= 0
        or buying_power <= 0
        or not 0 < risk_fraction < 1
        or remaining_slots <= 0
        or (max_notional is not None and (not math.isfinite(max_notional) or max_notional <= 0))
    ):
        raise ValueError("valid account, entry, stop, and slot inputs are required")
    quantity = min(
        int((equity * risk_fraction) / (entry_price * all_in_stop_pct)),
        int((buying_power / remaining_slots) / entry_price),
        int(equity / entry_price),
    )
    if max_notional is not None:
        quantity = min(quantity, int(max_notional / entry_price))
    if quantity < 1:
        raise CandidateRejected("cannot_fund_one_share_within_current_release_cap")
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


def _order_tree(orders: tuple[BrokerOrder, ...]) -> tuple[BrokerOrder, ...]:
    result: dict[str, BrokerOrder] = {}
    for order in orders:
        result[order.id] = order
        result.update({child.id: child for child in _order_tree(order.legs)})
    return tuple(result.values())


def _require_known_order_status(order: BrokerOrder) -> None:
    if order.status.lower() not in KNOWN_ORDER_STATUSES:
        raise RuntimeError("unknown broker order status requires reconciliation")


def _owned_exit_quantity(
    broker: DirectAlpacaPaperBroker,
    *,
    symbol: str,
    position: dict[str, object],
    entry: BrokerOrder | None,
    previous: BrokerOrder | None,
) -> int | None:
    holding = next((p for p in broker.list_positions() if p.symbol == symbol), None)
    if holding is None:
        return None
    quantity_decimal = Decimal(holding.qty)
    if (
        not quantity_decimal.is_finite()
        or holding.side.lower() != "long"
        or quantity_decimal <= 0
        or quantity_decimal != quantity_decimal.to_integral_value()
    ):
        raise RuntimeError("safe Paper exit requires a verified whole-share long")
    quantity = int(quantity_decimal)
    owned_quantity = int(Decimal(entry.filled_qty)) if entry is not None else position.get("shares")
    if not isinstance(owned_quantity, int) or quantity > owned_quantity:
        raise RuntimeError("broker quantity exceeds proven owned shares")
    if previous is not None and quantity > previous.qty - int(Decimal(previous.filled_qty)):
        raise RuntimeError("Paper exit fills and remaining position are inconsistent")
    return quantity


def request_position_exit(
    broker: DirectAlpacaPaperBroker,
    store: PaperStateStore,
    *,
    trade_date: date,
    symbol: str,
    position: dict[str, object],
    observed_at_utc: datetime,
    reason: str,
) -> BrokerOrder | None:
    """Reconcile before reducing a long; preserve live exits and retry terminal exits."""
    previous_id = str(position.get("exit_client_id", ""))
    previous = broker.get_order_by_client_id(previous_id) if previous_id else None
    visible_orders = _order_tree(broker.list_open_orders())
    for visible in visible_orders:
        _require_known_order_status(visible)
    if previous is None and previous_id:
        previous = next((o for o in visible_orders if o.client_order_id == previous_id), None)
    if previous is not None:
        _require_known_order_status(previous)
    if previous is not None and previous.status.lower() not in TERMINAL:
        return previous

    owned_clients = {
        str(value)
        for key, value in position.items()
        if key.endswith("_client_id") and isinstance(value, str)
    }
    entry_id = str(position.get("entry_client_id", ""))
    entry = broker.get_order_by_client_id(entry_id) if entry_id else None
    if entry is not None:
        _require_known_order_status(entry)
        owned_clients.update(order.client_order_id for order in _order_tree((entry,)))
    orders = tuple(
        order
        for order in visible_orders
        if order.symbol == symbol and order.status.lower() not in TERMINAL
    )
    if any(order.client_order_id not in owned_clients for order in orders):
        raise RuntimeError("unowned order prevents a safe Paper exit")
    # Check ownership and unresolved intent *before* removing any protective order.
    before_quantity = _owned_exit_quantity(
        broker, symbol=symbol, position=position, entry=entry, previous=previous
    )
    if previous_id and previous is None:
        raw_request = position.get("exit_request")
        if not isinstance(raw_request, dict):
            raise RuntimeError("persisted Paper exit request is missing")
        saved_request = PaperCloseRequest.model_validate(raw_request)
        if before_quantity is not None and saved_request.qty != before_quantity:
            raise RuntimeError("unresolved Paper exit intent no longer matches holdings")
    for order in orders:
        if order.status.lower() != "pending_cancel":
            broker.cancel_order(order.id)
    remaining = tuple(
        order
        for order in _order_tree(broker.list_open_orders())
        if order.symbol == symbol and order.status.lower() not in TERMINAL
    )
    reconciled_orders: dict[str, BrokerOrder] = {}
    for client_id in owned_clients:
        refreshed = broker.get_order_by_client_id(client_id)
        if refreshed is not None:
            for item in _order_tree((refreshed,)):
                _require_known_order_status(item)
                _remember_fill(position, item, observed_at_utc)
                prior_snapshot = reconciled_orders.get(item.id)
                if prior_snapshot is None or Decimal(item.filled_qty) >= Decimal(
                    prior_snapshot.filled_qty
                ):
                    reconciled_orders[item.id] = item
    position.update({"phase": "exit_pending", "exit_reason": reason})
    if remaining:
        store.save_symbol_state(
            trade_date=trade_date, symbol=symbol, state=position, observed_at_utc=observed_at_utc
        )
        return None

    refreshed_entry = broker.get_order_by_client_id(entry_id) if entry_id else None
    quantity = _owned_exit_quantity(
        broker, symbol=symbol, position=position, entry=refreshed_entry or entry, previous=previous
    )
    if quantity is None:
        if refreshed_entry is not None:
            sold = sum(
                (Decimal(o.filled_qty) for o in reconciled_orders.values() if o.side == "sell"),
                Decimal(0),
            )
            if Decimal(refreshed_entry.filled_qty) != sold:
                raise RuntimeError("confirmed filled inventory disagrees with flat position lookup")
        position["phase"] = "complete"
        store.save_symbol_state(
            trade_date=trade_date, symbol=symbol, state=position, observed_at_utc=observed_at_utc
        )
        return previous
    retry = int(str(position.get("exit_retry", 0)))
    if previous is not None:
        retry += 1
    if retry >= 3:
        raise RuntimeError("Paper exit retries exhausted; reconciliation required")
    if previous_id and previous is None:
        raw = position.get("exit_request")
        if not isinstance(raw, dict):
            raise RuntimeError("persisted Paper exit request is missing")
        request = PaperCloseRequest.model_validate(raw)
        if request.qty != quantity or request.symbol != symbol:
            raise RuntimeError("unresolved Paper exit intent no longer matches holdings")
    else:
        client_id = order_id(trade_date.isoformat(), symbol, "exit", attempt=_attempt(position))
        if retry:
            client_id = f"{client_id}-r{retry}"
        request = PaperCloseRequest(client_order_id=client_id, symbol=symbol, qty=quantity)
    store.record_order_intent(
        trade_date=trade_date,
        client_order_id=request.client_order_id,
        symbol=symbol,
        attempt=_attempt(position),
        role="exit",
        quantity=quantity,
        payload=request.broker_payload(),
        observed_at_utc=observed_at_utc,
    )
    position.update(
        {
            "exit_client_id": request.client_order_id,
            "exit_retry": retry,
            "exit_request": request.model_dump(mode="json"),
        }
    )
    store.save_symbol_state(
        trade_date=trade_date, symbol=symbol, state=position, observed_at_utc=observed_at_utc
    )
    submitted = broker.submit_close_order_idempotent(request)
    store.attach_broker_order(
        client_order_id=request.client_order_id,
        broker_order_id=submitted.id,
        status=submitted.status,
        observed_at_utc=observed_at_utc,
    )
    position["exit_order_id"] = submitted.id
    store.save_symbol_state(
        trade_date=trade_date, symbol=symbol, state=position, observed_at_utc=observed_at_utc
    )
    return submitted


def _remember_fill(position: dict[str, object], order: BrokerOrder, now: datetime) -> None:
    quantity = Decimal(order.filled_qty)
    if not quantity.is_finite() or not 0 <= quantity <= order.qty or quantity != int(quantity):
        raise RuntimeError("broker cumulative fill quantity is invalid")
    if quantity == 0:
        return
    if order.filled_avg_price is None or Decimal(order.filled_avg_price) <= 0:
        raise RuntimeError("broker cumulative fill price is unavailable")
    observations = position.setdefault("fill_observations", {})
    if not isinstance(observations, dict):
        raise RuntimeError("persisted fill observations are invalid")
    key = f"fill:{order.id}:{quantity}"
    initial_keys = position.get("recovery_initial_fill_keys", [])
    if not isinstance(initial_keys, list):
        raise RuntimeError("recovery baseline fill keys are invalid")
    if key in initial_keys:
        return
    if key not in observations:
        observations[key] = {
            "order": order.model_dump(mode="json", by_alias=True),
            "observed_at_utc": now.isoformat(),
            "quantity_semantics": "broker_order_cumulative",
        }


def reconcile_symbol_position(
    broker: DirectAlpacaPaperBroker,
    store: PaperStateStore,
    *,
    trade_date: date,
    symbol: str,
    position: dict[str, object],
    observed_at_utc: datetime,
    cancel_entries: bool,
) -> None:
    """Reconcile actual broker state first; never replay a saved buy request."""
    now = observed_at_utc
    stop: BrokerOrder | None
    target: BrokerOrder | None

    def save() -> None:
        store.save_symbol_state(
            trade_date=trade_date, symbol=symbol, state=position, observed_at_utc=now
        )

    def remember(order: BrokerOrder | None) -> None:
        if order is not None:
            for item in _order_tree((order,)):
                _require_known_order_status(item)
                _remember_fill(position, item, now)
            if store.get_order(order.client_order_id) is not None:
                store.attach_broker_order(
                    client_order_id=order.client_order_id,
                    broker_order_id=order.id,
                    status=order.status,
                    observed_at_utc=now,
                )
            save()

    def close(reason: str) -> None:
        for key, value in position.copy().items():
            if key.endswith("_client_id") and isinstance(value, str):
                remember(broker.get_order_by_client_id(value))
        old_id = position.get("exit_client_id")
        if isinstance(old_id, str):
            remember(broker.get_order_by_client_id(old_id))
        exit_order = request_position_exit(
            broker,
            store,
            trade_date=trade_date,
            symbol=symbol,
            position=position,
            observed_at_utc=now,
            reason=reason,
        )
        remember(exit_order)

    if position.get("phase") == "entry_pending":
        entry = broker.get_order_by_client_id(str(position["entry_client_id"]))
        if entry is None:
            if any(p.symbol == symbol for p in broker.list_positions()) or any(
                o.symbol == symbol for o in broker.list_open_orders()
            ):
                raise RuntimeError("entry lookup and broker exposure disagree")
            position.update(phase="complete", reason="unconfirmed_entry_not_replayed")
            save()
            return
        remember(entry)
        partial_fill = 0 < Decimal(entry.filled_qty) < entry.qty
        if entry.status.lower() not in TERMINAL and (cancel_entries or partial_fill):
            if entry.status.lower() != "pending_cancel":
                broker.cancel_order(entry.id)
            refreshed = broker.get_order_by_client_id(entry.client_order_id)
            if refreshed is None:
                raise RuntimeError("entry disappeared during cancellation")
            entry = refreshed
            remember(entry)
        if partial_fill and not _filled(entry):
            # Bracket protection may be held until the entire parent fills.
            close("部分买入成交，撤余单并退出未完整保护仓位")
            return
        if not _filled(entry):
            if entry.status.lower() in TERMINAL:
                if Decimal(entry.filled_qty) > 0:
                    close("入场余单终止，退出部分成交仓位")
                else:
                    position.update(phase="complete", reason="entry_terminal_without_fill")
                    save()
            return
        try:
            stop = _bracket_child(entry, "stop")
            target = _bracket_child(entry, "limit")
        except RuntimeError:
            close("买入已成交但保护单缺失，退出未保护仓位")
            return
        position.update(
            phase="active",
            shares=int(Decimal(entry.filled_qty)),
            entry_px=float(entry.filled_avg_price or 0),
            entered_at_utc=entry.filled_at or now,
            entry_time_source="broker_filled_at" if entry.filled_at else "first_observed_fill",
            stop_client_id=stop.client_order_id,
            target_client_id=target.client_order_id,
        )
        save()
    if position.get("phase") == "active":
        stop = broker.get_order_by_client_id(str(position["stop_client_id"]))
        target = broker.get_order_by_client_id(str(position["target_client_id"]))
        remember(stop)
        remember(target)
        holdings = [p for p in broker.list_positions() if p.symbol == symbol]
        if holdings:
            holding = holdings[0]
            quantity = Decimal(holding.qty)
            if holding.side != "long" or quantity <= 0 or quantity != int(quantity):
                raise RuntimeError("broker position is not a whole-share long")
            owned_quantity = position.get("shares")
            if not isinstance(owned_quantity, int) or quantity > owned_quantity:
                raise RuntimeError("broker quantity exceeds proven owned shares")
            position["shares"] = int(quantity)
            if (
                stop is None
                or target is None
                or any(
                    o.status.lower() not in PROTECTIVE_ORDER_STATUSES or Decimal(o.filled_qty) > 0
                    for o in (stop, target)
                    if o is not None
                )
            ):
                close("保护单部分成交或保护失效，核对残量后退出")
            else:
                save()
            return
        # A missing position row alone is not proof of flat inventory. Do this
        # check before touching protection: broker endpoints can temporarily disagree.
        entry_client_id = position.get("entry_client_id")
        entry = (
            broker.get_order_by_client_id(entry_client_id)
            if isinstance(entry_client_id, str) else None
        )
        remember(entry)
        exited_quantity = sum(
            (Decimal(order.filled_qty) for order in (stop, target) if order is not None),
            Decimal(0),
        )
        if (
            entry is None
            or entry.symbol != symbol
            or entry.side != "buy"
            or Decimal(entry.filled_qty) <= 0
            or exited_quantity != Decimal(entry.filled_qty)
        ):
            raise RuntimeError("flat position response disagrees with proven entry/exit fills")
        # A flat position is not complete until dangling sell orders are gone.
        for order in (stop, target):
            if order is not None and order.status.lower() not in TERMINAL:
                if order.status.lower() != "pending_cancel":
                    broker.cancel_order(order.id)
        if any(o.symbol == symbol for o in broker.list_open_orders()):
            save()
            return
        if not any(o is not None and Decimal(o.filled_qty) > 0 for o in (stop, target)):
            raise RuntimeError("position vanished without a known exit fill")
        if _attempt(position) == 1 and stop is not None and Decimal(stop.filled_qty) > 0:
            position.update(phase="stopped", reentry_after_utc=stop.filled_at or now)
        else:
            position.update(phase="complete", reason="protection_exit_confirmed")
        save()
    elif position.get("phase") == "exit_pending":
        close(str(position.get("exit_reason", "reconcile_exit")))


def _latest_sip_nbbo(symbol: str, observed_at_utc: datetime) -> FreshNbboQuote:
    frame = fetch_quotes(
        (symbol,),
        observed_at_utc - timedelta(seconds=10),
        observed_at_utc + timedelta(microseconds=1),
        feed="sip",
    ).sort("ts_utc")
    if frame.is_empty():
        raise CandidateRejected("symbol_sip_nbbo_unavailable")
    row = frame.tail(1).row(0, named=True)
    bid = row.get("bid_price")
    ask = row.get("ask_price")
    asof = row.get("ts_utc")
    feed = row.get("feed")
    if not isinstance(bid, (int, float)) or not isinstance(ask, (int, float)):
        raise CandidateRejected("symbol_sip_nbbo_prices_unavailable")
    if not isinstance(asof, datetime) or not isinstance(feed, str):
        raise CandidateRejected("symbol_sip_nbbo_identity_unavailable")
    return FreshNbboQuote(
        symbol=symbol,
        bid=Decimal(str(bid)),
        ask=Decimal(str(ask)),
        asof_utc=asof,
        feed=feed,
    )


def _optional_positive_env(name: str) -> float | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    value = float(raw)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _latest_sip_nbbo_now(symbol: str) -> FreshNbboQuote:
    return _latest_sip_nbbo(symbol, datetime.now(UTC))


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
) -> str | None:
    if base is None or order.filled_avg_price is None:
        return None
    quantity = int(Decimal(order.filled_qty))
    price = Decimal(order.filled_avg_price)
    return base.record_event(
        InvestmentTable.TRADE,
        f"paper:{client_order_id}:filled:{order.filled_qty}",
        {
            "运行ID": f"paper:{client_order_id}:filled:{order.filled_qty}",
            "成交时间": order.filled_at or datetime.now(UTC),
            "股票代码": symbol,
            "股票名称": symbol,
            "方向": direction,
            "订单状态": "已成交" if _filled(order) else "部分成交",
            "数量": quantity,
            "成交价格": str(price),
            "成交金额": str(price * quantity),
            "模拟账户": "paper",
            "持仓状态": "持仓中" if direction == "买入" else "退出成交，剩余仓位以券商对账为准",
            "触发来源": "modern_h15_momentum_v1",
            "下一动作": "继续盯盘" if direction == "买入" else "进入收盘复盘",
            "数据源状态": "alpaca.paper.direct|production=false",
            "执行摘要": f"{reason}；订单={order.id}；数量为订单累计成交，非本次增量",
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
        f"累计成交均价：${price}；本订单累计数量：{quantity}；原因：{reason}。\n"
        "仅Alpaca Paper模拟盘，禁止真实交易。"
    )
    if "\ufffd" in body or "??" in body:
        raise ValueError("Paper push contains invalid UTF-8 text")
    return push.push(body)


def _push_fill_once(
    store: PaperStateStore,
    push: LivermorePushClient,
    *,
    event_key: str,
    symbol: str,
    direction: str,
    order: BrokerOrder,
    reason: str,
) -> str | None:
    observed_at = datetime.now(UTC)
    store.enqueue_outbox(
        event_key=event_key,
        event_type="paper_fill",
        payload={
            "symbol": symbol,
            "direction": direction,
            "order_id": order.id,
            "cumulative_filled_qty": order.filled_qty,
            "cumulative_filled_avg_price": order.filled_avg_price,
            "reason": reason,
        },
        observed_at_utc=observed_at,
    )
    claim = store.claim_outbox(event_key, observed_at_utc=observed_at)
    if claim is OutboxClaim.SENT:
        return None
    if claim is OutboxClaim.IN_FLIGHT:
        raise RuntimeError("Paper fill notification has ambiguous delivery state")
    message_id = _push_fill(
        push,
        symbol=symbol,
        direction=direction,
        order=order,
        reason=reason,
    )
    store.mark_outbox_sent(
        event_key,
        message_id=message_id,
        observed_at_utc=datetime.now(UTC),
    )
    return message_id


def publish_fill_observations(
    store: PaperStateStore,
    push: LivermorePushClient,
    base: FeishuBaseEventClient | None,
    *,
    trade_date: date,
    events: list[dict[str, object]],
    message_ids: list[str],
) -> None:
    for symbol, position in store.load_symbol_states(trade_date).items():
        observations = position.get("fill_observations", {})
        if not isinstance(observations, dict):
            raise RuntimeError("persisted fill observations are invalid")
        for key, snapshot in observations.items():
            order = BrokerOrder.model_validate(snapshot["order"])
            if order.side not in {"buy", "sell"}:
                raise RuntimeError("broker fill side is unavailable")
            direction = "买入" if order.side == "buy" else "卖出"
            reason = "券商实际成交回报（数量为本订单累计值，非新增数量）"
            try:
                message_id = _push_fill_once(
                    store,
                    push,
                    event_key=str(key),
                    symbol=symbol,
                    direction=direction,
                    order=order,
                    reason=reason,
                )
                if message_id is not None:
                    message_ids.append(message_id)
            except Exception as exc:
                event: dict[str, object] = {
                    "type": "livermore_fill_delivery_unconfirmed",
                    "symbol": symbol,
                    "event_key": str(key),
                    "error_type": type(exc).__name__,
                }
                if event not in events:
                    events.append(event)
            if base is None:
                continue
            # Delivery state lives in the outbox, never overwrite trading state
            # from a stale snapshot while the reconciliation thread is advancing it.
            base_key = f"feishu:{key}"
            store.enqueue_outbox(
                event_key=base_key,
                event_type="feishu_fill",
                payload={"order_id": order.id, "cumulative_filled_qty": order.filled_qty},
                observed_at_utc=datetime.now(UTC),
            )
            claim = store.claim_outbox(base_key, observed_at_utc=datetime.now(UTC))
            if claim is OutboxClaim.SENT:
                continue
            try:
                if claim is OutboxClaim.IN_FLIGHT:
                    raise RuntimeError("Feishu fill delivery is ambiguous; reconcile receipt")
                record_id = _record_fill(
                    base,
                    client_order_id=order.client_order_id,
                    symbol=symbol,
                    direction=direction,
                    order=order,
                    reason=reason,
                )
                if not record_id:
                    raise RuntimeError("Feishu fill delivery has no record ID")
                store.mark_outbox_sent(
                    base_key, message_id=record_id, observed_at_utc=datetime.now(UTC)
                )
            except Exception as exc:
                event = {
                    "type": "feishu_write_failed",
                    "symbol": symbol,
                    "event_key": str(key),
                    "error_type": type(exc).__name__,
                }
                if event not in events:
                    events.append(event)


class NotificationPump:
    """One bounded background publisher; protection never waits for network delivery."""

    def __init__(
        self,
        store: PaperStateStore,
        push: LivermorePushClient,
        base: FeishuBaseEventClient | None,
        alerts: RuntimeAlertManager,
        trade_date: date,
    ):
        self.store, self.push, self.base = store, push, base
        self.alerts, self.trade_date = alerts, trade_date
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="paper-notifications")
        self.future: Future[tuple[list[dict[str, object]], list[str]]] | None = None

    def _deliver(self) -> tuple[list[dict[str, object]], list[str]]:
        events: list[dict[str, object]] = []
        messages: list[str] = []
        try:
            self.alerts.flush_pending()
        except Exception as exc:
            events.append({"type": "alarm_delivery_unconfirmed", "error_type": type(exc).__name__})
        publish_fill_observations(
            self.store,
            self.push,
            self.base,
            trade_date=self.trade_date,
            events=events,
            message_ids=messages,
        )
        return events, messages

    def _collect(self, events: list[dict[str, object]], messages: list[str]) -> None:
        if self.future is None:
            return
        try:
            new_events, new_messages = self.future.result()
        except Exception as exc:
            new_events = [{"type": "notification_worker_failed", "error_type": type(exc).__name__}]
            new_messages = []
        for event in new_events:
            if event not in events:
                events.append(event)
        messages.extend(message for message in new_messages if message not in messages)

    def poll(self, events: list[dict[str, object]], messages: list[str]) -> None:
        if self.future is None or self.future.done():
            self._collect(events, messages)
            self.future = self.executor.submit(self._deliver)

    def close(self, events: list[dict[str, object]], messages: list[str]) -> None:
        # Only call after the broker-management loop ends. Pending facts survive restart.
        self.executor.shutdown(wait=True)
        self._collect(events, messages)
        new_events, new_messages = self._deliver()
        events.extend(event for event in new_events if event not in events)
        messages.extend(message for message in new_messages if message not in messages)


def recover_prior_day_tick(
    broker: DirectAlpacaPaperBroker,
    store: PaperStateStore,
    *,
    history_root: Path,
    source_trade_date: date,
    trade_date: date,
    observed_at_utc: datetime,
) -> bool:
    """Explicit exit-only recovery. Old orders are evidence, never buy instructions."""
    history = read_prior_day_states(history_root, trade_date=trade_date)
    evidence = history.get(source_trade_date)
    if evidence is None:
        raise RuntimeError("explicit prior-day recovery source is unavailable")
    store.import_exit_recovery(
        evidence.path,
        source_trade_date=source_trade_date,
        trade_date=trade_date,
        observed_at_utc=observed_at_utc,
    )
    states = store.load_symbol_states(trade_date)
    source_dates = {source_trade_date}
    for position in states.values():
        source_date = date.fromisoformat(str(position.get("recovery_source_trade_date", "")))
        source = history.get(source_date)
        if (
            position.get("recovery_only") is not True
            or source is None
            or position.get("recovery_source_path") != str(source.path)
        ):
            raise RuntimeError("imported recovery state has no historical source proof")
        source_dates.add(source_date)
    # Recheck leases and immutable fingerprints for every previously imported day.
    for source_date in sorted(source_dates - {source_trade_date}):
        store.import_exit_recovery(
            history[source_date].path, source_trade_date=source_date, trade_date=trade_date,
            observed_at_utc=observed_at_utc,
        )
    open_orders, holdings = broker.list_open_orders(), broker.list_positions()
    if any(item.symbol not in states or item.side != "long" for item in holdings):
        raise RuntimeError("unknown broker inventory prevents prior-day recovery")
    held = {item.symbol: item for item in holdings}
    if len(held) != len(holdings):
        raise RuntimeError("duplicate broker inventory prevents prior-day recovery")
    for holding in holdings:
        quantity = Decimal(holding.qty)
        if not quantity.is_finite() or quantity <= 0 or quantity != int(quantity):
            raise RuntimeError("broker inventory is not a whole-share long")
    persisted_orders = store.list_orders()
    persisted_by_client = {order.client_order_id: order for order in persisted_orders}
    all_known: dict[str, BrokerOrder] = {}
    # Prove the full inventory for all symbols before performing any broker write.
    for symbol, position in states.items():
        source_date = date.fromisoformat(str(position["recovery_source_trade_date"]))
        original = history[source_date].states[symbol]
        entry_client = original.get("entry_client_id")
        if original.get("phase") != "entry_pending" and (
            not isinstance(entry_client, str) or not entry_client
        ):
            raise RuntimeError("historical entry proof is unavailable")
        clients = {
            str(value)
            for state in (original, position) for key, value in state.items()
            if key.endswith("_client_id") and isinstance(value, str)
        }
        clients.update(
            order.client_order_id for order in persisted_orders if order.symbol == symbol
        )
        known: dict[str, BrokerOrder] = {}
        for client_id in clients:
            order = broker.get_order_by_client_id(client_id)
            saved = persisted_by_client.get(client_id)
            if order is None:
                if (saved is not None and saved.broker_order_id is not None) or (
                    client_id == entry_client and original.get("phase") != "entry_pending"
                ):
                    raise RuntimeError("historical broker order proof is unavailable")
                continue
            if order.client_order_id != client_id or (
                client_id == entry_client and order.side != "buy"
            ) or (saved is not None and (
                (saved.broker_order_id is not None and saved.broker_order_id != order.id)
                or saved.symbol != order.symbol or saved.quantity != order.qty
                or order.side != ("buy" if saved.role == "entry" else "sell")
            )):
                raise RuntimeError("recovery order identity disagrees with historical state")
            known.update({item.id: item for item in _order_tree((order,))})
        if any(
            order.symbol != symbol or order.side not in {"buy", "sell"} for order in known.values()
        ):
            raise RuntimeError("recovery order identity disagrees with historical state")
        for order in known.values():
            if order.id in all_known:
                raise RuntimeError("broker order identity is shared by different recovery symbols")
            _require_known_order_status(order)
            quantity = Decimal(order.filled_qty)
            if (
                not quantity.is_finite() or not 0 <= quantity <= order.qty
                or quantity != int(quantity)
            ):
                raise RuntimeError("broker cumulative fill quantity is invalid")
        all_known.update(known)
        expected = sum(
            (Decimal(o.filled_qty) * (1 if o.side == "buy" else -1) for o in known.values()),
            Decimal(0),
        )
        actual = Decimal(held[symbol].qty) if symbol in held else Decimal(0)
        if expected < 0 or expected != actual:
            raise RuntimeError("historical filled inventory does not match broker holdings")
        if "recovery_initial_fill_keys" not in position:
            position["recovery_initial_fill_keys"] = [
                f"fill:{o.id}:{Decimal(o.filled_qty)}"
                for o in known.values()
                if Decimal(o.filled_qty) > 0
            ]
            position["recovery_broker_baseline"] = [
                o.model_dump(mode="json", by_alias=True) for o in known.values()
            ]
        for order in known.values():
            _remember_fill(position, order, observed_at_utc)
            if order.client_order_id in persisted_by_client:
                store.attach_broker_order(
                    client_order_id=order.client_order_id, broker_order_id=order.id,
                    status=order.status, observed_at_utc=observed_at_utc,
                )
        if actual > 0:
            position["shares"] = int(actual)
        if actual == 0 and not any(o.symbol == symbol for o in _order_tree(open_orders)) and all(
            o.status.lower() in TERMINAL for o in known.values()
        ):
            position.update(phase="complete", reason="recovery_inventory_flat")
        elif position.get("phase") == "complete":
            position["phase"] = "exit_pending"
        store.save_symbol_state(
            trade_date=trade_date, symbol=symbol, state=position, observed_at_utc=observed_at_utc
        )
    # Resolve parent/child proof before deciding whether an open order is foreign.
    for order in _order_tree(open_orders):
        proof = all_known.get(order.id)
        if proof is None or (
            proof.client_order_id, proof.symbol, proof.side, proof.qty,
        ) != (order.client_order_id, order.symbol, order.side, order.qty):
            raise RuntimeError("unknown broker order identity prevents prior-day recovery")
    for symbol, position in states.items():
        if position.get("phase") == "complete":
            continue
        if position.get("phase") != "entry_pending":
            position.update(phase="exit_pending", exit_reason="已确认归属的跨日残仓恢复清理")
        reconcile_symbol_position(
            broker,
            store,
            trade_date=trade_date,
            symbol=symbol,
            position=position,
            observed_at_utc=observed_at_utc,
            cancel_entries=True,
        )
    return not broker.list_positions() and not broker.list_open_orders()


def _run_prior_day_recovery(args: argparse.Namespace) -> None:
    now = datetime.now(UTC)
    sessions = build_xnys_schedule(args.trade_date, args.trade_date)
    if sessions.height != 1:
        raise RuntimeError("Paper recovery requires a current XNYS session")
    session = sessions.row(0, named=True)
    if not session["market_open_utc"] <= now < session["market_close_utc"]:
        raise RuntimeError("Paper recovery requires the current regular session")
    if args.source_trade_date is None or args.source_trade_date >= args.trade_date:
        raise ValueError("recovery requires an explicit prior --source-trade-date")
    enabled = os.getenv("BROKER_WRITE_ENABLED", "").lower() == "true"
    killed = os.getenv("TRADING_KILL_SWITCH", "true").lower() != "false"
    if args.check or not args.arm_paper or not enabled or killed:
        raise RuntimeError(
            "exit-only recovery requires explicit Paper arming and inactive kill switch"
        )
    broker = _broker(writes_enabled=True)
    push = _push_client()
    run_dir = ROOT / "runs" / "paper-recovery" / args.trade_date.isoformat()
    store = PaperStateStore(run_dir / "paper-state.sqlite3")
    lease_store = PaperStateStore(
        ROOT / "runs" / "modern-momentum" / args.trade_date.isoformat() / "paper-state.sqlite3"
    )
    owner = f"pid-{os.getpid()}"
    events: list[dict[str, object]] = []
    messages: list[str] = []
    alerts = RuntimeAlertManager(run_dir / "runtime-alerts.sqlite3", push=push, defer_delivery=True)
    notifications = NotificationPump(store, push, None, alerts, args.trade_date)
    try:
        account = broker.get_account()
        if account.status != "ACTIVE" or account.account_blocked or account.trading_blocked:
            raise RuntimeError("Paper account is not tradable")
        while datetime.now(UTC) < session["market_close_utc"]:
            if not lease_store.claim_run(
                args.trade_date, owner=owner, observed_at_utc=datetime.now(UTC)
            ):
                raise RuntimeError("another Paper writer holds the current-day lease")
            flat = recover_prior_day_tick(
                broker,
                store,
                history_root=args.history_root or ROOT / "runs" / "modern-momentum",
                source_trade_date=args.source_trade_date,
                trade_date=args.trade_date,
                observed_at_utc=datetime.now(UTC),
            )
            notifications.poll(events, messages)
            _save(
                run_dir / "recovery.json",
                {
                    "recovery_only": True,
                    "broker_flat": flat,
                    "source_trade_date": str(args.source_trade_date),
                    "events": events,
                    "message_ids": messages,
                    "positions": store.load_symbol_states(args.trade_date),
                },
            )
            if flat:
                print(json.dumps({"status": "recovered_flat", "paper_only": True}))
                return
            time.sleep(1)
        raise RuntimeError("recovery ended before flat broker confirmation")
    except Exception as exc:
        alerts.report_failure(
            "paper-exit-recovery",
            component="Paper跨日恢复",
            error_type=type(exc).__name__,
            observed_at_utc=datetime.now(UTC),
        )
        raise
    finally:
        broker.close()
        notifications.close(events, messages)
        push.close()


def main() -> None:
    load_project_env(ROOT)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True, type=date.fromisoformat)
    parser.add_argument("--arm-paper", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--confirmation-path", type=Path)
    parser.add_argument("--recover-prior-day-only", action="store_true")
    parser.add_argument("--source-trade-date", type=date.fromisoformat)
    parser.add_argument("--history-root", type=Path)
    args = parser.parse_args()
    if args.recover_prior_day_only:
        _run_prior_day_recovery(args)
        return
    data_root = project_data_root(ROOT)
    smoke_max_notional = _optional_positive_env("AI_QUANT_PAPER_SMOKE_MAX_NOTIONAL")
    pool = _latest_pool(data_root, args.trade_date)
    session = build_xnys_schedule(args.trade_date, args.trade_date).row(0, named=True)
    opened = session["market_open_utc"]
    market_close = session["market_close_utc"]
    start_at = opened + timedelta(minutes=26)
    entry_cutoff, cancel_at, flatten_at = session_control_times(opened, market_close)
    symbols = tuple(pool.get_column("symbol").to_list())
    confirmation_path = args.confirmation_path or (
        ROOT / "runs" / "autonomous" / args.trade_date.isoformat() / "open_confirmation.json"
    )
    confirmation_sha256 = hashlib.sha256(confirmation_path.read_bytes()).hexdigest()
    confirmation = load_open_confirmation(confirmation_path)
    plan_bytes = confirmation.config_path.read_bytes()
    plan = json.loads(plan_bytes)
    if not isinstance(plan, dict):
        raise ValueError("approved Paper plan must be an object")
    strategy_matches = approved_strategy_matches(plan)
    broker_write_enabled = os.getenv("BROKER_WRITE_ENABLED", "").strip().lower() == "true"
    kill_switch = os.getenv("TRADING_KILL_SWITCH", "true").strip().lower() != "false"
    broker = _broker(
        writes_enabled=bool(args.arm_paper and broker_write_enabled and not kill_switch)
    )
    runtime_policy = PaperRuntimePolicy()
    if not args.check:
        validate_smoke_notional(smoke_max_notional)
        runtime_policy.validate_arming(
            trade_date=args.trade_date,
            broker_write_enabled=broker_write_enabled and bool(args.arm_paper),
            trading_kill_switch=kill_switch,
            broker_base_url=broker.base_url,
            authorization=confirmation.authorization,
            expected_candidate_pool=symbols,
            expected_strategy_version=STRATEGY_VERSION,
        )
    observed_start_utc = datetime.now(UTC)
    account = broker.get_account()
    if account.status != "ACTIVE" or account.account_blocked or account.trading_blocked:
        raise RuntimeError("Alpaca Paper account is not tradable")
    if args.check:
        print(
            json.dumps(
                {
                    "status": "ready" if strategy_matches else "entry_blocked_strategy_mismatch",
                    "broker": broker.broker_identity,
                    "base_url": broker.base_url,
                    "symbols": symbols,
                    "start_at_utc": start_at,
                    "entry_cutoff_utc": entry_cutoff,
                    "cancel_at_utc": cancel_at,
                    "flatten_at_utc": flatten_at,
                    "paper_writes_enabled": broker.writes_enabled,
                    "live_trading_enabled": False,
                    "sizing": "account_equity_and_buying_power",
                    "smoke_max_notional": smoke_max_notional,
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
    store = PaperStateStore(run_dir / "paper-state.sqlite3")
    lease_owner = f"pid-{os.getpid()}"
    if not store.claim_run(
        args.trade_date,
        owner=lease_owner,
        observed_at_utc=datetime.now(UTC),
    ):
        raise RuntimeError("another Modern H15 Paper monitor owns the run lease")
    startup_open_orders = broker.list_open_orders()
    startup_positions = broker.list_positions()
    store.assert_reconcilable(
        args.trade_date,
        open_orders=startup_open_orders,
        positions=startup_positions,
        parent_orders=tuple(
            parent
            for intent in store.list_orders()
            if intent.trade_date == args.trade_date and intent.role == "entry"
            if (parent := broker.get_order_by_client_id(intent.client_order_id)) is not None
        ),
    )
    startup_evidence_error: str | None = None
    try:
        evidence_path = capture_startup(
            directory=run_dir, trade_date=args.trade_date, account=account,
            positions=startup_positions, open_orders=startup_open_orders,
            plan_path=confirmation.config_path, confirmation_path=confirmation_path,
            ledger_path=store.path, observed_start_utc=observed_start_utc,
            observed_end_utc=datetime.now(UTC),
        )
        evidence = json.loads(evidence_path.read_bytes())
        if (
            evidence["plan_sha256"] != confirmation.authorization.config_sha256
            or hashlib.sha256(plan_bytes).hexdigest() != confirmation.authorization.config_sha256
            or evidence["confirmation_sha256"] != confirmation_sha256
        ):
            raise ValueError("startup evidence differs from validated authorization")
    except Exception as exc:
        startup_evidence_error = type(exc).__name__
    persisted_states = store.load_symbol_states(args.trade_date)
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
            "smoke_max_notional": smoke_max_notional,
            "notional_cap_scope": "portfolio_positions_and_pending_buys_no_leverage",
            "attempt_risk_weights": ATTEMPT_WEIGHTS,
        },
        "status": "waiting",
        "entry_strategy_matches_approval": strategy_matches,
        "startup_evidence_error": startup_evidence_error,
    }
    config = ModernMomentumConfig()
    state["strategy_manifest"] = modern_strategy_manifest(config)
    candidate_blocks: dict[str, dict[str, object]] = {}
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
    sectors = {
        str(row["symbol"]): (str(row.get("sector_symbol", "")).strip().upper() or "UNKNOWN")
        for row in pool.iter_rows(named=True)
    }
    positions = {
        symbol: item
        for symbol, item in persisted_states.items()
        if item.get("phase") in {"entry_pending", "active", "exit_pending"}
    }
    completed = {
        symbol for symbol, item in persisted_states.items() if item.get("phase") == "complete"
    }
    attempts = {
        symbol: attempt
        for symbol, item in persisted_states.items()
        if isinstance((attempt := item.get("attempt")), int)
    }
    entries_started = {symbol for symbol, attempt in attempts.items() if attempt >= 1}
    reentry_after = {
        symbol: value
        for symbol, item in persisted_states.items()
        if isinstance((value := item.get("reentry_after_utc")), datetime)
    }
    events: list[dict[str, object]] = []
    message_ids: list[str] = []
    last_minute: datetime | None = None
    push = _push_client()
    alerts = RuntimeAlertManager(run_dir / "runtime-alerts.sqlite3", push=push, defer_delivery=True)
    base = FeishuBaseEventClient.from_environment()
    notifications = NotificationPump(store, push, base, alerts, args.trade_date)
    try:
        while datetime.now(UTC) < market_close:
            try:
                now = datetime.now(UTC)
                if not store.claim_run(
                    args.trade_date,
                    owner=lease_owner,
                    observed_at_utc=now,
                ):
                    raise RuntimeError("Modern H15 Paper run lease was lost")
                guard_account = broker.get_account()
                last_equity = float(guard_account.last_equity)
                if last_equity <= 0:
                    raise RuntimeError("Alpaca Paper last equity is invalid")
                daily_return = (float(guard_account.equity) - last_equity) / last_equity
                hard_loss_flatten = runtime_policy.must_flatten_for_daily_return(daily_return)
                lifecycle_errors: list[str] = []
                for symbol, position in list(positions.items()):
                    try:
                        if (now >= flatten_at or hard_loss_flatten) and position.get(
                            "phase"
                        ) == "active":
                            position.update(
                                phase="exit_pending",
                                exit_reason=(
                                    "日亏达到2%强制清仓"
                                    if hard_loss_flatten
                                    else "15:50日内强制清仓"
                                ),
                            )
                        reconcile_symbol_position(
                            broker,
                            store,
                            trade_date=args.trade_date,
                            symbol=symbol,
                            position=position,
                            observed_at_utc=now,
                            cancel_entries=(
                                now >= cancel_at
                                or hard_loss_flatten
                                or not strategy_matches
                                or alerts.is_frozen("modern-paper-loop")
                            ),
                        )
                        phase = position.get("phase")
                        if phase in {"complete", "stopped"}:
                            positions.pop(symbol)
                            if phase == "complete":
                                completed.add(symbol)
                            elif isinstance(
                                stopped_at := position.get("reentry_after_utc"), datetime
                            ):
                                reentry_after[symbol] = stopped_at
                    except Exception as exc:
                        # A broken symbol must not prevent protection of the others.
                        lifecycle_errors.append(f"{symbol}:{type(exc).__name__}")
                # Notifications use persisted broker fill snapshots, even after state
                # becomes terminal. Delivery faults must not interrupt broker protection.
                notifications.poll(events, message_ids)
                if lifecycle_errors:
                    state["lifecycle_errors"] = lifecycle_errors
                    raise RuntimeError("one_or_more_symbol_reconciliations_failed")

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

                    candidate_position = positions.get(symbol)
                    if candidate_position is None:
                        if startup_evidence_error is not None:
                            candidate_blocks[symbol] = {"code": "startup_evidence_unavailable"}
                            continue
                        if not strategy_matches:
                            candidate_blocks[symbol] = {
                                "code": "approved_strategy_manifest_mismatch"
                            }
                            continue
                        if now >= entry_cutoff or alerts.is_frozen("modern-paper-loop"):
                            continue
                        previous_attempt = attempts.get(symbol, 0)
                        attempt = previous_attempt + 1
                        if attempt > 2 or (
                            attempt == 1 and len(entries_started) >= MAX_DAILY_ENTRIES
                        ):
                            continue
                        if attempt == 1:
                            signal = latest_modern_momentum_signal(
                                symbol_bars,
                                session_open_utc=opened,
                                prior_close=prior_closes[symbol],
                                market_cap=market_caps[symbol],
                                premarket_rvol=rvols[symbol],
                                config=config,
                                asof_utc=complete_minute,
                            )
                            if signal is None:
                                continue
                            entry_price, stop_level = signal.entry_reference, signal.stop_level
                            signal_ts_utc, h15 = signal.signal_ts_utc, signal.h15
                        else:
                            reentry_stopped_at = reentry_after.get(symbol)
                            prior_state = store.load_symbol_states(args.trade_date).get(symbol, {})
                            h15_value = prior_state.get("h15")
                            if reentry_stopped_at is None or not isinstance(
                                h15_value, (int, float)
                            ):
                                candidate_blocks[symbol] = {"code": "reentry_history_unavailable"}
                                continue
                            h15 = float(h15_value)
                            reentry = pullback_reentry(
                                _five_minute_bars(symbol_bars, session_open_utc=opened),
                                stopped_at_utc=reentry_stopped_at,
                                h15=h15,
                                asof_utc=complete_minute,
                                session_open_utc=opened,
                                config=config,
                            )
                            if reentry is None:
                                continue
                            entry_price = reentry.entry_reference
                            stop_level = max(reentry.structural_stop, entry_price * 0.985)
                            signal_ts_utc = reentry.signal_ts_utc
                        try:
                            quote = bounded_retry(partial(_latest_sip_nbbo_now, symbol))
                        except CandidateRejected as exc:
                            candidate_blocks[symbol] = {"code": str(exc)}
                            alerts.report_failure(
                                f"market-data:{symbol}",
                                component=f"Paper {symbol} SIP",
                                error_type=str(exc),
                                observed_at_utc=datetime.now(UTC),
                            )
                            continue
                        quote_observed_at = datetime.now(UTC)
                        actual_all_in_stop_pct = (
                            float((quote.ask - Decimal(str(stop_level))) / quote.ask)
                            + config.stop_slippage_reserve_pct
                        )
                        current_account = broker.get_account()
                        remaining_slots = max(1, MAX_DAILY_ENTRIES - len(positions))
                        base_fraction = risk_fraction(hard_catalyst=hard_catalysts[symbol])
                        allocation_fraction = attempt_risk_fraction(
                            base_fraction,
                            attempt=attempt,
                        )
                        sector = sectors[symbol]
                        open_risks = {
                            open_symbol: _position_risk(open_position)
                            for open_symbol, open_position in positions.items()
                        }
                        broker_position_snapshot = broker.list_positions()
                        current_broker_positions = {
                            item.symbol: item for item in broker_position_snapshot
                        }
                        sector_main_has_profit = any(
                            open_symbol != symbol
                            and sectors.get(open_symbol, "UNKNOWN") == sector
                            and open_symbol in current_broker_positions
                            and current_broker_positions[open_symbol].current_price is not None
                            and current_broker_positions[open_symbol].avg_entry_price is not None
                            and Decimal(current_broker_positions[open_symbol].current_price or "0")
                            > Decimal(current_broker_positions[open_symbol].avg_entry_price or "0")
                            for open_symbol in positions
                        )
                        try:
                            runtime_policy.validate_entry_risk(
                                proposed_risk_fraction=allocation_fraction,
                                symbol_open_risk=open_risks.get(symbol, 0.0),
                                sector_open_risk=sum(
                                    risk
                                    for open_symbol, risk in open_risks.items()
                                    if sectors.get(open_symbol, "UNKNOWN") == sector
                                ),
                                portfolio_open_risk=sum(open_risks.values()),
                                daily_return=daily_return,
                                sector_main_has_profit=sector_main_has_profit,
                            )
                            entry_client_id = order_id(
                                args.trade_date.isoformat(),
                                symbol,
                                "entry",
                                attempt=attempt,
                            )
                            base_entry_client_id = entry_client_id
                            aborted_count = 0
                            while (old_intent := store.get_order(entry_client_id)) is not None:
                                if old_intent.status != "aborted":
                                    break
                                aborted_count += 1
                                entry_client_id = f"{base_entry_client_id}-r{aborted_count}"
                            pending_clients = {
                                str(item.get("entry_client_id", ""))
                                for item in positions.values()
                                if item.get("phase") == "entry_pending"
                            }
                            available_notional = remaining_entry_notional(
                                cap=smoke_max_notional, equity=current_account.equity,
                                positions=broker_position_snapshot,
                                open_orders=broker.list_open_orders(),
                                pending_entries=tuple(
                                    order for order in store.list_orders()
                                    if order.role == "entry" and (
                                        order.status.lower() not in TERMINAL | {"aborted"}
                                        or order.client_order_id in pending_clients
                                    )
                                ),
                            )
                            entry_builder = partial(
                                build_protected_entry,
                                client_order_id=entry_client_id,
                                symbol=symbol,
                                signal_reference=Decimal(str(entry_price)),
                                structural_stop=Decimal(str(stop_level)),
                                quote=quote,
                                stop_slippage_reserve=Decimal(
                                    str(config.stop_slippage_reserve_pct)
                                ),
                            )
                            protected_entry = entry_builder(
                                qty=1, observed_at_utc=datetime.now(UTC),
                            )
                            limit_price = Decimal(protected_entry.limit_price)
                            actual_all_in_stop_pct = float(
                                (limit_price - Decimal(protected_entry.stop_loss_price))
                                / limit_price
                            ) + config.stop_slippage_reserve_pct
                            quantity = min(
                                position_size(
                                    entry_price=float(limit_price),
                                    all_in_stop_pct=actual_all_in_stop_pct,
                                    equity=float(current_account.equity),
                                    buying_power=float(current_account.buying_power),
                                    risk_fraction=allocation_fraction,
                                    remaining_slots=remaining_slots,
                                    max_notional=float(available_notional),
                                ),
                                int(available_notional / limit_price),
                            )
                            if quantity < 1:
                                raise CandidateRejected("portfolio_cap_cannot_fund_one_share")
                            protected_entry = protected_entry.model_copy(update={"qty": quantity})
                        except (ValueError, RuntimeError) as exc:
                            candidate_blocks[symbol] = {
                                "code": "entry_guard_refused",
                                "reason": str(exc),
                                "observed_at_utc": datetime.now(UTC),
                            }
                            continue
                        if datetime.now(UTC) >= entry_cutoff or alerts.is_frozen(
                            "modern-paper-loop"
                        ):
                            candidate_blocks[symbol] = {"code": "entry_time_or_freeze_changed"}
                            continue
                        store.record_order_intent(
                            trade_date=args.trade_date,
                            client_order_id=entry_client_id,
                            symbol=symbol,
                            attempt=attempt,
                            role="entry",
                            quantity=quantity,
                            payload=protected_entry.broker_payload(),
                            observed_at_utc=quote_observed_at,
                        )
                        pending_position: dict[str, object] = {
                            "phase": "entry_pending",
                            "attempt": attempt,
                            "entry_client_id": entry_client_id,
                            "entry_request": protected_entry.model_dump(mode="json"),
                            "signal_ts_utc": signal_ts_utc,
                            "h15": h15,
                            "strategy_manifest": modern_strategy_manifest(config),
                            "stop_level": float(protected_entry.stop_loss_price),
                            "target_level": float(protected_entry.take_profit_price),
                            "sizing_equity": current_account.equity,
                            "sizing_buying_power": current_account.buying_power,
                            "available_portfolio_notional": str(available_notional),
                            "risk_fraction": allocation_fraction,
                            "sector": sector,
                            "all_in_stop_pct": actual_all_in_stop_pct,
                        }
                        prior_attempt_state = store.load_symbol_states(args.trade_date).get(symbol)
                        if prior_attempt_state is not None:
                            pending_position["fill_observations"] = prior_attempt_state.get(
                                "fill_observations", {}
                            )
                            pending_position["prior_attempt"] = {
                                key: value
                                for key, value in prior_attempt_state.items()
                                if key not in {"fill_observations", "prior_attempt"}
                            }
                        positions[symbol] = pending_position
                        store.save_symbol_state(
                            trade_date=args.trade_date,
                            symbol=symbol,
                            state=pending_position,
                            observed_at_utc=datetime.now(UTC),
                        )
                        attempts[symbol] = attempt
                        entries_started.add(symbol)

                        def validate_before_entry_post(
                            builder: Callable[..., ProtectedPaperEntryRequest] = entry_builder,
                            expected: ProtectedPaperEntryRequest = protected_entry,
                        ) -> None:
                            frozen = alerts.is_frozen("modern-paper-loop")
                            killed = (
                                os.getenv("TRADING_KILL_SWITCH", "true").strip().lower() != "false"
                            )
                            checked_at = datetime.now(UTC)
                            if (
                                checked_at >= entry_cutoff or frozen or killed
                                or os.getenv("BROKER_WRITE_ENABLED", "").strip().lower() != "true"
                            ):
                                raise PreSubmitRejected("entry_time_or_freeze_changed_before_post")
                            try:
                                rebuilt = builder(qty=expected.qty, observed_at_utc=checked_at)
                            except ValueError as exc:
                                raise PreSubmitRejected(
                                    "final_quote_or_protection_rejected"
                                ) from exc
                            if rebuilt != expected:
                                raise PreSubmitRejected("protected_entry_changed_before_post")

                        try:
                            entry = broker.submit_protected_entry_idempotent(
                                protected_entry, before_submit=validate_before_entry_post,
                            )
                        except PreSubmitRejected as exc:
                            store.abort_unsubmitted_entry(
                                client_order_id=entry_client_id, prior_state=prior_attempt_state,
                                observed_at_utc=datetime.now(UTC),
                            )
                            positions.pop(symbol)
                            if previous_attempt:
                                attempts[symbol] = previous_attempt
                            else:
                                attempts.pop(symbol, None)
                                entries_started.discard(symbol)
                            candidate_blocks[symbol] = {"code": str(exc)}
                            continue
                        store.attach_broker_order(
                            client_order_id=entry_client_id,
                            broker_order_id=entry.id,
                            status=entry.status,
                            observed_at_utc=datetime.now(UTC),
                        )
                        pending_position["entry_order_id"] = entry.id
                        store.save_symbol_state(
                            trade_date=args.trade_date,
                            symbol=symbol,
                            state=pending_position,
                            observed_at_utc=datetime.now(UTC),
                        )
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
                    entered_at = candidate_position.get("entered_at_utc")
                    target_level = candidate_position.get("target_level")
                    if not isinstance(entered_at, datetime) or not isinstance(
                        target_level, (int, float)
                    ):
                        raise RuntimeError("actual entry time or target is unavailable")
                    exit_reason = actual_position_exit_reason(
                        symbol_bars,
                        session_open_utc=opened,
                        entered_at_utc=entered_at,
                        asof_utc=complete_minute,
                        target_level=float(target_level),
                        liquidation_utc=flatten_at,
                        attempt=_attempt(candidate_position),
                    )
                    if exit_reason is not None:
                        close = request_position_exit(
                            broker,
                            store,
                            trade_date=args.trade_date,
                            symbol=symbol,
                            position=candidate_position,
                            observed_at_utc=datetime.now(UTC),
                            reason=exit_reason,
                        )
                        if close is not None:
                            events.append(
                                {
                                    "type": "paper_sell_submitted",
                                    "symbol": symbol,
                                    "order": close.id,
                                }
                            )
                state.update(
                    {
                        "positions": positions,
                        "attempts": attempts,
                        "reentry_after_utc": reentry_after,
                        "completed_symbols": sorted(completed),
                        "events": events,
                        "message_ids": message_ids,
                        "status": "frozen" if alerts.is_frozen("modern-paper-loop") else "running",
                        "candidate_blocks": candidate_blocks,
                        "last_complete_minute_utc": complete_minute,
                    }
                )
                _save(state_path, state)
                try:
                    alerts.report_recovery(
                        "modern-paper-loop",
                        component="Modern H15 Paper",
                        observed_at_utc=datetime.now(UTC),
                    )
                except Exception:
                    pass
                time.sleep(1)
            except Exception as exc:
                frames = traceback.extract_tb(exc.__traceback__)
                location = frames[-1] if frames else None
                state.update(
                    {
                        "status": "degraded",
                        "last_error_type": type(exc).__name__,
                        "last_error_location": (
                            f"{Path(location.filename).name}:{location.lineno}:{location.name}"
                            if location
                            else "unknown"
                        ),
                        "positions": positions,
                        "events": events,
                    }
                )
                _save(state_path, state)
                try:
                    alerts.report_failure(
                        "modern-paper-loop",
                        component="Modern H15 Paper",
                        error_type=type(exc).__name__,
                        observed_at_utc=datetime.now(UTC),
                    )
                except Exception:
                    pass
                time.sleep(5)
        remaining_positions = broker.list_positions()
        remaining_orders = broker.list_open_orders()
        state["status"] = (
            "complete" if not remaining_positions and not remaining_orders else "degraded"
        )
        state["remaining_positions"] = [item.symbol for item in remaining_positions]
        state["remaining_order_ids"] = [item.id for item in remaining_orders]
        state["broker_flat"] = not remaining_positions and not remaining_orders
        state["entry_frozen"] = alerts.is_frozen("modern-paper-loop")
        if state["entry_frozen"]:
            state["status"] = "frozen"
        elif not strategy_matches:
            state["status"] = "entry_blocked_strategy_mismatch"
        _save(state_path, state)
    finally:
        broker.close()
        notifications.close(events, message_ids)
        state["events"] = events
        state["message_ids"] = message_ids
        _save(state_path, state)
        push.close()


if __name__ == "__main__":
    main()
