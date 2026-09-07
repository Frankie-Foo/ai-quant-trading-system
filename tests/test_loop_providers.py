"""No-secret native exports and broker API boundary fixtures."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from test_loop_execution import AS_OF, TRADE_DATE, native_plan_payload, pinned_json

from operations.autonomous_selection_handoff import create_open_confirmation
from operations.loop_integration import execution_summary


def native_inputs(tmp_path: Path) -> dict[str, Any]:
    native = native_plan_payload()
    native.pop("loop_review")
    native["candidates"] = [{"symbol": "MORNING"}]
    plan_path = tmp_path / "native.json"
    plan_hash = pinned_json(plan_path, native)
    confirmation = tmp_path / "confirmation.json"
    create_open_confirmation(
        confirmation_path=confirmation, config_path=plan_path, trade_date=TRADE_DATE,
        selection_snapshot_id="final-selection", candidate_pool=("MORNING",),
        feishu_record_ids=("fixture-record",), livermore_message_id="fixture-message",
        strategy_version="modern-h15.v1",
        generated_at_utc=datetime(2026, 9, 1, 13, 35, tzinfo=UTC),
    )
    import hashlib
    confirmation_hash = hashlib.sha256(confirmation.read_bytes()).hexdigest()
    pool = tmp_path / "first.json"
    pool_hash = pinned_json(pool, {
        "schema_version": "modern_funnel.first_wave.v2", "trade_date": str(TRADE_DATE),
        "generated_at_utc": "2026-09-01T13:20:00+00:00",
        "strategy_context": {"active_policy_hash": "a" * 64},
        "candidates": [{"symbol": "MORNING"}, {"symbol": "WATCH", "rvol": 2}],
    })
    return dict(
        plan_path=plan_path, plan_sha256=plan_hash,
        confirmation_path=confirmation, confirmation_sha256=confirmation_hash,
        first_pool_path=pool, first_pool_sha256=pool_hash, active_policy_hash="a" * 64,
        selection_cutoff_utc=datetime(2026, 9, 1, 13, 25, tzinfo=UTC), as_of=AS_OF,
    )


def test_native_context_exports_full_pool_without_fabricated_verdict(tmp_path: Path) -> None:
    inputs = native_inputs(tmp_path)
    context = execution_summary.export_native_context(**inputs)
    assert context.available_at_utc == datetime(2026, 9, 1, 13, 35, tzinfo=UTC)
    assert context.candidates is not None and len(context.candidates) == 2
    assert context.candidates[1].symbol == "WATCH"
    assert context.candidates[1].verdict is None
    assert context.strategy.risk_policy.symbol_risk_fraction == .005


def test_broker_incremental_activities_not_snapshots_or_manual_orders(tmp_path: Path) -> None:
    import hashlib

    from test_loop_execution import fills_payload

    from operations.loop_integration import broker_fills
    from operations.paper_state import PaperStateStore

    context = execution_summary.export_native_context(**native_inputs(tmp_path))
    ledger_path = tmp_path / "ledger.sqlite3"
    store = PaperStateStore(ledger_path)
    store.record_order_intent(
        trade_date=TRADE_DATE, client_order_id="strategy-entry", symbol="MORNING",
        attempt=1, role="entry", quantity=5,
        payload={"side": "buy"}, observed_at_utc=context.available_at_utc,
    )
    store.attach_broker_order(
        client_order_id="strategy-entry", broker_order_id="b1", status="filled",
        observed_at_utc=context.available_at_utc,
    )
    import sqlite3
    frozen_path = tmp_path / "frozen.sqlite3"
    with sqlite3.connect(ledger_path) as source, sqlite3.connect(frozen_path) as dest:
        source.backup(dest)
        dest.execute("PRAGMA journal_mode=DELETE")
    ledger_path = frozen_path
    ledger_hash = hashlib.sha256(ledger_path.read_bytes()).hexdigest()
    metadata = fills_payload(str(context.native_plan_sha256), [])
    metadata.pop("fills")
    activity = {"id": "f1", "activity_type": "FILL", "order_id": "b1",
                "symbol": "MORNING", "side": "buy", "qty": "2", "price": "100",
                "transaction_time": "2026-09-01T14:00:00Z"}
    second = {**activity, "id": "f2", "qty": "3", "price": "102"}
    manual = {**activity, "id": "manual", "order_id": "manual-order"}
    orders: dict[str, dict[str, Any]] = {
        "b1": {"id": "b1", "client_order_id": "strategy-entry", "symbol": "MORNING",
               "side": "buy", "filled_qty": "5", "filled_avg_price": "101.2"},
        "manual-order": {"id": "manual-order", "client_order_id": "manual",
                         "symbol": "MORNING", "side": "buy"},
    }

    def page(cursor: str | None) -> tuple[list[dict[str, Any]], str | None]:
        return ([activity, manual], "p2") if cursor is None else ([activity, second], None)

    result = broker_fills.collect_broker_fills(
        plan=context, metadata=metadata, ledger_path=ledger_path, ledger_sha256=ledger_hash,
        read_fill_page=page, read_order=orders.__getitem__,
    )
    assert [item.quantity for item in result.fills] == [2, 3]
    assert all(item.fees is None for item in result.fills)
    assert result.costs_complete is False
    assert result.source_evidence["excluded_activity_ids"] == ["manual"]
    assert result.reconciled_complete is False  # Unknown ownership cannot prove no-trade.
    assert ledger_path.read_bytes() and hashlib.sha256(ledger_path.read_bytes()).hexdigest() == (
        ledger_hash
    )
    with pytest.raises(ValueError, match="cumulative"):
        broker_fills.collect_broker_fills(
            plan=context, metadata=metadata, ledger_path=ledger_path, ledger_sha256=ledger_hash,
            read_fill_page=lambda _: ([], None), read_order=orders.__getitem__,
        )
    with pytest.raises(ValueError, match="FILL"):
        broker_fills.collect_broker_fills(
            plan=context, metadata=metadata, ledger_path=ledger_path, ledger_sha256=ledger_hash,
            read_fill_page=lambda _: ([{"quantity_semantics": "broker_order_cumulative"}], None),
            read_order=orders.__getitem__,
        )
    orders["exit"] = {
        "id": "exit", "client_order_id": "broker-generated-leg", "symbol": "MORNING",
        "side": "sell", "filled_qty": "5", "filled_avg_price": "110",
    }
    orders["b1"]["legs"] = [orders["exit"]]
    exit_fill = {**activity, "id": "f3", "order_id": "exit", "side": "sell",
                 "qty": "5", "price": "110", "transaction_time": "2026-09-01T15:00:00Z"}
    result = broker_fills.collect_broker_fills(
        plan=context, metadata=metadata, ledger_path=ledger_path, ledger_sha256=ledger_hash,
        read_fill_page=lambda _: ([activity, second, exit_fill, manual], None),
        read_order=orders.__getitem__,
    )
    assert [item.fill_id for item in result.fills] == ["f1", "f2", "f3"]


def test_alpaca_page_token_is_last_activity_id_not_cumulative_quantity() -> None:
    from operations.loop_integration.broker_fills import alpaca_fill_page

    calls: list[dict[str, Any]] = []
    rows = [{"id": f"id-{i}", "qty": "1", "cum_qty": str(i + 1)} for i in range(100)]

    def get(path: str, params: dict[str, Any]) -> object:
        assert path == "/v2/account/activities/FILL"
        calls.append(params)
        return rows if "page_token" not in params else []

    first, token = alpaca_fill_page(get, TRADE_DATE, None)
    assert len(first) == 100 and token == "id-99"
    assert alpaca_fill_page(get, TRADE_DATE, token) == ([], None)
    assert calls[1]["page_token"] == "id-99"


@pytest.mark.parametrize("prepare", [False, True])
def test_paper_cli_saves_raw_audit_and_pinned_evidence_without_external_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    prepare: bool,
) -> None:
    import hashlib
    import json
    import sqlite3
    import sys

    import httpx
    from test_loop_execution import fills_payload

    from operations.paper_state import PaperStateStore
    from scripts import export_loop_broker_fills

    inputs = native_inputs(tmp_path)
    context = execution_summary.export_native_context(**inputs)
    context_path = tmp_path / "context.json"
    context_hash = pinned_json(context_path, context.model_dump(mode="json"))
    source = tmp_path / "state.sqlite3"
    PaperStateStore(source)
    ledger = tmp_path / "frozen.sqlite3"
    with sqlite3.connect(source) as origin, sqlite3.connect(ledger) as target:
        origin.backup(target)
        target.execute("PRAGMA journal_mode=DELETE")
    metadata = fills_payload(inputs["plan_sha256"], [])
    metadata.pop("fills")
    metadata["broker"] = "alpaca"
    metadata_path = tmp_path / "metadata.json"
    metadata_hash = pinned_json(metadata_path, metadata)
    urls: list[str] = []

    def transport(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        assert request.method == "GET"
        assert request.url.host == "paper-api.alpaca.markets"
        data: Any = {"id": "test-account", "currency": "USD", "equity": str(len(urls))} if (
            request.url.path == "/v2/account"
        ) else []
        return httpx.Response(200, json=data)

    client_type = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: client_type(
        **kw, transport=httpx.MockTransport(transport),
    ))
    monkeypatch.setenv("ALPACA_PAPER_KEY_ID", "fixture-key-not-secret")
    monkeypatch.setenv("ALPACA_PAPER_SECRET_KEY", "fixture-secret-not-secret")
    monkeypatch.setattr(sys, "argv", [
        "export_loop_broker_fills", "--plan", str(inputs["plan_path"]),
        "--plan-sha256", inputs["plan_sha256"], "--review-context", str(context_path),
        "--review-context-sha256", context_hash, "--ledger", str(ledger),
        "--ledger-sha256", hashlib.sha256(ledger.read_bytes()).hexdigest(),
        "--trade-date", str(TRADE_DATE), "--as-of", AS_OF.isoformat(),
        "--read-paper-broker", "--metadata", str(metadata_path),
        "--metadata-sha256", metadata_hash, "--output-dir", str(tmp_path / "export"),
    ])
    if prepare:
        from scripts import prepare_loop_execution

        config_path = tmp_path / "provider.json"
        config_hash = pinned_json(config_path, {
            "context": {key: value.isoformat() if isinstance(value, datetime) else str(value)
                        for key, value in inputs.items()},
            "ledger_path": str(ledger), "ledger_sha256": hashlib.sha256(
                ledger.read_bytes()
            ).hexdigest(),
            "metadata_path": str(metadata_path), "metadata_sha256": metadata_hash,
            "output_dir": str(tmp_path / "export"), "read_paper_broker": True,
        })
        receipt = prepare_loop_execution.run([
            "--config", str(config_path), "--config-sha256", config_hash,
            "--trade-date", str(TRADE_DATE),
        ])
        index = execution_summary.load_execution_index(
            Path(receipt["execution_index_path"]), receipt["execution_index_sha256"],
        )
        assert len(index) == 1 and index[0].strategy_sha256 == context.strategy_sha256
    else:
        export_loop_broker_fills.main()
        receipt = json.loads(capsys.readouterr().out)
    assert receipt["status"] == "prepared"
    for name in ("raw_audit", "fills"):
        raw = Path(receipt[f"{name}_path"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == receipt[f"{name}_sha256"]
        assert b"fixture-secret" not in raw
    assert len(urls) == 2
    if not prepare:
        repeated = export_loop_broker_fills.run()
        assert repeated["fills_sha256"] == receipt["fills_sha256"]
        assert repeated["raw_audit_sha256"] != receipt["raw_audit_sha256"]
