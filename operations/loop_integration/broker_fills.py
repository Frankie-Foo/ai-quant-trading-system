"""Explicit read-only broker activity adapter; local intents prove ownership only."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from .execution_summary import BrokerFill, BrokerFillEvidence, EffectiveModernPlan, read_pinned


def alpaca_fill_page(
    get: Callable[[str, dict[str, Any]], object], trade_date: date, cursor: str | None,
    *, all_activities: bool = False,
) -> tuple[list[dict[str, Any]], str | None]:
    """Alpaca pagination: page_token is the last activity id, not an offset."""
    params: dict[str, Any] = {"date": str(trade_date), "direction": "asc", "page_size": 100}
    if cursor is not None:
        params["page_token"] = cursor
    raw = get("/v2/account/activities" if all_activities else "/v2/account/activities/FILL", params)
    if not isinstance(raw, list) or len(raw) > 100 or any(
        not isinstance(row, dict) for row in raw
    ):
        raise ValueError("invalid broker FILL page")
    page: list[dict[str, Any]] = raw
    token = page[-1].get("id") if len(page) == 100 else None
    if len(page) == 100 and (not isinstance(token, str) or not token or token == cursor):
        raise ValueError("invalid broker FILL pagination token")
    return page, token


def collect_broker_fills(
    *, plan: EffectiveModernPlan, metadata: dict[str, Any],
    ledger_path: Path, ledger_sha256: str,
    read_fill_page: Callable[[str | None], tuple[list[dict[str, Any]], str | None]],
    read_order: Callable[[str], dict[str, Any]],
) -> BrokerFillEvidence:
    """Read injected FILL pages and order details, never order-writing capabilities.

    Ledger must be a frozen, checkpointed native paper-state SQLite export for
    this run. Caller attests run/plan/account provenance in validated metadata;
    the legacy ledger itself contains no strategy/config hash. No prefix matching.
    """
    if (metadata.get("strategy_sha256") != plan.strategy_sha256
            or metadata.get("plan_sha256") != plan.native_plan_sha256
            or metadata.get("trade_date") != str(plan.trade_date)):
        raise ValueError("broker metadata plan/strategy/date mismatch")
    for suffix in ("-wal", "-journal"):
        sidecar = Path(str(ledger_path) + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            raise ValueError("ledger requires a frozen checkpointed SQLite export")
    connection = sqlite3.connect(":memory:")
    try:
        connection.deserialize(read_pinned(ledger_path, ledger_sha256))
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT * FROM paper_orders WHERE trade_date=?", (str(plan.trade_date),)
        ).fetchall()
    finally:
        connection.close()
    owned = {str(row["client_order_id"]): dict(row) for row in rows}
    pool = {row.symbol for row in plan.candidates or ()}
    activities: dict[str, dict[str, Any]] = {}
    orders: dict[str, dict[str, Any]] = {}
    owned_broker_ids: set[str] = set()
    leg_owners: dict[str, dict[str, Any]] = {}
    for client_id, owner in owned.items():
        broker_id = owner["broker_order_id"]
        if broker_id is None:
            if owner["status"] == "aborted":
                continue  # Known pre-POST refusal; not a network-ambiguous intent.
            # An unacknowledged intent cannot prove absence at the broker.
            if metadata.get("reconciled_complete") is True:
                raise ValueError("unresolved native order intent prevents complete reconciliation")
            continue
        order = read_order(broker_id)
        if order.get("id") != broker_id or order.get("client_order_id") != client_id:
            raise ValueError("broker order ownership mismatch")
        orders[broker_id] = order
        owned_broker_ids.add(broker_id)
        for leg in order.get("legs") or []:
            leg_id = leg["id"]
            if leg.get("symbol") != owner["symbol"] or leg.get("side") != "sell":
                raise ValueError("broker bracket leg ownership mismatch")
            leg_owners[leg_id] = {
                **owner, "broker_order_id": leg_id, "payload_json": '{"side":"sell"}',
            }
            orders[leg_id] = leg
            owned_broker_ids.add(leg_id)
    cursors: set[str] = set()
    cursor = None
    while True:
        page, next_cursor = read_fill_page(cursor)
        for activity in page:
            if (activity.get("activity_type") != "FILL"
                    or activity.get("quantity_semantics", "incremental_execution")
                    != "incremental_execution"):
                raise ValueError("only broker incremental FILL activities are supported")
            fill_id = activity.get("id")
            if not isinstance(fill_id, str) or not fill_id.strip():
                raise ValueError("broker execution id required")
            if fill_id in activities and activities[fill_id] != activity:
                raise ValueError("conflicting broker execution correction requires reconciliation")
            activities[fill_id] = activity
        if next_cursor is None:
            break
        if not next_cursor or next_cursor in cursors:
            raise ValueError("broker pagination did not advance")
        cursors.add(next_cursor)
        cursor = next_cursor
    fills: list[BrokerFill] = []
    excluded: list[str] = []
    for fill_id, activity in activities.items():
        order_id = str(activity["order_id"])
        if order_id not in orders:
            orders[order_id] = read_order(order_id)
        order = orders[order_id]
        if order.get("id") != order_id:
            raise ValueError("broker order id mismatch")
        fill_owner = owned.get(str(order.get("client_order_id", ""))) or leg_owners.get(order_id)
        if fill_owner is None:
            excluded.append(fill_id)
            continue
        payload = json.loads(fill_owner["payload_json"])
        if (fill_owner["status"] == "aborted" or fill_owner["symbol"] not in pool
                or fill_owner["symbol"] != activity.get("symbol")
                or order.get("symbol") != activity.get("symbol")
                or payload.get("side") != activity.get("side")
                or order.get("side") != activity.get("side")
                or fill_owner["broker_order_id"] not in (None, order_id)):
            raise ValueError("broker execution disagrees with native strategy ownership")
        fill = BrokerFill.model_validate({
            "fill_id": fill_id, "broker_order_id": order_id,
            "symbol": activity["symbol"], "side": activity["side"],
            "quantity": activity["qty"], "price": activity["price"],
            "filled_at_utc": activity["transaction_time"],
            "fees": activity.get("fees"), "fee_source": activity.get("fee_source"),
            "source": f"{metadata['source']}#FILL:{fill_id}", "broker_confirmed": True,
        })
        if fill.filled_at_utc < max(plan.effective_at_utc, plan.available_at_utc):
            raise ValueError("broker fill precedes effective plan")
        fills.append(fill)
    # Cumulative order values are reconciliation checks, NEVER increments.
    for order_id in owned_broker_ids | {fill.broker_order_id for fill in fills}:
        selected = [fill for fill in fills if fill.broker_order_id == order_id]
        quantity = sum((fill.quantity for fill in selected), Decimal(0))
        order = orders[order_id]
        if metadata.get("reconciled_complete") is True:
            if quantity != Decimal(str(order["filled_qty"])):
                raise ValueError("incremental FILL total differs from broker cumulative quantity")
    return BrokerFillEvidence.model_validate({
        **metadata, "fills": [fill.model_dump(mode="json") for fill in fills],
        "reconciled_complete": metadata.get("reconciled_complete") is True and not excluded,
        "costs_complete": metadata.get("costs_complete") is True
        and all(fill.fees is not None for fill in fills),
        "source_evidence": {
            "ledger_sha256": ledger_sha256,
            "ownership_basis": "native paper_orders exact client/broker id; caller-pinned run",
            "activities": list(activities.values()), "orders": orders,
            "excluded_activity_ids": excluded,
        },
    })
