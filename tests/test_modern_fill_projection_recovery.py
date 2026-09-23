from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from test_vps_investment_base import Runner, binding

from execution.alpaca_paper import BrokerOrder
from operations.paper_state import PaperStateStore
from operations.vps_investment_base import VpsInvestmentClient
from scripts.monitor_modern_momentum_paper import (
    _remember_fill,
    publish_fill_observations,
    publish_monitor_transitions,
)


def test_trade_and_monitor_recover_without_repeating_livermore(tmp_path: Path) -> None:
    settings, _ = binding(tmp_path)
    runner = Runner(settings)
    runner.total_override = 201  # fail reads before append, not an uncertain write
    client = VpsInvestmentClient(settings, runner=runner, sleep=lambda _: None)
    store = PaperStateStore(tmp_path / "paper.sqlite3")
    now = datetime(2026, 9, 22, 14, tzinfo=UTC)
    trade_date = date(2026, 9, 22)
    state: dict[str, Any] = {}
    order = BrokerOrder(id="test-order", client_order_id="test-client", symbol="TEST",
                        qty=2, filled_qty="2", filled_avg_price="10", status="filled", side="buy")
    _remember_fill(state, order, now)
    store.save_symbol_state(trade_date=trade_date, symbol="TEST", state=state, observed_at_utc=now)

    class Push:
        count = 0

        def push(self, body: str) -> str:
            self.count += 1
            return "test-message"

    push = Push()
    events: list[dict[str, object]] = []
    messages: list[str] = []
    for _ in range(3):
        publish_fill_observations(store, push, client, trade_date=trade_date,  # type: ignore[arg-type]
                                  events=events, message_ids=messages)
        runner.total_override = None
    assert push.count == 1 and messages == ["test-message"]
    assert runner.writes == 2  # one trade and one monitor transition; no poll rows
    assert client.flush_pending() == {"delivered": 0, "failed": 0}
    assert {event["type"] for event in events} == {"monitor_write_failed", "feishu_write_failed"}


def test_no_fill_monitor_states_persist_and_deduplicate(tmp_path: Path) -> None:
    settings, _ = binding(tmp_path)
    runner = Runner(settings)
    client = VpsInvestmentClient(settings, runner=runner, sleep=lambda _: None)
    store = PaperStateStore(tmp_path / "paper.sqlite3")
    now = datetime(2026, 9, 22, 14, tzinfo=UTC)
    for _ in range(3):
        store.observe_monitor_transition(trade_date=now.date(), symbol="TEST", phase="blocked",
                                         reason="点差超过门槛", observed_at_utc=now)
    for reason in ("风险额度不足", "点差超过门槛"):
        store.observe_monitor_transition(trade_date=now.date(), symbol="TEST", phase="blocked",
                                         reason=reason, observed_at_utc=now)
    for phase in ("entry_pending", "entry_pending", "complete"):
        store.save_symbol_state(trade_date=now.date(), symbol="TEST",
                                state={"phase": phase, "entry_client_id": "test-entry"},
                                observed_at_utc=now)
    restarted = PaperStateStore(store.path)
    assert len(restarted.pending_monitor_transitions()) == 5
    events: list[dict[str, object]] = []
    publish_monitor_transitions(restarted, client, events)
    publish_monitor_transitions(restarted, client, events)
    assert not events and not restarted.pending_monitor_transitions()
    assert runner.writes == 5


def test_retained_day_journal_drains_without_running_trading_monitor(tmp_path: Path) -> None:
    from scripts.sync_investment_records import paper_journal_roots, recover_monitor_journals

    settings, _ = binding(tmp_path)
    runner = Runner(settings)
    client = VpsInvestmentClient(settings, runner=runner, sleep=lambda _: None)
    now = datetime(2026, 9, 21, 14, tzinfo=UTC)
    roots = paper_journal_roots(tmp_path)
    assert roots == (
        tmp_path / "runs/modern-momentum",
        tmp_path / "runs/paper-recovery",
    )
    stores = []
    for index, root in enumerate(roots):
        store = PaperStateStore(root / "2026-09-21/paper-state.sqlite3")
        store.observe_monitor_transition(
            trade_date=now.date(), symbol="TEST", phase="observing",
            reason=f"等待确认{index}", observed_at_utc=now,
        )
        stores.append(store)
    pending_keys = {
        key for store in stores for key, _ in store.pending_monitor_transitions()
    }
    assert len(pending_keys) == 2
    for _ in range(2):
        assert sum(recover_monitor_journals(root, client) for root in roots) == 0
    assert runner.writes == 2
    assert all(not store.list_orders() for store in stores)


def test_paper_journal_source_initialization_is_concurrent_safe(tmp_path: Path) -> None:
    path = tmp_path / "runs/modern-momentum/2026-09-21/paper-state.sqlite3"
    with ThreadPoolExecutor(max_workers=8) as pool:
        stores = tuple(pool.map(lambda _: PaperStateStore(path), range(24)))
    assert {store.source_id for store in stores} == {"modern-momentum"}


def test_failing_monitor_batch_rotates_without_starving_newer_events(tmp_path: Path) -> None:
    settings, _ = binding(tmp_path)
    runner = Runner(settings)
    runner.total_override = 201
    client = VpsInvestmentClient(settings, runner=runner, sleep=lambda _: None)
    store = PaperStateStore(tmp_path / "paper.sqlite3")
    now = datetime(2026, 9, 22, 14, tzinfo=UTC)
    for index in range(6):
        store.observe_monitor_transition(trade_date=now.date(), symbol="TEST", phase="blocked",
                                         reason=f"测试条件{index}", observed_at_utc=now)
    before = {key for key, _ in store.pending_monitor_transitions()}
    publish_monitor_transitions(store, client, [])
    assert before != {key for key, _ in store.pending_monitor_transitions()}
