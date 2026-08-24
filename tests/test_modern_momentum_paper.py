from datetime import UTC, datetime
from pathlib import Path

from execution.alpaca_paper import BrokerOrder
from research.h30_challenger import _FiveMinuteBar
from scripts.monitor_modern_momentum_paper import (
    _record_fill_best_effort,
    attempt_risk_fraction,
    order_id,
    position_size,
    pullback_reentry,
    reentry_exit_reason,
    risk_fraction,
    session_control_times,
)


def test_modern_paper_source_never_submits_an_unprotected_entry() -> None:
    source = Path(__file__).parents[1].joinpath(
        "scripts", "monitor_modern_momentum_paper.py"
    ).read_text(encoding="utf-8")
    assert "build_protected_entry" in source
    assert "load_open_confirmation" in source
    assert "validate_arming" in source
    assert "PaperOrderRequest(" not in source
    assert "submit_stop_order_idempotent" not in source
    assert "PaperStateStore" in source


def test_session_controls_stop_entries_and_force_flatten_before_close() -> None:
    opened = datetime(2026, 8, 24, 13, 30, tzinfo=UTC)
    closed = datetime(2026, 8, 24, 20, 0, tzinfo=UTC)

    entry_cutoff, cancel_at, flatten_at = session_control_times(opened, closed)

    assert entry_cutoff == datetime(2026, 8, 24, 19, 0, tzinfo=UTC)
    assert cancel_at == datetime(2026, 8, 24, 19, 45, tzinfo=UTC)
    assert flatten_at == datetime(2026, 8, 24, 19, 50, tzinfo=UTC)


def test_paper_identity_and_position_size_are_bounded() -> None:
    assert order_id("2026-08-18", "AMLX", "entry", attempt=1) == (
        "mm-20260818-AMLX-entry-1"
    )
    assert order_id("2026-08-18", "AMLX", "entry", attempt=2) == (
        "mm-20260818-AMLX-entry-2"
    )
    assert risk_fraction(hard_catalyst=True) == 0.005
    assert risk_fraction(hard_catalyst=False) == 0.0025
    assert attempt_risk_fraction(0.005, attempt=1) == 0.003
    assert attempt_risk_fraction(0.005, attempt=2) == 0.002
    assert (
        position_size(
            entry_price=20.0,
            all_in_stop_pct=0.02,
            equity=200_000.0,
            buying_power=400_000.0,
            risk_fraction=0.005,
            remaining_slots=3,
        )
        == 2_500
    )
    assert (
        position_size(
            entry_price=200.0,
            all_in_stop_pct=0.005,
            equity=200_000.0,
            buying_power=400_000.0,
            risk_fraction=0.005,
            remaining_slots=3,
        )
        == 666
    )


def test_amlx_replay_allows_one_reentry_after_pullback_acceptance() -> None:
    stopped_at = datetime(2026, 8, 18, 14, 48, tzinfo=UTC)
    bars = [
        _bar("2026-08-18T14:50:00+00:00", 30.50, 31.0799, 30.99, 177_952, 30.2303),
        _bar("2026-08-18T14:55:00+00:00", 30.95, 31.24, 31.2121, 121_263, 30.2424),
        _bar("2026-08-18T15:00:00+00:00", 31.14, 31.5526, 31.52, 166_076, 30.2641),
    ]

    signal = pullback_reentry(
        bars,
        stopped_at_utc=stopped_at,
        h15=30.69,
        asof_utc=datetime(2026, 8, 18, 15, 5, tzinfo=UTC),
    )

    assert signal is not None
    assert signal.entry_reference == 31.52
    assert signal.structural_stop == 30.95


def test_reentry_rejects_weak_reclaim() -> None:
    stopped_at = datetime(2026, 8, 18, 14, 48, tzinfo=UTC)
    bars = [
        _bar("2026-08-18T14:50:00+00:00", 30.50, 31.08, 30.99, 177_952, 30.2303),
        _bar("2026-08-18T14:55:00+00:00", 30.95, 31.24, 31.21, 121_263, 30.2424),
        _bar("2026-08-18T15:00:00+00:00", 31.14, 31.55, 31.52, 50_000, 30.2641),
    ]

    assert (
        pullback_reentry(
            bars,
            stopped_at_utc=stopped_at,
            h15=30.69,
            asof_utc=datetime(2026, 8, 18, 15, 5, tzinfo=UTC),
        )
        is None
    )


def test_second_entry_uses_target_or_confirmed_trend_exit() -> None:
    entered_at = datetime(2026, 8, 18, 15, 5, tzinfo=UTC)
    bars = [
        _bar("2026-08-18T15:05:00+00:00", 31.50, 32.04, 31.91, 407_537, 30.34),
        _bar("2026-08-18T15:10:00+00:00", 31.80, 32.43, 32.36, 430_101, 30.42),
    ]

    assert (
        reentry_exit_reason(
            bars,
            entered_at_utc=entered_at,
            asof_utc=datetime(2026, 8, 18, 15, 15, tzinfo=UTC),
            target_level=32.40,
            liquidation_utc=datetime(2026, 8, 18, 19, 45, tzinfo=UTC),
        )
        == "target_3r"
    )


def test_feishu_failure_does_not_block_fill_processing() -> None:
    class BrokenBase:
        def record_event(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("base unavailable")

    error = _record_fill_best_effort(
        BrokenBase(),  # type: ignore[arg-type]
        client_order_id="mm-20260818-AMLX-stop-1",
        symbol="AMLX",
        direction="卖出",
        order=BrokerOrder(
            id="order-1",
            client_order_id="mm-20260818-AMLX-stop-1",
            symbol="AMLX",
            qty=1245,
            filled_qty="1245",
            status="filled",
            filled_avg_price="30.876385",
        ),
        reason="第1次入场保护止损",
    )

    assert error == "RuntimeError"


def _bar(
    timestamp: str,
    low: float,
    high: float,
    close: float,
    volume: float,
    session_vwap: float,
) -> _FiveMinuteBar:
    return _FiveMinuteBar(
        datetime.fromisoformat(timestamp),
        close,
        high,
        low,
        close,
        volume,
        close,
        session_vwap,
        close,
        close,
    )
