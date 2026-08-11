from __future__ import annotations

import json
import subprocess
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
    client = FeishuBaseEventClient(_settings(tmp_path), runner=runner, sleep=lambda _: None)

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
        FeishuBaseEventClient(_settings(tmp_path), runner=DuplicateRunner()).record_event(
            InvestmentTable.TRADE, "event-1", {"操作": "买入"}
        )


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
        FeishuBaseSettings.from_environment({"FEISHU_INVESTMENT_BASE_TOKEN": "base-token"})


def test_legacy_configuration_is_rejected() -> None:
    with pytest.raises(RuntimeError, match="legacy"):
        FeishuBaseSettings.from_environment({"FEISHU_BASE_TOKEN": "old-base"})


def test_subprocess_json_payload_uses_utf8_file_for_windows_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(
        command: Sequence[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        del kwargs
        json_arg = command[command.index("--json") + 1]
        assert json_arg.startswith("@")
        payload_path = Path(json_arg[1:])
        captured["exists_during_call"] = payload_path.exists()
        captured["payload"] = json.loads(payload_path.read_text(encoding="utf-8"))
        return subprocess.CompletedProcess(command, 0, '{"ok":true}', "")

    monkeypatch.setattr("operations.feishu_base.subprocess.run", fake_run)
    client = FeishuBaseEventClient(_settings(tmp_path))

    assert client._run_subprocess(("base", "+record-upsert", "--json", '{"运行ID":"事件-1"}')) == {
        "ok": True
    }
    assert captured == {
        "exists_during_call": True,
        "payload": {"运行ID": "事件-1"},
    }
