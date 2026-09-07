"""Owner-approved Paper portfolio release limits; never an arming mechanism."""

from __future__ import annotations

import argparse
import math
from decimal import Decimal, InvalidOperation

from execution.alpaca_paper import BrokerOrder, PaperPosition
from operations.paper_state import TERMINAL_ORDER_STATUSES, StoredPaperOrder

MAX_PAPER_PORTFOLIO_NOTIONAL = 200_000.0


def validate_smoke_notional(value: float | None) -> float:
    """Keep the old interface, but interpret the cap as total portfolio notional."""
    if (
        value is None or isinstance(value, bool) or not math.isfinite(value)
        or not 0 < value <= MAX_PAPER_PORTFOLIO_NOTIONAL
    ):
        raise ValueError(
            "Paper requires an explicit finite portfolio notional cap in "
            f"(0, {MAX_PAPER_PORTFOLIO_NOTIONAL:g}] USD; not a per-symbol allowance"
        )
    return value


def _amount(value: object, *, zero_allowed: bool = False) -> Decimal:
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("Paper portfolio amount is unavailable or invalid") from exc
    if not amount.is_finite() or amount < 0 or (not zero_allowed and amount == 0):
        raise ValueError("Paper portfolio amount must be finite and positive")
    return amount


def remaining_entry_notional(
    *, cap: float | None, equity: str, positions: tuple[PaperPosition, ...],
    open_orders: tuple[BrokerOrder, ...],
    pending_entries: tuple[StoredPaperOrder, ...] = (),
) -> Decimal:
    """Gross long exposure plus buy remainders; sells never release budget early."""
    ceiling = min(Decimal(str(validate_smoke_notional(cap))), _amount(equity))
    held: dict[str, Decimal] = {}
    for position in positions:
        if position.side != "long" or position.symbol in held:
            raise ValueError("Paper portfolio requires unique long position snapshots")
        _amount(position.qty)
        held[position.symbol] = _amount(position.market_value)
    seen: dict[str, tuple[object, ...]] = {}
    clients: dict[str, str] = {}
    partial_fills: dict[str, Decimal] = {}
    unresolved = {entry.client_order_id: entry for entry in pending_entries}
    if len(unresolved) != len(pending_entries):
        raise ValueError("duplicate pending Paper entry intents")
    aborted_clients: set[str] = set()
    for aborted_intent in pending_entries:
        if aborted_intent.status == "aborted":
            if aborted_intent.role != "entry" or aborted_intent.broker_order_id is not None:
                raise ValueError("local aborted entry has conflicting broker identity")
            aborted_clients.add(aborted_intent.client_order_id)
            unresolved.pop(aborted_intent.client_order_id)
    reserved = Decimal(0)
    pending = list(open_orders)
    while pending:
        order = pending.pop()
        pending.extend(order.legs)
        if order.client_order_id in aborted_clients:
            raise ValueError("local aborted entry unexpectedly exists at broker")
        identity = (
            order.client_order_id, order.symbol, order.side, order.qty,
            order.filled_qty, order.limit_price, order.status, order.order_type,
        )
        if (
            not order.id or not order.client_order_id
            or seen.get(order.id, identity) != identity
            or clients.get(order.client_order_id, order.id) != order.id
        ):
            raise ValueError("conflicting Paper order snapshots prevent entry budgeting")
        if order.id in seen:
            continue
        seen[order.id] = identity
        clients[order.client_order_id] = order.id
        if order.side not in {"buy", "sell"}:
            raise ValueError("Paper order side is unavailable for entry budgeting")
        if order.side == "sell" or order.status.lower() in TERMINAL_ORDER_STATUSES:
            continue
        saved = unresolved.pop(order.client_order_id, None)
        if saved is not None and (
            saved.role != "entry" or saved.symbol != order.symbol or saved.quantity != order.qty
            or (saved.broker_order_id is not None and saved.broker_order_id != order.id)
        ):
            raise ValueError("Paper entry intent conflicts with broker budget snapshot")
        filled = _amount(order.filled_qty, zero_allowed=True)
        if filled > order.qty:
            raise ValueError("Paper buy fill exceeds order quantity")
        if order.order_type != "limit":
            raise ValueError("unbounded active Paper buy prevents new entries")
        price = _amount(
            order.limit_price if order.limit_price is not None
            else saved.payload.get("limit_price") if saved is not None else None
        )
        reserved += (Decimal(order.qty) - filled) * price
        partial_fills[order.symbol] = partial_fills.get(order.symbol, Decimal(0)) + filled * price
    # Reserve the entire request until reconciliation resolves a missing broker snapshot.
    for saved in unresolved.values():
        if saved.role != "entry" or saved.payload.get("type") != "limit" or saved.quantity <= 0:
            raise ValueError("unpriced pending Paper entry prevents new entries")
        reserved += Decimal(saved.quantity) * _amount(saved.payload.get("limit_price"))
    # A lagging position endpoint must not make a partial buy look cheaper.
    used = reserved + sum(
        (max(held.get(symbol, Decimal(0)), partial_fills.get(symbol, Decimal(0)))
         for symbol in held.keys() | partial_fills.keys()), Decimal(0),
    )
    return max(Decimal(0), ceiling - used)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validate-cap", required=True, type=float)
    args = parser.parse_args()
    try:
        cap = validate_smoke_notional(args.validate_cap)
    except ValueError as exc:
        parser.error(str(exc))
    print(format(cap, ".15g"))


if __name__ == "__main__":
    main()
