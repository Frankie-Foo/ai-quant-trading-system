"""Discover one native run and derive reconciliation from startup and Paper facts."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from data_plane.calendar import build_xnys_schedule
from operations.local_env import alpaca_paper_credentials

from .broker_fills import alpaca_fill_page, collect_broker_fills
from .execution_summary import (
    build_factual_execution_summary,
    export_native_context,
    load_execution_index,
    read_pinned,
    require_utc,
    write_pinned_json,
)


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _within(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError("native evidence path escapes explicit run root")
    return resolved


def _backup_ledger(path: Path, output_dir: Path) -> tuple[Path, str]:
    """SQLite backup API includes committed WAL; never opens the source for writes."""
    if not path.is_file():
        raise ValueError("native ledger is unavailable")
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output_dir) as directory:
        copy = Path(directory) / "ledger.sqlite3"
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(copy)) as destination:
                source.backup(destination)
                destination.execute("PRAGMA journal_mode=DELETE")
        raw = copy.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    frozen = output_dir.resolve() / f"ledger-{digest}.sqlite3"
    try:
        with frozen.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        read_pinned(frozen, digest)
    return frozen, digest


def produce_daily(
    *,
    run_root: Path,
    trade_date: date,
    active_policy_hash: str,
    selection_cutoff_utc: datetime,
    as_of: datetime,
    output_dir: Path,
    get: Callable[[str, dict[str, Any]], object] | None = None,
    prior_execution_index_path: Path | None = None,
    prior_execution_index_sha256: str | None = None,
) -> dict[str, Any]:
    """No caller-supplied flat/complete flags; unknown evidence raises before submission."""
    require_utc(as_of)
    calendar = build_xnys_schedule(trade_date, trade_date)
    if calendar.height != 1:
        raise ValueError("daily evidence requires an XNYS session")
    session = calendar.row(0, named=True)
    opened, closed = session["market_open_utc"], session["market_close_utc"]
    if not isinstance(opened, datetime) or not isinstance(closed, datetime) or as_of < closed:
        raise ValueError("daily reconciliation requires a completed session")
    day = _within(run_root / "autonomous" / str(trade_date), run_root)
    runtime = _within(run_root / "modern-momentum" / str(trade_date), run_root)
    plan_path = _within(day / "modern_h15_paper_plan.json", run_root)
    confirmation_path = _within(day / "open_confirmation.json", run_root)
    pool_path = _within(day / "first_wave_pool.json", run_root)
    ledger_path = _within(runtime / "paper-state.sqlite3", run_root)
    plan_hash, confirmation_hash = _hash(plan_path), _hash(confirmation_path)
    context = export_native_context(
        plan_path=plan_path,
        plan_sha256=plan_hash,
        confirmation_path=confirmation_path,
        confirmation_sha256=confirmation_hash,
        first_pool_path=pool_path,
        first_pool_sha256=_hash(pool_path),
        active_policy_hash=active_policy_hash,
        selection_cutoff_utc=selection_cutoff_utc,
        as_of=as_of,
    )
    startups: list[tuple[datetime, Path, str, dict[str, Any]]] = []
    for path in runtime.glob("startup-*.json"):
        _within(path, run_root)
        digest = path.stem.removeprefix("startup-")
        raw = json.loads(read_pinned(path, digest))
        try:
            start = require_utc(datetime.fromisoformat(raw["observed_start_utc"]))
            end = require_utc(datetime.fromisoformat(raw["observed_end_utc"]))
            valid = (
                raw["schema_version"] == "paper_run_startup.v1"
                and raw["trade_date"] == str(trade_date)
                and raw["broker"] == "alpaca"
                and raw["environment"] == "paper"
                and raw["broker_base_url"] == "https://paper-api.alpaca.markets"
                and isinstance(raw["account"]["id"], str)
                and bool(raw["account"]["id"].strip())
                and raw["account"]["currency"] == "USD"
                and isinstance(raw["positions"], list)
                and isinstance(raw["open_orders"], list)
                and raw["plan_sha256"] == plan_hash
                and raw["confirmation_sha256"] == confirmation_hash
                and Path(raw["ledger_path"]).resolve() == ledger_path
                and Path(raw["plan_path"]).resolve() == plan_path
                and Path(raw["confirmation_path"]).resolve() == confirmation_path
                and context.available_at_utc <= start <= end < closed
                and start.astimezone(ZoneInfo("America/New_York")).date() == trade_date
            )
        except (KeyError, TypeError, ValueError):
            valid = False
        if not valid:
            raise ValueError("startup evidence identity/window is invalid")
        startups.append((end, path, digest, raw))
    if not startups:
        raise ValueError("valid flat startup evidence unavailable")
    startup_end, startup_path, startup_hash, startup = min(startups, key=lambda row: row[0])
    if startup["positions"] or startup["open_orders"]:
        raise ValueError("earliest startup is not flat; restart cannot replace baseline")
    if any(row[3]["account"]["id"] != startup["account"]["id"] for row in startups):
        raise ValueError("restart startup account mismatch")
    frozen, ledger_hash = _backup_ledger(ledger_path, output_dir)
    audit: dict[str, Any] = {
        "responses": [],
        "startup_path": str(startup_path),
        "startup_sha256": startup_hash,
        "orders_submitted": 0,
    }
    audit["all_startups"] = [{"sha256": row[2], "payload": row[3]} for row in startups]
    client = None
    if get is None:
        if as_of > datetime.now(UTC):
            raise ValueError("future reconciliation is unavailable")
        key, secret = alpaca_paper_credentials(os.environ)
        client = httpx.Client(
            base_url="https://paper-api.alpaca.markets",
            timeout=30,
            follow_redirects=False,
            headers={
                "APCA-API-KEY-ID": key.get_secret_value(),
                "APCA-API-SECRET-KEY": secret.get_secret_value(),
            },
        )

        def get(path: str, params: dict[str, Any]) -> object:
            assert client is not None
            response = client.get(path, params=params)
            response.raise_for_status()
            return response.json()

    def observed_get(path: str, params: dict[str, Any]) -> Any:
        assert get is not None
        value = get(path, params)
        audit["responses"].append({"path": path, "params": params, "body": value})
        return value

    try:
        account = observed_get("/v2/account", {})
        if account.get("id") != startup["account"]["id"] or account.get("currency") != "USD":
            raise ValueError("startup/broker account mismatch")
        # Inspect ALL activity types, not just FILL: unknown adjustments block.
        activities: dict[str, dict[str, Any]] = {}
        cursors: set[str] = set()
        cursor = None
        while True:
            page, token = alpaca_fill_page(observed_get, trade_date, cursor, all_activities=True)
            for activity in page:
                if activity.get("activity_type") != "FILL":
                    raise ValueError(
                        "non-FILL account activity requires reviewed position reconciliation"
                    )
                stamp = require_utc(datetime.fromisoformat(activity["transaction_time"]))
                if stamp <= startup_end:
                    raise ValueError("account FILL before startup prevents opening-flat proof")
                identifier = activity["id"]
                if identifier in activities and activities[identifier] != activity:
                    raise ValueError("conflicting activity correction")
                activities[identifier] = activity
            if token is None:
                break
            if token in cursors:
                raise ValueError("account activity pagination did not advance")
            cursors.add(token)
            cursor = token

        def read_order(identifier: str) -> dict[str, Any]:
            value = observed_get(f"/v2/orders/{quote(identifier, safe='')}", {"nested": "true"})
            if not isinstance(value, dict):
                raise ValueError("invalid broker order")
            return value

        metadata = {
            "schema_version": "loop_broker_fills.v1",
            "evidence_kind": "broker_confirmed_fills",
            "quantity_semantics": "incremental_execution",
            "trade_date": str(trade_date),
            "strategy_sha256": context.strategy_sha256,
            "plan_sha256": plan_hash,
            "broker": "alpaca",
            "account_id": account["id"],
            "environment": "paper",
            "currency": "USD",
            "source": "alpaca Paper account activities and native ledger",
            "validated_by": "loop_daily_native_provider.v1",
            "coverage_start_utc": opened.isoformat(),
            "coverage_end_utc": closed.isoformat(),
            "opening_positions_flat": True,
            "reconciled_complete": True,
            "costs_complete": False,
        }
        evidence = collect_broker_fills(
            plan=context,
            metadata=metadata,
            ledger_path=frozen,
            ledger_sha256=ledger_hash,
            read_fill_page=lambda _: (list(activities.values()), None),
            read_order=read_order,
        )
        if not evidence.reconciled_complete:
            raise ValueError("unowned account FILL prevents complete reconciliation")
        # Fail closed at the API's single-page ceiling; never call 500 rows complete.
        broker_orders = observed_get(
            "/v2/orders",
            {
                "status": "all",
                "after": opened.isoformat(),
                "until": closed.isoformat(),
                "limit": 500,
                "nested": "true",
                "direction": "asc",
            },
        )
        if not isinstance(broker_orders, list) or len(broker_orders) >= 500:
            raise ValueError("broker order inventory is incomplete")
        known_orders = evidence.source_evidence["orders"]
        if any(row.get("id") not in known_orders for row in broker_orders):
            raise ValueError("broker order absent from owned ledger")
        if observed_get("/v2/orders", {"status": "open", "limit": 500}) != []:
            raise ValueError("open broker orders prevent final reconciliation")
        positions = observed_get("/v2/positions", {})
        if positions != []:
            raise ValueError("daily provider requires flat final broker positions")
        actual: dict[str, Decimal] = {}
        for row in positions:
            if row.get("side") != "long" or row["symbol"] in actual:
                raise ValueError("unsupported broker inventory")
            actual[row["symbol"]] = Decimal(str(row["qty"]))
        expected: dict[str, Decimal] = {}
        for fill in evidence.fills:
            expected[fill.symbol] = expected.get(fill.symbol, Decimal(0)) + (
                fill.quantity if fill.side == "buy" else -fill.quantity
            )
        if {key: qty for key, qty in expected.items() if qty} != actual:
            raise ValueError("broker position differs from reconciled FILL inventory")
        _, final_ledger_hash = _backup_ledger(ledger_path, output_dir)
        if final_ledger_hash != ledger_hash:
            raise ValueError("native ledger changed during reconciliation; retry required")
    finally:
        if client is not None:
            client.close()
        audit_path, audit_hash = write_pinned_json(output_dir, "daily-broker-raw", audit)
    context_path, context_hash = write_pinned_json(
        output_dir,
        "review-context",
        context.model_dump(mode="json"),
    )
    payload = evidence.model_dump(mode="json")
    payload["source_evidence"].update(
        startup_sha256=startup_hash,
        all_startup_sha256=sorted(row[2] for row in startups),
        opening_flat_basis=(
            "flat startup plus complete day activities without earlier FILL or adjustments"
        ),
    )
    fills_path, fills_hash = write_pinned_json(output_dir, "broker-fills", payload)
    build_factual_execution_summary(
        plan_path=plan_path,
        plan_sha256=plan_hash,
        trade_date=trade_date,
        as_of=as_of,
        review_context_path=context_path,
        review_context_sha256=context_hash,
        fills_path=fills_path,
        fills_sha256=fills_hash,
    )
    prior = load_execution_index(prior_execution_index_path, prior_execution_index_sha256)
    entries = [
        entry.model_dump(mode="json")
        for entry in prior
        if (entry.trade_date, entry.strategy_sha256) != (trade_date, context.strategy_sha256)
    ]
    entries.append(
        {
            "trade_date": str(trade_date),
            "strategy_sha256": context.strategy_sha256,
            "plan_path": str(plan_path),
            "plan_sha256": plan_hash,
            "review_context_path": str(context_path),
            "review_context_sha256": context_hash,
            "fills_path": str(fills_path),
            "fills_sha256": fills_hash,
        }
    )
    index_path, index_hash = write_pinned_json(
        output_dir, "execution-index", {"executions": entries}
    )
    receipt = {
        "status": "prepared",
        "execution_index_path": str(index_path),
        "execution_index_sha256": index_hash,
        "raw_audit_path": str(audit_path),
        "raw_audit_sha256": audit_hash,
        "startup_sha256": startup_hash,
    }
    write_pinned_json(output_dir, "daily-provider-receipt", receipt)
    return receipt
