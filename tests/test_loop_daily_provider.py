"""Daily native discovery against frozen fixture inputs and injected Paper GET."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from test_loop_execution import AS_OF, TRADE_DATE
from test_loop_providers import native_inputs

from operations.loop_integration.execution_summary import load_execution_index, write_pinned_json
from operations.paper_state import PaperStateStore


def daily_inputs(root: Path) -> tuple[Path, dict[str, Any]]:
    day = root / "autonomous" / str(TRADE_DATE)
    day.mkdir(parents=True)
    inputs = native_inputs(day)
    plan_path = day / "modern_h15_paper_plan.json"
    plan_path.write_bytes(inputs["plan_path"].read_bytes())
    (day / "first_wave_pool.json").write_bytes(inputs["first_pool_path"].read_bytes())
    confirmation = json.loads(inputs["confirmation_path"].read_bytes())
    confirmation["config_path"] = str(plan_path)
    confirmation_path, confirmation_hash = write_pinned_json(day, "confirmation", confirmation)
    (day / "open_confirmation.json").write_bytes(confirmation_path.read_bytes())
    runtime = root / "modern-momentum" / str(TRADE_DATE)
    runtime.mkdir(parents=True)
    ledger = runtime / "paper-state.sqlite3"
    PaperStateStore(ledger)
    startup = {
        "schema_version": "paper_run_startup.v1",
        "trade_date": str(TRADE_DATE),
        "observed_start_utc": "2026-09-01T13:39:00Z",
        "observed_end_utc": "2026-09-01T13:40:00Z",
        "broker": "alpaca",
        "broker_base_url": "https://paper-api.alpaca.markets",
        "environment": "paper",
        "account": {"id": "fixture-account", "currency": "USD"},
        "positions": [],
        "open_orders": [],
        "plan_sha256": inputs["plan_sha256"],
        "confirmation_sha256": confirmation_hash,
        "ledger_path": str(ledger.resolve()),
        "plan_path": str(plan_path.resolve()),
        "confirmation_path": str((day / "open_confirmation.json").resolve()),
    }
    write_pinned_json(runtime, "startup", startup)
    return ledger, startup


def test_daily_native_discovery_derives_empty_execution_without_manual_metadata(
    tmp_path: Path,
) -> None:
    from operations.loop_integration.daily_provider import produce_daily

    daily_inputs(tmp_path)
    calls: list[str] = []

    def get(path: str, params: dict[str, Any]) -> object:
        calls.append(path)
        if path == "/v2/account":
            return {"id": "fixture-account", "currency": "USD"}
        assert path in {"/v2/account/activities", "/v2/positions", "/v2/orders"}
        return []

    receipt = produce_daily(
        run_root=tmp_path,
        trade_date=TRADE_DATE,
        active_policy_hash="a" * 64,
        selection_cutoff_utc=datetime(2026, 9, 1, 13, 25, tzinfo=UTC),
        as_of=AS_OF,
        output_dir=tmp_path / "loop",
        get=get,
    )
    assert receipt["status"] == "prepared"
    index = load_execution_index(
        Path(receipt["execution_index_path"]),
        receipt["execution_index_sha256"],
    )
    fills = json.loads(index[0].fills_path.read_bytes())
    assert fills["opening_positions_flat"] is True
    assert fills["reconciled_complete"] is True
    assert fills["fills"] == []
    assert "/v2/account/activities" in calls


@pytest.mark.parametrize(
    "case",
    [
        "pre_start_fill",
        "adjustment",
        "orphan_fill",
        "foreign_order",
        "positions",
        "open_orders",
        "restart_mismatch",
    ],
)
def test_daily_unknown_or_changed_inventory_never_confirms_zero(
    tmp_path: Path,
    case: str,
) -> None:
    from operations.loop_integration.daily_provider import produce_daily

    _, startup = daily_inputs(tmp_path)
    if case == "restart_mismatch":
        write_pinned_json(
            tmp_path / "modern-momentum" / str(TRADE_DATE),
            "startup",
            {
                **startup,
                "account": {"id": "other-account", "currency": "USD"},
                "observed_end_utc": "2026-09-01T14:00:00Z",
            },
        )

    def get(path: str, params: dict[str, Any]) -> object:
        if path == "/v2/account":
            return {"id": "fixture-account", "currency": "USD"}
        if path == "/v2/account/activities":
            if case == "adjustment":
                return [{"id": "adj", "activity_type": "JNLS"}]
            if case in {"pre_start_fill", "orphan_fill"}:
                return [
                    {
                        "id": "f1",
                        "activity_type": "FILL",
                        "order_id": "orphan",
                        "qty": "1",
                        "price": "100",
                        "symbol": "MORNING",
                        "side": "buy",
                        "transaction_time": "2026-09-01T13:31:00Z"
                        if case == "pre_start_fill"
                        else "2026-09-01T14:00:00Z",
                    }
                ]
            return []
        if path == "/v2/orders/orphan":
            return {"id": "orphan", "client_order_id": "missing-intent"}
        if path == "/v2/positions" and case == "positions":
            return [{"symbol": "MORNING", "qty": "1", "side": "long"}]
        if path == "/v2/orders" and (
            case == "foreign_order" or case == "open_orders" and params["status"] == "open"
        ):
            return [{"id": "unknown"}]
        return []

    with pytest.raises(ValueError):
        produce_daily(
            run_root=tmp_path,
            trade_date=TRADE_DATE,
            active_policy_hash="a" * 64,
            selection_cutoff_utc=datetime(2026, 9, 1, 13, 25, tzinfo=UTC),
            as_of=AS_OF,
            output_dir=tmp_path / "loop",
            get=get,
        )


@pytest.mark.parametrize("status", ["aborted", "intent"])
def test_daily_known_prepost_abort_is_not_an_unresolved_intent(tmp_path: Path, status: str) -> None:
    import sqlite3

    from operations.loop_integration.daily_provider import produce_daily

    ledger, _ = daily_inputs(tmp_path)
    store = PaperStateStore(ledger)
    store.record_order_intent(
        trade_date=TRADE_DATE,
        client_order_id="prepost-abort",
        symbol="MORNING",
        attempt=1,
        role="entry",
        quantity=1,
        payload={"side": "buy"},
        observed_at_utc=datetime(2026, 9, 1, 14, tzinfo=UTC),
    )
    if status == "aborted":
        with sqlite3.connect(ledger) as connection:
            connection.execute("UPDATE paper_orders SET status='aborted'")

    def get(path: str, params: dict[str, Any]) -> object:
        return {"id": "fixture-account", "currency": "USD"} if path == "/v2/account" else []

    args: dict[str, Any] = dict(
        run_root=tmp_path,
        trade_date=TRADE_DATE,
        active_policy_hash="a" * 64,
        selection_cutoff_utc=datetime(2026, 9, 1, 13, 25, tzinfo=UTC),
        as_of=AS_OF,
        output_dir=tmp_path / "loop",
        get=get,
    )
    if status == "aborted":
        assert produce_daily(**args)["status"] == "prepared"
    else:
        with pytest.raises(ValueError, match="unresolved"):
            produce_daily(**args)


def test_daily_owned_roundtrip_generates_realized_gross_and_unknown_net(tmp_path: Path) -> None:
    from operations.loop_integration.daily_provider import produce_daily
    from operations.loop_integration.execution_summary import build_factual_execution_summary

    ledger, _ = daily_inputs(tmp_path)
    store = PaperStateStore(ledger)
    orders: dict[str, dict[str, Any]] = {}
    activities = []
    for side, price, hour in (("buy", "100", 14), ("sell", "110", 15)):
        identifier = f"strategy-{side}"
        store.record_order_intent(
            trade_date=TRADE_DATE,
            client_order_id=identifier,
            symbol="MORNING",
            attempt=1,
            role="entry" if side == "buy" else "exit",
            quantity=5,
            payload={"side": side},
            observed_at_utc=datetime(2026, 9, 1, hour, tzinfo=UTC),
        )
        store.attach_broker_order(
            client_order_id=identifier,
            broker_order_id=side,
            status="filled",
            observed_at_utc=datetime(2026, 9, 1, hour, tzinfo=UTC),
        )
        orders[side] = {
            "id": side,
            "client_order_id": identifier,
            "symbol": "MORNING",
            "side": side,
            "filled_qty": "5",
            "filled_avg_price": price,
        }
        activities.append(
            {
                "id": identifier,
                "order_id": side,
                "symbol": "MORNING",
                "side": side,
                "activity_type": "FILL",
                "qty": "5",
                "cum_qty": "5",
                "price": price,
                "transaction_time": f"2026-09-01T{hour}:00:00Z",
            }
        )

    def get(path: str, params: dict[str, Any]) -> object:
        if path == "/v2/account":
            return {"id": "fixture-account", "currency": "USD"}
        if path == "/v2/account/activities":
            return activities
        if path.startswith("/v2/orders/"):
            return orders[path.rsplit("/", 1)[1]]
        if path == "/v2/orders" and params["status"] == "all":
            return list(orders.values())
        return []

    receipt = produce_daily(
        run_root=tmp_path,
        trade_date=TRADE_DATE,
        active_policy_hash="a" * 64,
        selection_cutoff_utc=datetime(2026, 9, 1, 13, 25, tzinfo=UTC),
        as_of=AS_OF,
        output_dir=tmp_path / "loop",
        get=get,
    )
    entry = load_execution_index(
        Path(receipt["execution_index_path"]),
        receipt["execution_index_sha256"],
    )[0]
    summary = build_factual_execution_summary(
        **entry.attachment_args(),
        trade_date=TRADE_DATE,
        as_of=AS_OF,
    )
    assert summary["realized_gross_pnl"] == 50
    assert summary["realized_net_pnl"] is None
    assert summary["fees"] is None


def test_daily_cli_and_history_only_need_no_manual_metadata_or_prior_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    from kernel.strategy_policy import build_strategy_policy, write_strategy_policy
    from scripts.produce_loop_daily import run

    daily_inputs(tmp_path)
    active = build_strategy_policy(
        version="fixture-active",
        status="active",
        min_rvol=3.0,
        created_at_utc=AS_OF,
        approved_by="fixture-owner",
        approved_at_utc=AS_OF,
    )
    active_path = tmp_path / "active.json"
    write_strategy_policy(active_path, active)
    first_path = tmp_path / "autonomous" / str(TRADE_DATE) / "first_wave_pool.json"
    first = json.loads(first_path.read_bytes())
    first["strategy_context"]["active_policy_hash"] = active.policy_hash
    first_path.write_text(json.dumps(first), encoding="utf-8")
    requests: list[str] = []

    def transport(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET" and request.url.host == "paper-api.alpaca.markets"
        requests.append(request.url.path)
        return httpx.Response(
            200,
            json={"id": "fixture-account", "currency": "USD"}
            if request.url.path == "/v2/account"
            else [],
        )

    client_type = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: client_type(
            **kw,
            transport=httpx.MockTransport(transport),
        ),
    )
    monkeypatch.setenv("ALPACA_PAPER_KEY_ID", "fixture-key")
    monkeypatch.setenv("ALPACA_PAPER_SECRET_KEY", "fixture-secret")
    produced = run(
        [
            "--run-root",
            str(tmp_path),
            "--trade-date",
            str(TRADE_DATE),
            "--active-policy",
            str(active_path),
            "--as-of",
            AS_OF.isoformat(),
            "--selection-cutoff-utc",
            "2026-09-01T13:25:00Z",
        ]
    )
    assert produced["status"] == "prepared"
    count = len(requests)
    historical = run(["--run-root", str(tmp_path), "--trade-date", "2026-09-02", "--history-only"])
    entries = load_execution_index(
        Path(historical["execution_index_path"]), historical["execution_index_sha256"]
    )
    assert len(entries) == 1 and entries[0].trade_date == TRADE_DATE
    assert len(requests) == count  # Missing today's plan does not trigger broker discovery.
