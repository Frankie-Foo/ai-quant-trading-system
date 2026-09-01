from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from execution.alpaca_paper import (
    BrokerOrder,
    PaperAccount,
    PaperCloseRequest,
    PaperExtendedLimitRequest,
    PaperOrderRequest,
    PaperPosition,
    PaperStopRequest,
)
from execution.autonomous_paper_session import (
    AutonomousPaperPlan,
    PaperSessionLedger,
    PaperSessionOrchestrator,
    PaperSessionSnapshot,
    SessionAction,
)
from kernel.intraday_policy import (
    DecisionMetric,
    EntryRoute,
    PolicySnapshot,
)

NOW = datetime(2026, 7, 29, 14, 0, tzinfo=UTC)
TRADE_DATE = date(2026, 7, 29)


def _metric(value: float, observed_at: datetime) -> DecisionMetric:
    return DecisionMetric(value, observed_at, "test.metric.v1")


def test_ledger_aggregates_plan_evaluations_without_storing_market_ticks(
    tmp_path: Path,
) -> None:
    ledger = PaperSessionLedger(tmp_path / "paper.sqlite3")
    plan = AutonomousPaperPlan(
        plan_id="plan-test",
        symbol="TEST",
        trade_date=TRADE_DATE,
        reference_price=Decimal("100"),
        hard_stop=Decimal("98"),
        max_notional_fraction=Decimal("0.1"),
        full_risk_fraction=Decimal("0.001"),
        source_snapshot_ids=("selection-test",),
        provenance="test.plan.v1",
    )

    ledger.record_plan_evaluation(
        plan,
        action=SessionAction.DATA_BLOCKED,
        reasons=("market_facts_unavailable",),
        degraded_reasons=("market_facts_unavailable",),
        submitted_order_ids=(),
        at_utc=NOW,
    )
    ledger.record_plan_evaluation(
        plan,
        action=SessionAction.OBSERVE,
        reasons=("waiting_for_trigger",),
        degraded_reasons=(),
        submitted_order_ids=(),
        at_utc=NOW + timedelta(seconds=1),
    )

    summary = ledger.plan_evaluation_summaries(TRADE_DATE)[0]
    assert summary.evaluation_count == 2
    assert summary.data_blocked_count == 1
    assert summary.observe_count == 1
    assert summary.submitted_order_count == 0
    assert summary.last_reasons == ("waiting_for_trigger",)


def _policy_snapshot(
    *,
    has_position: bool = True,
    agents_healthy: bool = True,
    observed_at: datetime = NOW,
    position_fraction: float | None = None,
    main_profit_realized: bool = False,
    right_tail_value: float = 75,
) -> PolicySnapshot:
    effective_fraction = (
        position_fraction
        if position_fraction is not None
        else 0.25
        if has_position
        else 0.0
    )
    return PolicySnapshot(
        trade_date=TRADE_DATE,
        observed_at_utc=observed_at,
        route=EntryRoute.CATALYST,
        catalyst=_metric(90, observed_at),
        factor=_metric(70, observed_at),
        order_flow=_metric(80, observed_at),
        execution=_metric(85, observed_at),
        right_tail=_metric(right_tail_value, observed_at),
        technical_structure_valid=True,
        negative_news_clear=True,
        material_negative=False,
        data_healthy=True,
        agents_healthy=agents_healthy,
        push_healthy=True,
        has_position=has_position,
        position_fraction=effective_fraction,
        average_entry_price=100.0 if has_position else None,
        last_price=102.0,
        main_profit_realized=main_profit_realized,
        first_target_reward_r=2.5,
        weighted_expected_reward_r=3.0,
        reward_risk_provenance="test.reward-risk.v1",
    )


def _plan() -> AutonomousPaperPlan:
    return AutonomousPaperPlan(
        plan_id="auto-20260729-AAPL",
        symbol="AAPL",
        trade_date=TRADE_DATE,
        reference_price=Decimal("102.01"),
        hard_stop=Decimal("98.00"),
        max_notional_fraction=Decimal("0.20"),
        full_risk_fraction=Decimal("0.0035"),
        source_snapshot_ids=("selection-20260729",),
        provenance="test.autonomous-plan.v1",
    )


class FakeAutonomousBroker:
    writes_enabled = True

    def __init__(self) -> None:
        self.account = PaperAccount(
            status="ACTIVE",
            account_blocked=False,
            trading_blocked=False,
            equity="97999",
            last_equity="100000",
            buying_power="200000",
        )
        self.positions: tuple[PaperPosition, ...] = (
            PaperPosition(
                symbol="AAPL",
                qty="10",
                side="long",
                market_value="1020",
                avg_entry_price="100",
                current_price="102",
            ),
        )
        self.orders: tuple[BrokerOrder, ...] = (
            BrokerOrder(
                id="protective-1",
                client_order_id="tsv2-owned-protection",
                symbol="AAPL",
                qty=10,
                filled_qty="0",
                status="new",
            ),
        )
        self.cancelled: list[str] = []
        self.close_requests: list[PaperCloseRequest] = []
        self.close_orders: dict[str, BrokerOrder] = {}
        self.entry_requests: list[PaperOrderRequest] = []
        self.entry_orders: dict[str, BrokerOrder] = {}
        self.extended_requests: list[PaperExtendedLimitRequest] = []
        self.stop_requests: list[PaperStopRequest] = []

    def get_account(self) -> PaperAccount:
        return self.account

    def list_positions(self) -> tuple[PaperPosition, ...]:
        return self.positions

    def list_open_orders(self) -> tuple[BrokerOrder, ...]:
        return self.orders + tuple(self.entry_orders.values())

    def cancel_order(self, order_id: str) -> bool:
        self.cancelled.append(order_id)
        return True

    def get_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        return self.entry_orders.get(client_order_id) or self.close_orders.get(
            client_order_id
        )

    def submit_close_order_idempotent(
        self, request: PaperCloseRequest
    ) -> BrokerOrder:
        existing = self.close_orders.get(request.client_order_id)
        if existing is not None:
            return existing
        self.close_requests.append(request)
        order = BrokerOrder(
            id="hard-loss-flat-1",
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            qty=request.qty,
            filled_qty="0",
            status="new",
        )
        self.close_orders[request.client_order_id] = order
        return order

    def submit_order_idempotent(self, request: PaperOrderRequest) -> BrokerOrder:
        existing = self.entry_orders.get(request.client_order_id)
        if existing is not None:
            return existing
        self.entry_requests.append(request)
        order = BrokerOrder(
            id=f"probe-entry-{len(self.entry_requests)}",
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            qty=request.qty,
            filled_qty="0",
            status="new",
        )
        self.entry_orders[request.client_order_id] = order
        return order

    def submit_extended_limit_idempotent(
        self, request: PaperExtendedLimitRequest
    ) -> BrokerOrder:
        existing = self.entry_orders.get(request.client_order_id)
        if existing is not None:
            return existing
        self.extended_requests.append(request)
        order = BrokerOrder(
            id=f"premarket-entry-{len(self.extended_requests)}",
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            qty=request.qty,
            filled_qty="0",
            status="new",
        )
        self.entry_orders[request.client_order_id] = order
        return order

    def submit_stop_order_idempotent(
        self,
        request: PaperStopRequest,
    ) -> BrokerOrder:
        existing = self.entry_orders.get(request.client_order_id)
        if existing is not None:
            return existing
        self.stop_requests.append(request)
        order = BrokerOrder(
            id=f"protective-stop-{len(self.stop_requests)}",
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            qty=request.qty,
            filled_qty="0",
            status="new",
        )
        self.entry_orders[request.client_order_id] = order
        return order


def test_paper_session_ledger_keeps_a_hash_chained_audit_trail_without_secrets(
    tmp_path: Path,
) -> None:
    ledger = PaperSessionLedger(tmp_path / "autonomous-paper.sqlite3")

    first = ledger.record_audit_event(
        run_id="paper-run-20260729-1",
        event_type="tick_open",
        at_utc=NOW,
        payload={"symbol": "AAPL", "source_snapshot_ids": ["selection-20260729"]},
    )
    second = ledger.record_audit_event(
        run_id="paper-run-20260729-1",
        event_type="tick_result",
        at_utc=NOW + timedelta(seconds=1),
        payload={"action": "observe", "order_ids": []},
    )

    events = ledger.audit_events(run_id="paper-run-20260729-1")

    assert [event["sequence"] for event in events] == [first["sequence"], second["sequence"]]
    assert events[0]["payload"] == {
        "source_snapshot_ids": ["selection-20260729"],
        "symbol": "AAPL",
    }
    assert events[1]["previous_hash"] == events[0]["event_hash"]
    with pytest.raises(ValueError, match="secret"):
        ledger.record_audit_event(
            run_id="paper-run-20260729-1",
            event_type="unsafe",
            at_utc=NOW,
            payload={"api_key": "must-not-persist"},
        )


def _orchestrator(
    tmp_path: Path,
    broker: FakeAutonomousBroker,
) -> PaperSessionOrchestrator:
    return PaperSessionOrchestrator(
        broker=broker,
        ledger=PaperSessionLedger(tmp_path / "autonomous-paper.sqlite3"),
        paper_authorized=True,
        owned_symbols=frozenset({"AAPL"}),
    )


def test_daily_hard_loss_flattens_and_persistently_locks_the_day(
    tmp_path: Path,
) -> None:
    broker = FakeAutonomousBroker()
    ledger_path = tmp_path / "autonomous-paper.sqlite3"

    first = _orchestrator(tmp_path, broker).tick(
        _plan(),
        PaperSessionSnapshot(
            policy=_policy_snapshot(),
            bid=Decimal("101.99"),
            ask=Decimal("102.01"),
            quote_asof_utc=NOW,
            quote_provenance="alpaca.sip.nbbo",
        ),
    )
    replay = PaperSessionOrchestrator(
        broker=broker,
        ledger=PaperSessionLedger(ledger_path),
        paper_authorized=True,
        owned_symbols=frozenset({"AAPL"}),
    ).tick(
        _plan(),
        PaperSessionSnapshot(
            policy=_policy_snapshot(),
            bid=Decimal("101.99"),
            ask=Decimal("102.01"),
            quote_asof_utc=NOW,
            quote_provenance="alpaca.sip.nbbo",
        ),
    )

    assert first.action is SessionAction.HARD_LOSS_FLATTEN
    assert first.day_locked is True
    assert first.new_entries_allowed is False
    assert first.daily_return == Decimal("-0.02001")
    assert broker.cancelled == ["protective-1"]
    assert [(item.symbol, item.qty) for item in broker.close_requests] == [
        ("AAPL", 10)
    ]
    assert replay.action is SessionAction.HARD_LOSS_FLATTEN
    assert len(broker.close_requests) == 1


def test_daily_soft_loss_blocks_a_valid_probe_without_broker_writes(
    tmp_path: Path,
) -> None:
    broker = FakeAutonomousBroker()
    broker.account = broker.account.model_copy(
        update={"equity": "98400", "last_equity": "100000"}
    )
    broker.positions = ()
    broker.orders = ()

    result = _orchestrator(tmp_path, broker).tick(
        _plan(),
        PaperSessionSnapshot(
            policy=_policy_snapshot(has_position=False),
            bid=Decimal("101.99"),
            ask=Decimal("102.01"),
            quote_asof_utc=NOW,
            quote_provenance="alpaca.sip.nbbo",
        ),
    )

    assert result.decision is not None
    assert result.decision.action.value == "enter_probe"
    assert result.action is SessionAction.SOFT_LOSS_BLOCK
    assert result.daily_return == Decimal("-0.016")
    assert result.new_entries_allowed is False
    assert broker.cancelled == []
    assert broker.close_requests == []


def test_agent_failure_exits_once_and_replays_without_duplicate_order(
    tmp_path: Path,
) -> None:
    broker = FakeAutonomousBroker()
    broker.account = broker.account.model_copy(
        update={"equity": "100000", "last_equity": "100000"}
    )
    ledger_path = tmp_path / "autonomous-paper.sqlite3"
    snapshot = PaperSessionSnapshot(
        policy=_policy_snapshot(agents_healthy=False),
        bid=Decimal("101.99"),
        ask=Decimal("102.01"),
        quote_asof_utc=NOW,
        quote_provenance="alpaca.sip.nbbo",
    )

    first = _orchestrator(tmp_path, broker).tick(_plan(), snapshot)
    replay = PaperSessionOrchestrator(
        broker=broker,
        ledger=PaperSessionLedger(ledger_path),
        paper_authorized=True,
        owned_symbols=frozenset({"AAPL"}),
    ).tick(_plan(), snapshot)

    assert first.decision is not None
    assert first.decision.action.value == "exit"
    assert first.action is SessionAction.EXIT_SUBMITTED
    assert first.reasons == ("required_agent_unhealthy",)
    assert broker.cancelled == ["protective-1"]
    assert [(item.symbol, item.qty) for item in broker.close_requests] == [
        ("AAPL", 10)
    ]
    assert replay.action is SessionAction.EXIT_SUBMITTED
    assert len(broker.close_requests) == 1


def test_unknown_manual_order_invokes_account_guardian_before_policy(
    tmp_path: Path,
) -> None:
    broker = FakeAutonomousBroker()
    broker.account = broker.account.model_copy(
        update={"equity": "100000", "last_equity": "100000"}
    )
    broker.orders = broker.orders + (
        BrokerOrder(
            id="manual-1",
            client_order_id="mobile-order",
            symbol="MSFT",
            qty=1,
            filled_qty="0",
            status="new",
        ),
    )

    result = _orchestrator(tmp_path, broker).tick(
        _plan(),
        PaperSessionSnapshot(
            policy=_policy_snapshot(),
            bid=Decimal("101.99"),
            ask=Decimal("102.01"),
            quote_asof_utc=NOW,
            quote_provenance="alpaca.sip.nbbo",
        ),
    )

    assert result.action is SessionAction.ACCOUNT_GUARDIAN_LOCK
    assert result.day_locked is True
    assert result.new_entries_allowed is False
    assert result.reasons == ("unknown_order_detected",)
    assert set(broker.cancelled) == {"protective-1", "manual-1"}
    assert [(item.symbol, item.qty) for item in broker.close_requests] == [
        ("AAPL", 10)
    ]


def test_regular_hours_probe_submits_one_risk_sized_bracket_order(
    tmp_path: Path,
) -> None:
    broker = FakeAutonomousBroker()
    broker.account = broker.account.model_copy(
        update={"equity": "100000", "last_equity": "100000"}
    )
    broker.positions = ()
    broker.orders = ()
    ledger_path = tmp_path / "autonomous-paper.sqlite3"
    snapshot = PaperSessionSnapshot(
        policy=_policy_snapshot(has_position=False),
        bid=Decimal("101.99"),
        ask=Decimal("102.01"),
        quote_asof_utc=NOW,
        quote_provenance="alpaca.sip.nbbo",
    )

    first = _orchestrator(tmp_path, broker).tick(_plan(), snapshot)
    replay = PaperSessionOrchestrator(
        broker=broker,
        ledger=PaperSessionLedger(ledger_path),
        paper_authorized=True,
        owned_symbols=frozenset({"AAPL"}),
    ).tick(_plan(), snapshot)

    assert first.action is SessionAction.ENTRY_SUBMITTED
    assert first.decision is not None
    assert first.decision.action.value == "enter_probe"
    assert first.submitted_order_ids == ("probe-entry-1",)
    assert len(broker.entry_requests) == 1
    request = broker.entry_requests[0]
    assert request.symbol == "AAPL"
    assert request.qty == 21
    assert request.side == "buy"
    assert request.order_type == "market"
    assert request.extended_hours is False
    assert request.stop_loss_price == "98.00"
    assert request.take_profit_price is None
    assert request.broker_payload()["order_class"] == "oto"
    assert replay.action is SessionAction.ENTRY_SUBMITTED
    assert len(broker.entry_requests) == 1


def test_regular_probe_splits_standard_tail_from_first_target_quantity(
    tmp_path: Path,
) -> None:
    broker = FakeAutonomousBroker()
    broker.account = broker.account.model_copy(
        update={"equity": "100000", "last_equity": "100000"}
    )
    broker.positions = ()
    broker.orders = ()

    result = _orchestrator(tmp_path, broker).tick(
        _plan(),
        PaperSessionSnapshot(
            policy=_policy_snapshot(
                has_position=False,
                right_tail_value=65,
            ),
            bid=Decimal("101.99"),
            ask=Decimal("102.01"),
            quote_asof_utc=NOW,
            quote_provenance="alpaca.sip.nbbo",
        ),
    )

    assert result.action is SessionAction.ENTRY_SUBMITTED
    assert result.submitted_order_ids == ("probe-entry-1", "probe-entry-2")
    assert [(request.qty, request.take_profit_price) for request in broker.entry_requests] == [
        (17, None),
        (4, "112.04"),
    ]


def test_main_target_fill_is_inferred_from_broker_position_and_persisted(
    tmp_path: Path,
) -> None:
    broker = FakeAutonomousBroker()
    broker.account = broker.account.model_copy(
        update={"equity": "100000", "last_equity": "100000"}
    )
    broker.positions = ()
    broker.orders = ()
    orchestrator = _orchestrator(tmp_path, broker)
    flat_snapshot = PaperSessionSnapshot(
        policy=_policy_snapshot(
            has_position=False,
            right_tail_value=65,
        ),
        bid=Decimal("101.99"),
        ask=Decimal("102.01"),
        quote_asof_utc=NOW,
        quote_provenance="alpaca.sip.nbbo",
    )
    orchestrator.tick(_plan(), flat_snapshot)
    broker.entry_orders = {
        key: value.model_copy(
            update={"filled_qty": str(value.qty), "status": "filled"}
        )
        for key, value in broker.entry_orders.items()
    }
    broker.positions = (
        PaperPosition(
            symbol="AAPL",
            qty="17",
            side="long",
            market_value="1734",
            avg_entry_price="102.01",
            current_price="112.10",
        ),
    )
    later = NOW + timedelta(seconds=15)
    tail_snapshot = PaperSessionSnapshot(
        policy=_policy_snapshot(
            has_position=True,
            observed_at=later,
            position_fraction=0.20,
            right_tail_value=65,
        ),
        bid=Decimal("112.09"),
        ask=Decimal("112.11"),
        quote_asof_utc=later,
        quote_provenance="alpaca.sip.nbbo",
    )

    first = orchestrator.tick(_plan(), tail_snapshot)
    replay = PaperSessionOrchestrator(
        broker=broker,
        ledger=PaperSessionLedger(tmp_path / "autonomous-paper.sqlite3"),
        paper_authorized=True,
        owned_symbols=frozenset({"AAPL"}),
    ).tick(_plan(), tail_snapshot)

    assert first.decision is not None
    assert first.decision.action.value == "hold"
    assert first.decision.reasons == ("tail_trend_intact",)
    assert first.decision.target_position_fraction == 0.20
    assert replay.decision == first.decision
    assert broker.close_requests == []


def test_tail_order_flow_duration_is_persistent_idempotent_and_gap_safe(
    tmp_path: Path,
) -> None:
    ledger = PaperSessionLedger(tmp_path / "autonomous-paper.sqlite3")

    first = ledger.advance_tail_runtime(
        "auto-20260729-AAPL",
        observed_at_utc=NOW,
        current_r=3.0,
        order_flow_score=40.0,
    )
    latest = first
    for index in range(1, 21):
        latest = ledger.advance_tail_runtime(
            "auto-20260729-AAPL",
            observed_at_utc=NOW + timedelta(seconds=15 * index),
            current_r=3.0 + (index / 100),
            order_flow_score=40.0,
        )
    replay = ledger.advance_tail_runtime(
        "auto-20260729-AAPL",
        observed_at_utc=NOW + timedelta(seconds=300),
        current_r=3.2,
        order_flow_score=40.0,
    )
    after_gap = ledger.advance_tail_runtime(
        "auto-20260729-AAPL",
        observed_at_utc=NOW + timedelta(seconds=345),
        current_r=3.1,
        order_flow_score=40.0,
    )

    assert first.order_flow_below_45_seconds == 0
    assert latest.order_flow_below_45_seconds == 300
    assert latest.maximum_favorable_excursion_r == 3.2
    assert replay.order_flow_below_45_seconds == 300
    assert after_gap.order_flow_below_45_seconds == 0
    assert after_gap.maximum_favorable_excursion_r == 3.2


def test_premarket_probe_uses_one_idempotent_extended_hours_limit(
    tmp_path: Path,
) -> None:
    premarket = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
    broker = FakeAutonomousBroker()
    broker.account = broker.account.model_copy(
        update={"equity": "100000", "last_equity": "100000"}
    )
    broker.positions = ()
    broker.orders = ()
    ledger_path = tmp_path / "autonomous-paper.sqlite3"
    snapshot = PaperSessionSnapshot(
        policy=_policy_snapshot(has_position=False, observed_at=premarket),
        bid=Decimal("101.99"),
        ask=Decimal("102.01"),
        quote_asof_utc=premarket,
        quote_provenance="alpaca.sip.nbbo",
    )

    first = _orchestrator(tmp_path, broker).tick(_plan(), snapshot)
    replay = PaperSessionOrchestrator(
        broker=broker,
        ledger=PaperSessionLedger(ledger_path),
        paper_authorized=True,
        owned_symbols=frozenset({"AAPL"}),
    ).tick(_plan(), snapshot)

    assert first.action is SessionAction.ENTRY_SUBMITTED
    assert first.submitted_order_ids == ("premarket-entry-1",)
    assert len(broker.extended_requests) == 1
    request = broker.extended_requests[0]
    assert request.symbol == "AAPL"
    assert request.qty == 21
    assert request.side == "buy"
    assert request.order_type == "limit"
    assert request.extended_hours is True
    assert request.limit_price == "102.01"
    assert replay.action is SessionAction.ENTRY_SUBMITTED
    assert len(broker.extended_requests) == 1


def test_premarket_working_order_reprices_after_three_seconds(
    tmp_path: Path,
) -> None:
    premarket = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
    broker = FakeAutonomousBroker()
    broker.account = broker.account.model_copy(
        update={"equity": "100000", "last_equity": "100000"}
    )
    broker.positions = ()
    broker.orders = ()
    orchestrator = _orchestrator(tmp_path, broker)

    orchestrator.tick(
        _plan(),
        PaperSessionSnapshot(
            policy=_policy_snapshot(has_position=False, observed_at=premarket),
            bid=Decimal("101.99"),
            ask=Decimal("102.01"),
            quote_asof_utc=premarket,
            quote_provenance="alpaca.sip.nbbo",
        ),
    )
    second_at = premarket + timedelta(seconds=4)
    second = orchestrator.tick(
        _plan(),
        PaperSessionSnapshot(
            policy=_policy_snapshot(has_position=False, observed_at=second_at),
            bid=Decimal("102.18"),
            ask=Decimal("102.20"),
            quote_asof_utc=second_at,
            quote_provenance="alpaca.sip.nbbo",
        ),
    )

    assert second.action is SessionAction.ENTRY_SUBMITTED
    assert broker.cancelled == ["premarket-entry-1"]
    assert len(broker.extended_requests) == 2
    assert broker.extended_requests[-1].qty == 21
    assert broker.extended_requests[-1].limit_price == "102.20"
    assert (
        broker.extended_requests[0].client_order_id
        != broker.extended_requests[1].client_order_id
    )


def test_premarket_unfilled_remainder_is_cancelled_after_ten_seconds(
    tmp_path: Path,
) -> None:
    premarket = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
    broker = FakeAutonomousBroker()
    broker.account = broker.account.model_copy(
        update={"equity": "100000", "last_equity": "100000"}
    )
    broker.positions = ()
    broker.orders = ()
    orchestrator = _orchestrator(tmp_path, broker)

    orchestrator.tick(
        _plan(),
        PaperSessionSnapshot(
            policy=_policy_snapshot(has_position=False, observed_at=premarket),
            bid=Decimal("101.99"),
            ask=Decimal("102.01"),
            quote_asof_utc=premarket,
            quote_provenance="alpaca.sip.nbbo",
        ),
    )
    expired_at = premarket + timedelta(seconds=11)
    expired = orchestrator.tick(
        _plan(),
        PaperSessionSnapshot(
            policy=_policy_snapshot(has_position=False, observed_at=expired_at),
            bid=Decimal("102.00"),
            ask=Decimal("102.02"),
            quote_asof_utc=expired_at,
            quote_provenance="alpaca.sip.nbbo",
        ),
    )

    assert expired.action is SessionAction.OBSERVE
    assert broker.cancelled == ["premarket-entry-1"]
    assert len(broker.extended_requests) == 1


def test_premarket_position_gets_regular_hours_protective_stop_before_upgrade(
    tmp_path: Path,
) -> None:
    premarket = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
    broker = FakeAutonomousBroker()
    broker.account = broker.account.model_copy(
        update={"equity": "100000", "last_equity": "100000"}
    )
    broker.positions = ()
    broker.orders = ()
    orchestrator = _orchestrator(tmp_path, broker)
    orchestrator.tick(
        _plan(),
        PaperSessionSnapshot(
            policy=_policy_snapshot(has_position=False, observed_at=premarket),
            bid=Decimal("101.99"),
            ask=Decimal("102.01"),
            quote_asof_utc=premarket,
            quote_provenance="alpaca.sip.nbbo",
        ),
    )
    broker.positions = (
        PaperPosition(
            symbol="AAPL",
            qty="21",
            side="long",
            market_value="2142",
            avg_entry_price="102.01",
            current_price="102.00",
        ),
    )
    regular = datetime(2026, 7, 29, 13, 31, tzinfo=UTC)
    snapshot = PaperSessionSnapshot(
        policy=_policy_snapshot(has_position=True, observed_at=regular),
        bid=Decimal("101.99"),
        ask=Decimal("102.01"),
        quote_asof_utc=regular,
        quote_provenance="alpaca.sip.nbbo",
    )

    first = orchestrator.tick(_plan(), snapshot)
    replay = orchestrator.tick(_plan(), snapshot)

    assert first.action is SessionAction.PROTECTION_SUBMITTED
    assert first.submitted_order_ids == ("protective-stop-1",)
    assert len(broker.stop_requests) == 1
    assert broker.stop_requests[0].qty == 21
    assert broker.stop_requests[0].stop_price == "98.00"
    assert replay.action is SessionAction.ENTRY_SUBMITTED
    assert len(broker.stop_requests) == 1


def test_premarket_position_below_stop_submits_synthetic_exit_limit(
    tmp_path: Path,
) -> None:
    premarket = datetime(2026, 7, 29, 12, 5, tzinfo=UTC)
    broker = FakeAutonomousBroker()
    broker.account = broker.account.model_copy(
        update={"equity": "100000", "last_equity": "100000"}
    )
    broker.positions = (
        PaperPosition(
            symbol="AAPL",
            qty="21",
            side="long",
            market_value="2055.90",
            avg_entry_price="102.01",
            current_price="97.90",
        ),
    )
    broker.orders = ()

    result = _orchestrator(tmp_path, broker).tick(
        _plan(),
        PaperSessionSnapshot(
            policy=_policy_snapshot(has_position=True, observed_at=premarket),
            bid=Decimal("97.90"),
            ask=Decimal("97.92"),
            quote_asof_utc=premarket,
            quote_provenance="alpaca.sip.nbbo",
            last_trade=Decimal("97.91"),
            last_trade_asof_utc=premarket,
            trade_provenance="alpaca.sip.trade",
        ),
    )

    assert result.action is SessionAction.STOP_EXIT_SUBMITTED
    assert len(broker.extended_requests) == 1
    request = broker.extended_requests[0]
    assert request.side == "sell"
    assert request.qty == 21
    assert request.limit_price == "97.65"
    assert result.submitted_order_ids == ("premarket-entry-1",)


def test_premarket_agent_failure_uses_extended_sell_not_market_close(
    tmp_path: Path,
) -> None:
    premarket = datetime(2026, 7, 29, 12, 5, tzinfo=UTC)
    broker = FakeAutonomousBroker()
    broker.account = broker.account.model_copy(
        update={"equity": "100000", "last_equity": "100000"}
    )
    broker.positions = (
        PaperPosition(
            symbol="AAPL",
            qty="21",
            side="long",
            market_value="2142",
            avg_entry_price="102.01",
            current_price="102.00",
        ),
    )
    broker.orders = ()

    result = _orchestrator(tmp_path, broker).tick(
        _plan(),
        PaperSessionSnapshot(
            policy=_policy_snapshot(
                has_position=True,
                agents_healthy=False,
                observed_at=premarket,
            ),
            bid=Decimal("101.99"),
            ask=Decimal("102.01"),
            quote_asof_utc=premarket,
            quote_provenance="alpaca.sip.nbbo",
            last_trade=Decimal("102.00"),
            last_trade_asof_utc=premarket,
            trade_provenance="alpaca.sip.trade",
        ),
    )

    assert result.action is SessionAction.EXIT_SUBMITTED
    assert broker.close_requests == []
    assert len(broker.extended_requests) == 1
    request = broker.extended_requests[0]
    assert request.side == "sell"
    assert request.qty == 21
    assert request.limit_price == "101.73"


def test_opening_confirmation_upgrades_only_to_half_position(
    tmp_path: Path,
) -> None:
    regular = datetime(2026, 7, 29, 13, 40, tzinfo=UTC)
    broker = FakeAutonomousBroker()
    broker.account = broker.account.model_copy(
        update={"equity": "100000", "last_equity": "100000"}
    )
    broker.positions = (
        PaperPosition(
            symbol="AAPL",
            qty="21",
            side="long",
            market_value="2142",
            avg_entry_price="100",
            current_price="102",
        ),
    )
    broker.orders = ()

    result = _orchestrator(tmp_path, broker).tick(
        _plan(),
        PaperSessionSnapshot(
            policy=_policy_snapshot(has_position=True, observed_at=regular),
            bid=Decimal("101.99"),
            ask=Decimal("102.01"),
            quote_asof_utc=regular,
            quote_provenance="alpaca.sip.nbbo",
        ),
    )

    assert result.decision is not None
    assert result.decision.action.value == "upgrade"
    assert result.action is SessionAction.ENTRY_SUBMITTED
    assert len(broker.entry_requests) == 1
    assert broker.entry_requests[0].qty == 22


def test_realized_main_profit_trims_only_the_difference_to_tail(
    tmp_path: Path,
) -> None:
    regular = datetime(2026, 7, 29, 15, 0, tzinfo=UTC)
    broker = FakeAutonomousBroker()
    broker.account = broker.account.model_copy(
        update={"equity": "100000", "last_equity": "100000"}
    )
    broker.positions = (
        PaperPosition(
            symbol="AAPL",
            qty="43",
            side="long",
            market_value="4386",
            avg_entry_price="100",
            current_price="102",
        ),
    )
    broker.orders = (
        BrokerOrder(
            id="working-target-1",
            client_order_id="tsv2-owned-target",
            symbol="AAPL",
            qty=43,
            filled_qty="0",
            status="new",
        ),
    )
    orchestrator = _orchestrator(tmp_path, broker)

    result = orchestrator.tick(
        _plan(),
        PaperSessionSnapshot(
            policy=_policy_snapshot(
                has_position=True,
                observed_at=regular,
                position_fraction=0.50,
                main_profit_realized=True,
            ),
            bid=Decimal("101.99"),
            ask=Decimal("102.01"),
            quote_asof_utc=regular,
            quote_provenance="alpaca.sip.nbbo",
        ),
    )
    replay = orchestrator.tick(
        _plan(),
        PaperSessionSnapshot(
            policy=_policy_snapshot(
                has_position=True,
                observed_at=regular,
                position_fraction=0.50,
                main_profit_realized=True,
            ),
            bid=Decimal("101.99"),
            ask=Decimal("102.01"),
            quote_asof_utc=regular,
            quote_provenance="alpaca.sip.nbbo",
        ),
    )

    assert result.decision is not None
    assert result.decision.action.value == "trim_to_tail"
    assert result.decision.target_position_fraction == 0.25
    assert result.action is SessionAction.REDUCE_SUBMITTED
    assert result.cancelled_order_ids == ("working-target-1",)
    assert [(item.symbol, item.qty) for item in broker.close_requests] == [
        ("AAPL", 22)
    ]
    assert replay.action is SessionAction.REDUCE_SUBMITTED
    assert replay.cancelled_order_ids == ()
    assert broker.cancelled == ["working-target-1"]
    assert len(broker.close_requests) == 1


def test_valid_entry_is_explicitly_blocked_until_paper_is_armed(
    tmp_path: Path,
) -> None:
    broker = FakeAutonomousBroker()
    broker.account = broker.account.model_copy(
        update={"equity": "100000", "last_equity": "100000"}
    )
    broker.positions = ()
    broker.orders = ()
    result = PaperSessionOrchestrator(
        broker=broker,
        ledger=PaperSessionLedger(tmp_path / "autonomous-paper.sqlite3"),
        paper_authorized=False,
        owned_symbols=frozenset({"AAPL"}),
    ).tick(
        _plan(),
        PaperSessionSnapshot(
            policy=_policy_snapshot(has_position=False),
            bid=Decimal("101.99"),
            ask=Decimal("102.01"),
            quote_asof_utc=NOW,
            quote_provenance="alpaca.sip.nbbo",
        ),
    )

    assert result.decision is not None
    assert result.decision.action.value == "enter_probe"
    assert result.action is SessionAction.WRITES_BLOCKED
    assert result.reasons[-1] == "paper_entry_writes_not_authorized"
    assert broker.entry_requests == []
    assert broker.extended_requests == []
    assert broker.close_requests == []


def test_partial_premarket_fill_cancels_remainder_and_protects_filled_shares(
    tmp_path: Path,
) -> None:
    premarket = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
    broker = FakeAutonomousBroker()
    broker.account = broker.account.model_copy(
        update={"equity": "100000", "last_equity": "100000"}
    )
    broker.positions = ()
    broker.orders = ()
    orchestrator = _orchestrator(tmp_path, broker)
    orchestrator.tick(
        _plan(),
        PaperSessionSnapshot(
            policy=_policy_snapshot(has_position=False, observed_at=premarket),
            bid=Decimal("101.99"),
            ask=Decimal("102.01"),
            quote_asof_utc=premarket,
            quote_provenance="alpaca.sip.nbbo",
        ),
    )
    first_client_id = broker.extended_requests[0].client_order_id
    broker.entry_orders[first_client_id] = broker.entry_orders[
        first_client_id
    ].model_copy(update={"filled_qty": "5", "status": "partially_filled"})
    broker.positions = (
        PaperPosition(
            symbol="AAPL",
            qty="5",
            side="long",
            market_value="489.50",
            avg_entry_price="102.01",
            current_price="97.90",
        ),
    )
    expired_at = premarket + timedelta(seconds=11)

    result = orchestrator.tick(
        _plan(),
        PaperSessionSnapshot(
            policy=_policy_snapshot(has_position=True, observed_at=expired_at),
            bid=Decimal("97.90"),
            ask=Decimal("97.92"),
            quote_asof_utc=expired_at,
            quote_provenance="alpaca.sip.nbbo",
            last_trade=Decimal("97.91"),
            last_trade_asof_utc=expired_at,
            trade_provenance="alpaca.sip.trade",
        ),
    )

    assert result.action is SessionAction.STOP_EXIT_SUBMITTED
    assert broker.cancelled == ["premarket-entry-1"]
    assert [request.side for request in broker.extended_requests] == ["buy", "sell"]
    assert broker.extended_requests[-1].qty == 5


def test_thirteen_hundred_force_exit_is_idempotent(
    tmp_path: Path,
) -> None:
    force_exit = datetime(2026, 7, 29, 17, 0, tzinfo=UTC)
    broker = FakeAutonomousBroker()
    broker.account = broker.account.model_copy(
        update={"equity": "100000", "last_equity": "100000"}
    )
    broker.positions = (
        PaperPosition(
            symbol="AAPL",
            qty="21",
            side="long",
            market_value="2142",
            avg_entry_price="100",
            current_price="102",
        ),
    )
    broker.orders = ()
    orchestrator = _orchestrator(tmp_path, broker)
    snapshot = PaperSessionSnapshot(
        policy=_policy_snapshot(has_position=True, observed_at=force_exit),
        bid=Decimal("101.99"),
        ask=Decimal("102.01"),
        quote_asof_utc=force_exit,
        quote_provenance="alpaca.sip.nbbo",
    )

    first = orchestrator.tick(_plan(), snapshot)
    replay = orchestrator.tick(_plan(), snapshot)

    assert first.decision is not None
    assert first.decision.reasons == ("intraday_force_exit_1200",)
    assert first.action is SessionAction.EXIT_SUBMITTED
    assert len(broker.close_requests) == 1
    assert broker.close_requests[0].qty == 21
    assert replay.action is SessionAction.EXIT_SUBMITTED
    assert len(broker.close_requests) == 1


def test_stale_quote_explicitly_blocks_a_valid_entry(
    tmp_path: Path,
) -> None:
    broker = FakeAutonomousBroker()
    broker.account = broker.account.model_copy(
        update={"equity": "100000", "last_equity": "100000"}
    )
    broker.positions = ()
    broker.orders = ()

    result = _orchestrator(tmp_path, broker).tick(
        _plan(),
        PaperSessionSnapshot(
            policy=_policy_snapshot(has_position=False),
            bid=Decimal("101.99"),
            ask=Decimal("102.01"),
            quote_asof_utc=NOW - timedelta(seconds=31),
            quote_provenance="alpaca.sip.nbbo",
        ),
    )

    assert result.decision is not None
    assert result.decision.action.value == "enter_probe"
    assert result.action is SessionAction.DATA_BLOCKED
    assert result.reasons[-1] == "quote_stale"
    assert broker.entry_requests == []
    assert broker.extended_requests == []


def test_premarket_daily_hard_loss_uses_marketable_extended_limit(
    tmp_path: Path,
) -> None:
    premarket = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
    broker = FakeAutonomousBroker()

    result = _orchestrator(tmp_path, broker).tick(
        _plan(),
        PaperSessionSnapshot(
            policy=_policy_snapshot(has_position=True, observed_at=premarket),
            bid=Decimal("101.99"),
            ask=Decimal("102.01"),
            quote_asof_utc=premarket,
            quote_provenance="alpaca.sip.nbbo",
        ),
    )

    assert result.action is SessionAction.HARD_LOSS_FLATTEN
    assert broker.close_requests == []
    assert len(broker.extended_requests) == 1
    assert broker.extended_requests[0].side == "sell"
    assert broker.extended_requests[0].qty == 10
    assert broker.extended_requests[0].limit_price == "101.73"


def test_runtime_failure_regular_hours_closes_position_idempotently(
    tmp_path: Path,
) -> None:
    broker = FakeAutonomousBroker()
    broker.account = broker.account.model_copy(
        update={"equity": "100000", "last_equity": "100000"}
    )
    orchestrator = _orchestrator(tmp_path, broker)

    first = orchestrator.fail_closed(
        _plan(),
        observed_at_utc=NOW,
        reason="market_adapter_failed",
    )
    replay = orchestrator.fail_closed(
        _plan(),
        observed_at_utc=NOW,
        reason="market_adapter_failed",
    )

    assert first.action is SessionAction.EXIT_SUBMITTED
    assert first.reasons == ("market_adapter_failed",)
    assert len(broker.close_requests) == 1
    assert replay.action is SessionAction.EXIT_SUBMITTED
    assert len(broker.close_requests) == 1


def test_runtime_failure_premarket_without_fresh_bid_blocks_unsafe_exit(
    tmp_path: Path,
) -> None:
    premarket = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
    broker = FakeAutonomousBroker()
    broker.account = broker.account.model_copy(
        update={"equity": "100000", "last_equity": "100000"}
    )

    result = _orchestrator(tmp_path, broker).fail_closed(
        _plan(),
        observed_at_utc=premarket,
        reason="market_adapter_failed",
    )

    assert result.action is SessionAction.DATA_BLOCKED
    assert result.reasons == (
        "market_adapter_failed",
        "extended_exit_quote_unavailable",
    )
    assert broker.close_requests == []
    assert broker.extended_requests == []


def test_runtime_failure_flat_account_blocks_entries_without_broker_writes(
    tmp_path: Path,
) -> None:
    broker = FakeAutonomousBroker()
    broker.account = broker.account.model_copy(
        update={"equity": "100000", "last_equity": "100000"}
    )
    broker.positions = ()
    broker.orders = ()

    result = _orchestrator(tmp_path, broker).fail_closed(
        _plan(),
        observed_at_utc=NOW,
        reason="required_agent_unavailable",
    )

    assert result.action is SessionAction.DATA_BLOCKED
    assert result.new_entries_allowed is False
    assert broker.cancelled == []
    assert broker.close_requests == []


def test_runtime_failure_flat_account_cancels_pending_entry_before_returning(
    tmp_path: Path,
) -> None:
    broker = FakeAutonomousBroker()
    broker.account = broker.account.model_copy(
        update={"equity": "100000", "last_equity": "100000"}
    )
    broker.positions = ()
    broker.orders = (
        BrokerOrder(
            id="pending-entry-1",
            client_order_id="tsv2-owned-entry",
            symbol="AAPL",
            qty=10,
            filled_qty="0",
            status="new",
        ),
    )

    result = _orchestrator(tmp_path, broker).fail_closed(
        _plan(),
        observed_at_utc=NOW,
        reason="notification_push_failed",
    )

    assert result.action is SessionAction.DATA_BLOCKED
    assert result.new_entries_allowed is False
    assert result.cancelled_order_ids == ("pending-entry-1",)
    assert broker.cancelled == ["pending-entry-1"]
    assert broker.close_requests == []


def test_autonomous_paper_schema_is_versioned(tmp_path: Path) -> None:
    ledger = PaperSessionLedger(tmp_path / "autonomous-paper.sqlite3")

    with ledger._connect() as connection:
        row = connection.execute(
            """
            SELECT owner, version, name
            FROM schema_migrations
            WHERE owner = 'execution.autonomous_paper_session'
            """
        ).fetchone()

    assert row is not None
    assert tuple(row) == (
        "execution.autonomous_paper_session",
        1,
        "autonomous_paper_schema",
    )
