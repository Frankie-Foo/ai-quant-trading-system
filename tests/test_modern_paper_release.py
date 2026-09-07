"""Paper release checks: synthetic account snapshots, no credentials or network."""

import json
import os
import socket
import subprocess
import sys
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import NoReturn, Self

import httpx
import polars as pl
import pytest
from test_modern_paper_lifecycle import PaperExchange

from execution.alpaca_paper import BrokerOrder, FreshNbboQuote, PaperPosition
from operations import paper_release as release
from operations.autonomous_selection_handoff import create_open_confirmation
from operations.feishu_base import FeishuBaseEventClient
from operations.paper_state import PaperStateStore, StoredPaperOrder
from research.modern_momentum import modern_strategy_manifest
from scripts import monitor_modern_momentum_paper as paper
from scripts import run_modern_funnel_stage as stage


@pytest.fixture(autouse=True)
def no_external_io(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("release tests forbid credentials and network access")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(paper, "load_project_env", forbidden)


def test_owner_approved_paper_portfolio_release_accepts_200000() -> None:
    assert paper.validate_smoke_notional(200_000) == 200_000


@pytest.mark.parametrize("cap", [None, 0, -1, 200_000.01, float("nan"), float("inf")])
def test_release_requires_an_explicit_finite_cap_within_owner_limit(cap: float | None) -> None:
    with pytest.raises(ValueError):
        paper.validate_smoke_notional(cap)


def _position(symbol: str, value: str, qty: str = "100") -> PaperPosition:
    return PaperPosition(symbol=symbol, qty=qty, side="long", market_value=value)


def _buy(client: str, *, filled: str = "0", qty: int = 300) -> BrokerOrder:
    return BrokerOrder(
        id=client, client_order_id=client, symbol="PART", side="buy", qty=qty,
        filled_qty=filled, status="partially_filled" if filled != "0" else "new",
        type="limit", limit_price="100",
    )


@pytest.mark.parametrize("bound", [False, True])
def test_local_aborted_intent_releases_only_proven_unsubmitted_budget(bound: bool) -> None:
    intent = StoredPaperOrder(
        trade_date=date(2026, 9, 3), client_order_id="never-posted",
        broker_order_id="unexpected" if bound else None, symbol="FIRST", attempt=1,
        role="entry", quantity=1, status="aborted", payload={"type": "limit", "limit_price": "50"},
    )
    if bound:
        with pytest.raises(ValueError):
            release.remaining_entry_notional(
                cap=100, equity="100", positions=(), open_orders=(), pending_entries=(intent,),
            )
    else:
        assert release.remaining_entry_notional(
            cap=100, equity="100", positions=(), open_orders=(), pending_entries=(intent,),
        ) == Decimal("100")


def test_empty_account_uses_equity_not_leveraged_buying_power() -> None:
    assert release.remaining_entry_notional(
        cap=200_000, equity="100574.93", positions=(), open_orders=(),
    ) == Decimal("100574.93")


def test_all_positions_and_partial_buy_remainder_share_one_portfolio_cap() -> None:
    buy = _buy("partial", filled="100")
    positions = (
        _position("FIRST", "60000"), _position("SECOND", "30000"),
        _position("PART", "10000"),
    )
    assert release.remaining_entry_notional(
        cap=200_000, equity="150000", positions=positions, open_orders=(buy,),
    ) == Decimal("30000")


def test_partial_fill_missing_from_position_snapshot_still_consumes_budget() -> None:
    assert release.remaining_entry_notional(
        cap=200_000, equity="100574.93", positions=(),
        open_orders=(_buy("partial", filled="100"),),
    ) == Decimal("70574.93")


@pytest.mark.parametrize("visible", [False, True])
def test_persisted_entry_reserves_budget_without_double_counting_visible_order(
    visible: bool,
) -> None:
    intent = StoredPaperOrder(
        trade_date=date(2026, 9, 3), client_order_id="pending", broker_order_id="pending",
        symbol="PART", attempt=2, role="entry", quantity=300, status="new",
        payload={"side": "buy", "type": "limit", "limit_price": "100"},
    )
    assert release.remaining_entry_notional(
        cap=200_000, equity="100574.93", positions=(),
        open_orders=(_buy("pending"),) if visible else (), pending_entries=(intent,),
    ) == Decimal("70574.93")


def test_partial_reentry_and_deduplicated_bracket_do_not_release_budget_for_open_sells() -> None:
    stop = _buy("stop").model_copy(update={"side": "sell", "order_type": "stop"})
    buy = _buy("reentry", filled="100").model_copy(update={"legs": (stop,)})
    assert release.remaining_entry_notional(
        cap=40_000, equity="100574.93", positions=(_position("PART", "10000"),),
        open_orders=(buy, buy, stop),
    ) == Decimal("10000")


@pytest.mark.parametrize("updates", [
    {"limit_price": None}, {"limit_price": "NaN"}, {"filled_qty": "301"},
    {"filled_qty": "-1"}, {"order_type": "market"}, {"side": None},
])
def test_unpriced_or_invalid_active_buys_block_new_entries(updates: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        release.remaining_entry_notional(
            cap=200_000, equity="100574.93", positions=(),
            open_orders=(_buy("pending").model_copy(update=updates),),
        )


@pytest.mark.parametrize("hide_open_orders", [False, True])
@pytest.mark.parametrize("cap,expected_orders", [(100, 1), (200_000, 2)])
@pytest.mark.parametrize("budget_delay", [0, 3, -3, -1, -2, -4, -5, -6])
def test_real_monitor_does_not_grant_the_same_portfolio_budget_to_two_symbols(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hide_open_orders: bool,
    cap: int, expected_orders: int, budget_delay: int,
) -> None:
    day = date(2026, 9, 3)
    opened = datetime(2026, 9, 3, 13, 30, tzinfo=UTC)
    closed = opened + timedelta(minutes=390)

    class Clock(datetime):
        current = opened + timedelta(minutes=28)

        @classmethod
        def now(cls, tz: object = None) -> Self:
            return cls.fromtimestamp(cls.current.timestamp(), tz=UTC)

    class Push:
        def push(self, body: str) -> str:
            return "fixture-only-message"

        def close(self) -> None:
            pass

    class Exchange(PaperExchange):
        def handle(self, request: httpx.Request) -> httpx.Response:
            if budget_delay == -2 and request.method == "POST":
                raise httpx.ReadTimeout("fixture ambiguous POST", request=request)
            if request.url.path == "/v2/orders" and request.method == "GET":
                Clock.current += timedelta(seconds=max(0, budget_delay))
            if budget_delay in {-3, -1} and request.url.path == "/v2/orders:by_client_order_id":
                if budget_delay == -3 or Clock.current < opened + timedelta(minutes=29):
                    Clock.current += timedelta(seconds=3)
            if request.url.path == "/v2/account":
                if budget_delay == -4:
                    plan.write_text("{}")
                if budget_delay == -5:
                    confirmation.write_text("{}")
                return httpx.Response(200, json=dict(
                    status="ACTIVE", account_blocked=False, trading_blocked=False,
                    equity="100574.93", last_equity="100574.93", buying_power="400000",
                ))
            if hide_open_orders and request.url.path == "/v2/orders" and request.method == "GET":
                return httpx.Response(200, json=[])
            return super().handle(request)

    def sleep(_: float) -> None:
        Clock.current = (
            opened + timedelta(minutes=29)
            if budget_delay == -1 and Clock.current < opened + timedelta(minutes=29)
            else closed
        )

    rows = []
    for symbol in ("FIRST", "SECOND"):
        for minute in range(29 if budget_delay == -1 else 28):
            price = 99.4 + min(minute, 14) * 0.035
            if 15 <= minute < 26:
                price = 99.85
            if minute >= 26:
                price = 100.45 + (min(minute, 27) - 26) * 0.08
            rows.append(dict(
                symbol=symbol, ts_utc=opened + timedelta(minutes=minute),
                open=(price - 0.02) / 2, high=(price + 0.08) / 2,
                low=(price - 0.08) / 2, close=price / 2, vwap=price / 2, volume=10000,
            ))
    bars = pl.DataFrame(rows)
    pool = pl.DataFrame(dict(
        symbol=["FIRST", "SECOND"], price=[48.0, 48.0], forward_market_cap=[2e9, 2e9],
        rvol=[2.0, 2.0], hard_catalyst=[True, True], sector_symbol=["ONE", "TWO"],
    ))
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"modern_strategy_manifest": modern_strategy_manifest()}))
    confirmation = tmp_path / "confirmation.json"
    create_open_confirmation(
        trade_date=day, selection_snapshot_id="fixture-selection",
        candidate_pool=("FIRST", "SECOND"),
        feishu_record_ids=("fixture-base",), livermore_message_id="fixture-message",
        strategy_version=paper.STRATEGY_VERSION, config_path=plan,
        confirmation_path=confirmation, generated_at_utc=opened,
    )
    exchange = Exchange()
    exchange.quantity = 0
    broker = exchange.broker()
    monkeypatch.setattr(paper, "ROOT", tmp_path)
    monkeypatch.setattr(paper, "load_project_env", lambda _: None)
    monkeypatch.setattr(paper, "project_data_root", lambda _: tmp_path)
    monkeypatch.setattr(paper, "_latest_pool", lambda *_: pool)
    monkeypatch.setattr(paper, "build_xnys_schedule", lambda *_: pl.DataFrame({
        "market_open_utc": [opened], "market_close_utc": [closed],
    }))
    monkeypatch.setattr(paper, "_broker", lambda **_: broker)
    monkeypatch.setattr(paper, "_push_client", Push)
    monkeypatch.setattr(FeishuBaseEventClient, "from_environment", lambda: None)
    monkeypatch.setattr(paper, "fetch_bars", lambda *_: bars)
    monkeypatch.setattr(paper, "_latest_sip_nbbo_now", lambda symbol: FreshNbboQuote(
        symbol=symbol, bid=Decimal("50.26"), ask=Decimal("50.27"),
        asof_utc=Clock.current, feed="sip",
    ))
    monkeypatch.setattr(paper, "datetime", Clock)
    monkeypatch.setattr(time, "sleep", sleep)
    monkeypatch.setenv("BROKER_WRITE_ENABLED", "true")
    monkeypatch.setenv("TRADING_KILL_SWITCH", "false")
    monkeypatch.setenv("AI_QUANT_PAPER_SMOKE_MAX_NOTIONAL", str(cap))
    monkeypatch.setattr(sys, "argv", [
        "monitor", "--trade-date", str(day), "--arm-paper",
        "--confirmation-path", str(confirmation),
    ])
    if budget_delay == -6:
        def fail_sync(fd: int) -> NoReturn:
            raise OSError("fixture disk failure secret must not be reported")

        monkeypatch.setattr(os, "fsync", fail_sync)
    paper.main()
    store = PaperStateStore(
        tmp_path / "runs" / "modern-momentum" / str(day) / "paper-state.sqlite3"
    )
    if budget_delay in {-4, -5, -6}:
        assert exchange.submits == []
        snapshot = json.loads((store.path.parent / "paper-state.json").read_text())
        assert snapshot["startup_evidence_error"] is not None
        assert snapshot["candidate_blocks"]["FIRST"]["code"] == "startup_evidence_unavailable"
        assert not snapshot["entry_frozen"]
        assert "secret" not in json.dumps(snapshot)
        return
    if budget_delay == -2:
        assert store.load_symbol_states(day)["FIRST"]["phase"] == "entry_pending"
        assert {order.status for order in store.list_orders()} == {"intent"}
        return
    if budget_delay == -1:
        assert len(exchange.submits) == expected_orders
        assert all(order["client_order_id"].endswith("-r1") for order in exchange.submits)
        assert all(state["attempt"] == 1 for state in store.load_symbol_states(day).values())
        return
    assert len(list(store.path.parent.glob("startup-*.json"))) == 1
    if budget_delay:
        assert exchange.submits == []
        if budget_delay < 0:
            store = PaperStateStore(
                tmp_path / "runs" / "modern-momentum" / str(day) / "paper-state.sqlite3"
            )
            assert store.load_symbol_states(day) == {}
            assert {order.status for order in store.list_orders()} == {"aborted"}
            snapshot = json.loads((
                tmp_path / "runs" / "modern-momentum" / str(day) / "paper-state.json"
            ).read_text(encoding="utf-8"))
            assert snapshot["attempts"] == {}
            assert "last_error_type" not in snapshot
        if budget_delay > 0:
            snapshot = json.loads((
                tmp_path / "runs" / "modern-momentum" / str(day) / "paper-state.json"
            ).read_text(encoding="utf-8"))
            assert "stale" in snapshot["candidate_blocks"]["FIRST"]["reason"]
        return
    assert len(exchange.submits) == expected_orders
    assert exchange.submits[0]["symbol"] == "FIRST"
    total = sum(Decimal(o["qty"]) * Decimal(o["limit_price"]) for o in exchange.submits)
    assert total <= min(Decimal(cap), Decimal("100574.93"))
    for order in exchange.submits:
        limit = Decimal(order["limit_price"])
        risk = limit - Decimal(order["stop_loss"]["stop_price"]) + limit * Decimal("0.005")
        assert Decimal(order["qty"]) * risk <= Decimal("100574.93") * Decimal("0.003")
        assert risk / limit <= Decimal("0.02")


def test_launcher_uses_the_same_200000_release_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_modern_funnel_stage import _authorization, _clock, _process_identity

    day = date(2026, 9, 3)
    now = datetime(2026, 9, 3, 14, 0, tzinfo=UTC)
    confirmation = _authorization(tmp_path, day)
    store = PaperStateStore(
        tmp_path / "runs" / "modern-momentum" / str(day) / "paper-state.sqlite3"
    )
    assert store.claim_run(day, owner="pid-456", observed_at_utc=now)
    monkeypatch.setattr(stage, "ROOT", tmp_path)
    monkeypatch.setenv("AI_QUANT_PAPER_RUNTIME_CONFIRMED", "true")
    monkeypatch.setenv("AI_QUANT_PAPER_SMOKE_MAX_NOTIONAL", "200000")
    _clock(monkeypatch, now)
    monkeypatch.setattr(stage, "_process_running", lambda _: True)
    _process_identity(monkeypatch, day, confirmation)
    assert stage._launch_paper_if_confirmed(day, confirmation) == 456
    monkeypatch.delenv("AI_QUANT_PAPER_RUNTIME_CONFIRMED")
    assert stage._launch_paper_if_confirmed(day, confirmation) is None


@pytest.mark.parametrize("name", [
    "install_local_observation_tasks.ps1", "run_modern_funnel_tick.ps1",
])
def test_powershell_release_contract_accepts_cap_without_implicitly_arming(name: str) -> None:
    path = Path(__file__).parents[1] / "scripts" / name
    source = path.read_text(encoding="utf-8")
    assert "-m operations.paper_release --validate-cap" in source
    if name.startswith("run_"):
        assert '--first-wave-not-before-beijing "21:00"' in source
    # Execute only the parsed parameter block, never the installer/tick body.
    if sys.platform != "win32":
        pytest.skip("PowerShell parameter binding requires Windows")
    arguments = (
        "-StrategyPolicyApprovedBy fixture" if name.startswith("install")
        else "-ActivePolicyFile fixture -ChallengerPolicyFile fixture"
    )
    command = (
        "$tokens=$null; $errors=$null; "
        f"$ast=[System.Management.Automation.Language.Parser]::ParseFile('{path}',"
        "[ref]$tokens,[ref]$errors); if ($errors.Count) {exit 1}; "
        "$probe=[scriptblock]::Create($ast.ParamBlock.Extent.Text + "
        "' ; if ($ArmPaper) {throw \"unexpected arming\"}; Write-Output $PaperSmokeMaxNotional'); "
        "& $probe -PythonPath fixture -EnvironmentFile fixture -DataRoot fixture "
        f"{arguments} -PaperSmokeMaxNotional 200000"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True, text=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "200000"


@pytest.mark.parametrize("cap,allowed", [("200000", True), ("200000.01", False), ("NaN", False)])
def test_powershell_shared_cli_validator(cap: str, allowed: bool) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "operations.paper_release", "--validate-cap", cap],
        cwd=Path(__file__).parents[1], capture_output=True, text=True, timeout=20, check=False,
    )
    assert (result.returncode == 0) is allowed
    if allowed:
        assert result.stdout.strip() == "200000"


def test_narrow_stop_cannot_turn_leveraged_buying_power_into_extra_equity() -> None:
    assert paper.position_size(
        entry_price=100, all_in_stop_pct=0.0001, equity=100574.93,
        buying_power=400000, risk_fraction=0.003, remaining_slots=1,
    ) == 1005
    assert paper.risk_fraction(hard_catalyst=True) == 0.005
    assert paper.attempt_risk_fraction(0.005, attempt=1) == 0.003
    assert paper.attempt_risk_fraction(0.005, attempt=2) == 0.002


def test_over_cap_positions_cannot_be_offset_by_pending_sells() -> None:
    sell = _buy("exit").model_copy(update={"side": "sell"})
    assert release.remaining_entry_notional(
        cap=200_000, equity="100574.93", positions=(_position("OLD", "100575"),),
        open_orders=(sell,),
    ) == 0


def test_conflicting_duplicate_order_types_cannot_hide_an_unbounded_buy() -> None:
    limit = _buy("same")
    market = limit.model_copy(update={"order_type": "market"})
    with pytest.raises(ValueError):
        release.remaining_entry_notional(
            cap=200_000, equity="100574.93", positions=(), open_orders=(market, limit),
        )
