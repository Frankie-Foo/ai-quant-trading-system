from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from scripts.report_modern_paper_summary import render_summary, summary_slot

EASTERN = ZoneInfo("America/New_York")


def _utc(hour: int, minute: int) -> datetime:
    return datetime(2026, 9, 16, hour, minute, tzinfo=EASTERN).astimezone(UTC)


def test_summary_slots_are_hourly_and_end_before_close() -> None:
    assert summary_slot(_utc(9, 59)) is None
    assert summary_slot(_utc(10, 0)) == "1000"
    assert summary_slot(_utc(10, 4)) == "1000"
    assert summary_slot(_utc(10, 5)) is None
    assert summary_slot(_utc(11, 0)) == "1100"
    assert summary_slot(_utc(15, 0)) == "1500"
    assert summary_slot(_utc(15, 5)) is None
    assert summary_slot(_utc(15, 50)) is None


def test_summary_is_factual_about_positions_and_fills() -> None:
    body = render_summary(
        trade_date=date(2026, 9, 16),
        observed_at_utc=_utc(10, 0),
        symbols=("AAA", "BBB"),
        states={"AAA": {"phase": "active"}},
        order_count=2,
        filled_order_count=1,
    )

    assert "10:00 ET" in body
    assert "每小时盘中摘要" in body
    assert "AAA、BBB" in body
    assert "大盘：未获取" in body
    assert "AAA：持仓中" in body
    assert "已成交订单：1" in body
    assert "15:50前清仓" in body


def test_summary_explains_missing_pool() -> None:
    body = render_summary(
        trade_date=date(2026, 9, 16),
        observed_at_utc=_utc(10, 0),
        symbols=(),
        states={},
        order_count=0,
        filled_order_count=0,
        pool_status="第一波阶段失败，后续阶段阻断",
        market_status="SPY+0.40%；QQQ+0.75%",
    )

    assert "票池：未生成（第一波阶段失败，后续阶段阻断）" in body
    assert "大盘：SPY+0.40%；QQQ+0.75%" in body
    assert "AI量化运行报警" in body
