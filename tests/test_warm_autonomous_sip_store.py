from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from scripts.warm_autonomous_sip_store import build_fetch_windows


def test_full_warmup_is_causal_and_microstructure_is_bounded_to_ten_minutes() -> None:
    observed = datetime(2026, 7, 29, 14, 2, 37, tzinfo=UTC)

    windows = build_fetch_windows(
        trade_date=date(2026, 7, 29),
        observed_at_utc=observed,
        history_days=10,
        incremental=False,
    )

    assert windows.bars_start_utc < datetime(
        2026, 7, 29, 13, 30, tzinfo=UTC
    )
    assert windows.bars_end_utc == datetime(
        2026, 7, 29, 14, 2, tzinfo=UTC
    )
    assert windows.micro_start_utc == observed - timedelta(minutes=10)
    assert windows.micro_end_utc == observed


def test_incremental_warmup_starts_at_current_market_open() -> None:
    observed = datetime(2026, 7, 29, 14, 2, 37, tzinfo=UTC)

    windows = build_fetch_windows(
        trade_date=date(2026, 7, 29),
        observed_at_utc=observed,
        history_days=10,
        incremental=True,
    )

    assert windows.bars_start_utc == datetime(
        2026, 7, 29, 13, 30, tzinfo=UTC
    )
    assert windows.bars_end_utc == datetime(
        2026, 7, 29, 14, 2, tzinfo=UTC
    )
