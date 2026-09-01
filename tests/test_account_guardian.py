from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from execution.account_guardian import (
    AccountGuardian,
    AccountGuardianLedger,
    AccountGuardianStatus,
)
from execution.alpaca_paper import BrokerOrder, PaperCloseRequest, PaperPosition

NOW = datetime(2026, 7, 29, 14, 0, tzinfo=UTC)
PREMARKET = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
TRADE_DATE = date(2026, 7, 29)


class FakeExclusiveBroker:
    writes_enabled = True

    def __init__(self, *, unknown_order: bool = False) -> None:
        self.unknown_order = unknown_order
        self.cancelled: list[str] = []
        self.flattened: list[tuple[str, int]] = []

    def list_open_orders(self) -> tuple[BrokerOrder, ...]:
        orders = [
            BrokerOrder(
                id="owned-1",
                client_order_id="tsv2-owned-1",
                symbol="AAPL",
                qty=5,
                filled_qty="0",
                status="new",
            )
        ]
        if self.unknown_order:
            orders.append(
                BrokerOrder(
                    id="manual-1",
                    client_order_id="mobile-order",
                    symbol="MSFT",
                    qty=2,
                    filled_qty="0",
                    status="new",
                )
            )
        return tuple(orders)

    def list_positions(self) -> tuple[PaperPosition, ...]:
        return (
            PaperPosition(
                symbol="AAPL",
                qty="5",
                side="long",
                market_value="500",
            ),
            PaperPosition(
                symbol="MSFT",
                qty="2",
                side="long",
                market_value="200",
            ),
        )

    def cancel_order(self, order_id: str) -> bool:
        self.cancelled.append(order_id)
        return True

    def submit_close_order_idempotent(
        self, request: PaperCloseRequest
    ) -> BrokerOrder:
        self.flattened.append((request.symbol, request.qty))
        return BrokerOrder(
            id=f"flatten-{request.symbol}",
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            qty=request.qty,
            filled_qty="0",
            status="new",
        )


def test_clean_exclusive_account_remains_trade_enabled(tmp_path: Path) -> None:
    broker = FakeExclusiveBroker()
    guardian = AccountGuardian(
        broker=broker,
        ledger=AccountGuardianLedger(tmp_path / "guardian.sqlite3"),
        paper_authorized=True,
    )

    result = guardian.reconcile(
        trade_date=TRADE_DATE,
        now_utc=NOW,
        owned_position_symbols=frozenset({"AAPL", "MSFT"}),
    )

    assert result.status is AccountGuardianStatus.CLEAR
    assert result.new_entries_allowed is True
    assert broker.cancelled == []
    assert broker.flattened == []


def test_unknown_order_cancels_all_flattens_all_and_locks_day(tmp_path: Path) -> None:
    broker = FakeExclusiveBroker(unknown_order=True)
    guardian = AccountGuardian(
        broker=broker,
        ledger=AccountGuardianLedger(tmp_path / "guardian.sqlite3"),
        paper_authorized=True,
    )

    result = guardian.reconcile(
        trade_date=TRADE_DATE,
        now_utc=NOW,
        owned_position_symbols=frozenset({"AAPL", "MSFT"}),
    )

    assert result.status is AccountGuardianStatus.DAY_LOCKED
    assert result.new_entries_allowed is False
    assert set(broker.cancelled) == {"owned-1", "manual-1"}
    assert set(broker.flattened) == {("AAPL", 5), ("MSFT", 2)}
    assert result.reasons == ("unknown_order_detected",)


def test_unknown_position_also_triggers_lock_and_survives_restart(
    tmp_path: Path,
) -> None:
    broker = FakeExclusiveBroker()
    ledger_path = tmp_path / "guardian.sqlite3"
    first = AccountGuardian(
        broker=broker,
        ledger=AccountGuardianLedger(ledger_path),
        paper_authorized=True,
    )
    first_result = first.reconcile(
        trade_date=TRADE_DATE,
        now_utc=NOW,
        owned_position_symbols=frozenset({"AAPL"}),
    )
    restarted = AccountGuardian(
        broker=broker,
        ledger=AccountGuardianLedger(ledger_path),
        paper_authorized=True,
    )
    replay = restarted.reconcile(
        trade_date=TRADE_DATE,
        now_utc=NOW,
        owned_position_symbols=frozenset({"AAPL", "MSFT"}),
    )

    assert first_result.reasons == ("unknown_position_detected",)
    assert replay.status is AccountGuardianStatus.DAY_LOCKED
    assert replay.new_entries_allowed is False
    assert broker.flattened == [("AAPL", 5), ("MSFT", 2)]


def test_guardian_does_not_mutate_broker_until_paper_is_armed(
    tmp_path: Path,
) -> None:
    broker = FakeExclusiveBroker(unknown_order=True)
    result = AccountGuardian(
        broker=broker,
        ledger=AccountGuardianLedger(tmp_path / "guardian.sqlite3"),
        paper_authorized=False,
    ).reconcile(
        trade_date=TRADE_DATE,
        now_utc=NOW,
        owned_position_symbols=frozenset({"AAPL", "MSFT"}),
    )

    assert result.status is AccountGuardianStatus.BLOCKED
    assert result.new_entries_allowed is False
    assert broker.cancelled == []
    assert broker.flattened == []


def test_premarket_unknown_state_cancels_orders_but_never_queues_market_exit(
    tmp_path: Path,
) -> None:
    broker = FakeExclusiveBroker(unknown_order=True)
    guardian = AccountGuardian(
        broker=broker,
        ledger=AccountGuardianLedger(tmp_path / "guardian.sqlite3"),
        paper_authorized=True,
    )

    result = guardian.reconcile(
        trade_date=TRADE_DATE,
        now_utc=PREMARKET,
        owned_position_symbols=frozenset({"AAPL", "MSFT"}),
    )

    assert result.status is AccountGuardianStatus.BLOCKED
    assert result.cancelled_order_ids == ("owned-1", "manual-1")
    assert result.flatten_order_ids == ()
    assert result.reasons == (
        "unknown_order_detected",
        "extended_hours_guardian_flatten_unavailable",
    )
    assert broker.flattened == []
    assert guardian.ledger.status(TRADE_DATE) is None


def test_account_guardian_schema_is_versioned(tmp_path: Path) -> None:
    ledger = AccountGuardianLedger(tmp_path / "guardian.sqlite3")

    with ledger._connect() as connection:
        row = connection.execute(
            """
            SELECT owner, version, name
            FROM schema_migrations
            WHERE owner = 'execution.account_guardian'
            """
        ).fetchone()

    assert row is not None
    assert tuple(row) == (
        "execution.account_guardian",
        1,
        "account_guardian_days",
    )
