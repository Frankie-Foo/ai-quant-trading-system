from __future__ import annotations

from datetime import UTC, date, datetime

from schedule.paper import paper_session_window


def test_paper_window_opens_before_rth_and_closes_at_exchange_close() -> None:
    assert paper_session_window(
        datetime(2026, 7, 22, 13, 19, tzinfo=UTC),
        start_lead_minutes=10,
    ) is None

    window = paper_session_window(
        datetime(2026, 7, 22, 13, 20, tzinfo=UTC),
        start_lead_minutes=10,
    )
    assert window is not None
    assert window.trade_date == date(2026, 7, 22)
    assert window.market_open_utc == datetime(2026, 7, 22, 13, 30, tzinfo=UTC)
    assert window.market_close_utc == datetime(2026, 7, 22, 20, 0, tzinfo=UTC)

    assert paper_session_window(
        datetime(2026, 7, 22, 20, 0, tzinfo=UTC),
        start_lead_minutes=10,
    ) is None


def test_paper_window_uses_exchange_calendar_dst_and_skips_weekends() -> None:
    winter = paper_session_window(
        datetime(2026, 11, 3, 14, 20, tzinfo=UTC),
        start_lead_minutes=10,
    )
    assert winter is not None
    assert winter.market_open_utc == datetime(2026, 11, 3, 14, 30, tzinfo=UTC)

    assert paper_session_window(
        datetime(2026, 7, 19, 14, 0, tzinfo=UTC),
        start_lead_minutes=10,
    ) is None
