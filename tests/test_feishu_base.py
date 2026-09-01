from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from operations.feishu_base import (
    FeishuBaseDuplicateError,
    FeishuBaseEventClient,
    FeishuBaseSettings,
    FeishuTableSettings,
    InvestmentTable,
)


def _settings(tmp_path: Path) -> FeishuBaseSettings:
    return FeishuBaseSettings(
        base_token="base-token",
        selection=FeishuTableSettings("tbl-selection", "事件ID"),
        monitor=FeishuTableSettings("tbl-monitor", "事件ID"),
        trade=FeishuTableSettings("tbl-trade", "事件ID"),
        review=FeishuTableSettings("tbl-review", "事件ID"),
        lock_db_path=tmp_path / "feishu-lock.sqlite3",
    )


class FakeLark:
    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.created: dict[str, object] | None = None

    def __call__(self, command: Sequence[str]) -> Mapping[str, object]:
        self.commands.append(tuple(command))
        if "+record-upsert" in command:
            index = command.index("--json") + 1
            self.created = json.loads(command[index])
            return {"ok": True, "data": {"record_id": "rec-created"}}
        if "+record-list" in command:
            if self.created is None:
                return {
                    "ok": True,
                    "data": {
                        "fields": ["事件ID", "操作", "数量"],
                        "data": [],
                        "record_id_list": [],
                    },
                }
            return {
                "ok": True,
                "data": {
                    "fields": ["事件ID", "操作", "数量"],
                    "data": [
                        [
                            self.created["事件ID"],
                            self.created["操作"],
                            self.created["数量"],
                        ]
                    ],
                    "record_id_list": ["rec-created"],
                },
            }
        raise AssertionError(f"unexpected command: {command}")


def test_event_write_is_create_then_exact_readback_and_replay_safe(tmp_path: Path) -> None:
    runner = FakeLark()
    client = FeishuBaseEventClient(
        _settings(tmp_path), runner=runner, sleep=lambda _: None
    )

    first = client.record_event(
        InvestmentTable.TRADE,
        "operation:2026-08-06:NVDA:entry",
        {"操作": "买入", "数量": 10},
    )
    replay = client.record_event(
        InvestmentTable.TRADE,
        "operation:2026-08-06:NVDA:entry",
        {"操作": "买入", "数量": 10},
    )

    assert first == "rec-created"
    assert replay == "rec-created"
    assert sum("+record-upsert" in command for command in runner.commands) == 1
    assert all(command[-2:] == ("--format", "json") for command in runner.commands)
    with sqlite3.connect(_settings(tmp_path).lock_db_path) as connection:
        row = connection.execute(
            """
            SELECT owner, version, name
            FROM schema_migrations
            WHERE owner = 'operations.feishu_write_lock'
            """
        ).fetchone()
    assert row is not None
    assert tuple(row) == (
        "operations.feishu_write_lock",
        1,
        "feishu_write_lock",
    )


def test_event_write_rejects_duplicate_business_key(tmp_path: Path) -> None:
    class DuplicateRunner:
        def __call__(self, command: Sequence[str]) -> Mapping[str, object]:
            del command
            return {
                "ok": True,
                "data": {
                    "fields": ["事件ID", "操作"],
                    "data": [["event-1", "买入"], ["event-1", "买入"]],
                    "record_id_list": ["rec-1", "rec-2"],
                },
            }

    with pytest.raises(FeishuBaseDuplicateError):
        FeishuBaseEventClient(
            _settings(tmp_path), runner=DuplicateRunner()
        ).record_event(InvestmentTable.TRADE, "event-1", {"操作": "买入"})


def test_settings_are_optional_only_when_completely_unconfigured() -> None:
    assert FeishuBaseSettings.from_environment({}) is None
    settings = FeishuBaseSettings.from_environment(
        {
            "FEISHU_INVESTMENT_BASE_TOKEN": "base-token",
            "FEISHU_INVESTMENT_SELECTION_TABLE_ID": "tbl-selection",
            "FEISHU_INVESTMENT_MONITOR_TABLE_ID": "tbl-monitor",
            "FEISHU_INVESTMENT_TRADE_TABLE_ID": "tbl-trade",
            "FEISHU_INVESTMENT_REVIEW_TABLE_ID": "tbl-review",
        }
    )
    assert settings is not None
    assert settings.trade.event_id_field == "运行ID"
    with pytest.raises(RuntimeError, match="incomplete dedicated"):
        FeishuBaseSettings.from_environment(
            {"FEISHU_INVESTMENT_BASE_TOKEN": "base-token"}
        )


def test_legacy_configuration_is_rejected() -> None:
    with pytest.raises(RuntimeError, match="legacy"):
        FeishuBaseSettings.from_environment({"FEISHU_BASE_TOKEN": "old-base"})
