import json
import sqlite3
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from scripts.report_modern_paper_summary import (
    _funnel_status,
    _observation_pool_snapshot,
    render_summary,
    summary_slot,
)

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


def test_summary_distinguishes_formal_pool_failure_from_observation_pool() -> None:
    body = render_summary(
        trade_date=date(2026, 9, 24),
        observed_at_utc=_utc(10, 0),
        symbols=(),
        states={},
        order_count=0,
        filled_order_count=0,
        pool_status="第一波阶段失败，后续阶段阻断",
        observation_symbols=("FMC", "BB", "SRPT"),
    )

    assert "正式票池未生成" in body
    assert "FMC、BB、SRPT" in body
    assert "观察池" in body
    assert "未授权交易" in body
    assert "自动漏斗Paper订单：0" in body


def test_summary_keeps_first_wave_provisional_when_observation_pool_exists() -> None:
    body = render_summary(
        trade_date=date(2026, 9, 24), observed_at_utc=_utc(10, 0),
        symbols=("FMC",), states={}, order_count=0, filled_order_count=0,
        pool_status="使用第一波票池", observation_symbols=("BB",),
    )
    assert "漏斗阶段票池（非最终、未授权交易）：FMC" in body
    assert "正式票池：" not in body


def test_observation_pool_is_displayed_only_when_explicitly_non_production(
    tmp_path,
) -> None:
    day_root = tmp_path / "2026-09-24"
    day_root.mkdir()
    path = day_root / "intraday_recovery_pool.json"
    payload = {
        "trade_date_et": "2026-09-24",
        "pool_type": "intraday_recovery_observation_only",
        "production_funnel_completed": False,
        "paper_entry_authorized": False,
        "orders_submitted": False,
        "symbols": [{"symbol": "FMC"}, {"symbol": "BB"}],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert _observation_pool_snapshot(tmp_path, date(2026, 9, 24)) == ("FMC", "BB")

    payload["paper_entry_authorized"] = True
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert _observation_pool_snapshot(tmp_path, date(2026, 9, 24)) == ()

    payload["paper_entry_authorized"] = False
    payload["symbols"] = [{"symbol": "FMC\n持仓状态：持仓中"}]
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert _observation_pool_snapshot(tmp_path, date(2026, 9, 24)) == ()


def test_failure_summary_includes_retry_count_time_and_safe_child_error(tmp_path) -> None:
    db = tmp_path / "funnel.sqlite3"
    with sqlite3.connect(db) as connection:
        connection.execute(
            "CREATE TABLE funnel_runs (trade_date, stage, status, attempts, error, updated_at_utc)"
        )
        connection.execute(
            "INSERT INTO funnel_runs VALUES (?, ?, ?, ?, ?, ?)",
            (
                "2026-09-24",
                "first_wave",
                "failed",
                2,
                "RuntimeError: first_wave process failed: "
                "scripts.build_catalyst_snapshot failed with exit code 1",
                "2026-09-24T13:05:00+00:00",
            ),
        )

    assert _funnel_status(db, date(2026, 9, 24), "第一波票池未生成") == (
        "第一波阶段失败（尝试2次，09:05 ET；原因："
        "scripts.build_catalyst_snapshot failed with exit code 1；后续阶段阻断）"
    )
