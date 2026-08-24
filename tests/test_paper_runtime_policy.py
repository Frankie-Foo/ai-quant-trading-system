from datetime import UTC, date, datetime

import pytest

from operations.paper_runtime_policy import ExecutionAuthorization, PaperRuntimePolicy

TRADE_DATE = date(2026, 8, 24)
POLICY = PaperRuntimePolicy()


def _authorization(**overrides: object) -> ExecutionAuthorization:
    values: dict[str, object] = {
        "trade_date": TRADE_DATE,
        "selection_snapshot_id": "selection-1",
        "open_confirmation_id": "confirmation-1",
        "feishu_record_id": "record-1",
        "livermore_message_id": "message-1",
        "strategy_version": "modern-h15-v1",
    }
    values.update(overrides)
    return ExecutionAuthorization(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("writes_enabled", "kill_switch", "base_url", "authorization"),
    [
        (False, False, "https://paper-api.alpaca.markets", _authorization()),
        (True, True, "https://paper-api.alpaca.markets", _authorization()),
        (True, False, "https://api.alpaca.markets", _authorization()),
        (True, False, "https://paper-api.alpaca.markets", _authorization(feishu_record_id="")),
        (
            True,
            False,
            "https://paper-api.alpaca.markets",
            _authorization(livermore_message_id=""),
        ),
    ],
)
def test_arming_fails_closed_without_every_gate(
    writes_enabled: bool,
    kill_switch: bool,
    base_url: str,
    authorization: ExecutionAuthorization,
) -> None:
    with pytest.raises(RuntimeError):
        POLICY.validate_arming(
            trade_date=TRADE_DATE,
            broker_write_enabled=writes_enabled,
            trading_kill_switch=kill_switch,
            broker_base_url=base_url,
            authorization=authorization,
        )


def test_arming_accepts_complete_paper_authorization() -> None:
    POLICY.validate_arming(
        trade_date=TRADE_DATE,
        broker_write_enabled=True,
        trading_kill_switch=False,
        broker_base_url="https://paper-api.alpaca.markets",
        authorization=_authorization(),
    )


@pytest.mark.parametrize(
    ("hour", "minute", "entry_allowed", "cancel_entries", "flatten"),
    [
        (9, 55, False, False, False),
        (9, 56, True, False, False),
        (14, 59, True, False, False),
        (15, 0, False, False, False),
        (15, 45, False, True, False),
        (15, 50, False, True, True),
    ],
)
def test_market_clock_enforces_entry_and_liquidation_windows(
    hour: int,
    minute: int,
    entry_allowed: bool,
    cancel_entries: bool,
    flatten: bool,
) -> None:
    now_et = datetime(2026, 8, 24, hour, minute, tzinfo=UTC)
    assert POLICY.entry_allowed_at(now_et) is entry_allowed
    assert POLICY.must_cancel_entries_at(now_et) is cancel_entries
    assert POLICY.must_flatten_at(now_et) is flatten


def test_risk_limits_are_stop_loss_budgets_not_notional_caps() -> None:
    assert POLICY.max_symbol_loss(100_000) == 500
    assert POLICY.max_sector_loss(100_000) == 750
    assert POLICY.max_portfolio_loss(100_000) == 1_500
    assert POLICY.stop_new_entries_loss(100_000) == 1_500
    assert POLICY.flatten_account_loss(100_000) == 2_000
    assert POLICY.position_quantity(
        equity=100_000,
        entry_price=20,
        all_in_stop_pct=0.02,
        buying_power=100_000,
    ) == 1_250


def test_position_quantity_rejects_stop_above_two_percent() -> None:
    with pytest.raises(ValueError, match="2%"):
        POLICY.position_quantity(
            equity=100_000,
            entry_price=20,
            all_in_stop_pct=0.0201,
            buying_power=100_000,
        )
