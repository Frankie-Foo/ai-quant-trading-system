from __future__ import annotations

import json
import subprocess
import traceback
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from operations.feishu_base import (
    FeishuBaseDuplicateError,
    FeishuBaseError,
    FeishuBaseEventClient,
    FeishuBaseSettings,
    FeishuTableSettings,
    InvestmentTable,
    _cell_text,
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


def test_readonly_access_check_never_upserts(tmp_path: Path) -> None:
    runner = FakeLark()
    checked = FeishuBaseEventClient(_settings(tmp_path), runner=runner).check_access()

    assert checked == {
        "selection": "tbl-selection",
        "monitor": "tbl-monitor",
        "trade": "tbl-trade",
        "review": "tbl-review",
    }
    assert len(runner.commands) == 4
    assert all("+record-list" in command for command in runner.commands)


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


def test_cell_text_normalizes_feishu_millisecond_timestamps() -> None:
    assert _cell_text(1786449600000) == "2026-08-11T12:00:00+00:00"


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


@pytest.mark.parametrize("output", [
    '{"ok":false,"error":{"type":"rate_limit","code":99991400,"message":"SECRET"}}',
    '{"ok":false,"error":{"type":"SECRET","code":"SECRET"}}',
    'SECRET not-json',
    '["SECRET"]',
    '[' * 2000 + '"SECRET"' + ']' * 2000,
], ids=["rate-limit", "hostile-fields", "not-json", "not-object", "deep-json"])
def test_cli_failures_are_structured_bounded_and_do_not_leak_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output: str,
) -> None:
    calls = 0

    def fail(command: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(command, 1, output, "SECRET stderr")

    monkeypatch.setattr(subprocess, "run", fail)
    client = FeishuBaseEventClient(_settings(tmp_path), sleep=lambda _: None)
    with pytest.raises(FeishuBaseError) as caught:
        client.check_access()
    message = str(caught.value)
    assert "type=" in message and "code=" in message and "exit_code=1" in message
    assert "SECRET" not in "".join(traceback.format_exception(caught.value))
    assert 1 <= calls <= 3
    if "rate_limit" in output:
        assert "type=rate_limit" in message and "code=99991400" in message


def test_readonly_cli_timeout_retries_are_bounded_and_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def timeout(command: Sequence[str], **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        raise subprocess.TimeoutExpired([*command, "SECRET"], 45, "SECRET", "SECRET")

    monkeypatch.setattr(subprocess, "run", timeout)
    client = FeishuBaseEventClient(_settings(tmp_path), sleep=lambda _: None)
    with pytest.raises(FeishuBaseError) as caught:
        client.check_access()
    assert calls == 3
    assert "type=timeout" in str(caught.value)
    assert "SECRET" not in "".join(traceback.format_exception(caught.value))


def test_timed_out_write_is_reconciled_by_readback_without_second_upsert(tmp_path: Path) -> None:
    class AcceptedButTimedOut(FakeLark):
        def __call__(self, command: Sequence[str]) -> Mapping[str, object]:
            response = super().__call__(command)
            if "+record-upsert" in command:
                raise subprocess.TimeoutExpired(["SECRET"], 45, "SECRET")
            return response

    runner = AcceptedButTimedOut()
    client = FeishuBaseEventClient(_settings(tmp_path), runner=runner, sleep=lambda _: None)
    assert client.record_event(
        InvestmentTable.TRADE, "event-1", {"操作": "买入", "数量": 1},
    ) == "rec-created"
    assert sum("+record-upsert" in command for command in runner.commands) == 1


def test_timed_out_json_write_removes_temporary_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    runner = FakeLark()

    def timeout(command: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "+record-list" in command:
            return subprocess.CompletedProcess(command, 0, json.dumps(runner(command)), "")
        raise subprocess.TimeoutExpired(["SECRET"], 45, "SECRET")

    monkeypatch.setattr(subprocess, "run", timeout)
    client = FeishuBaseEventClient(_settings(tmp_path), sleep=lambda _: None)
    with pytest.raises(FeishuBaseError) as caught:
        client.record_event(InvestmentTable.TRADE, "event-1", {"操作": "买入", "数量": 1})
    assert "type=timeout" in str(caught.value)
    assert "SECRET" not in "".join(traceback.format_exception(caught.value))
    assert list(tmp_path.glob(".lark-json-*")) == []
