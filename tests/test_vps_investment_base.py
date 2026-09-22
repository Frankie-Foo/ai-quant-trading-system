from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from operations.feishu_base import FeishuBaseError, FeishuBaseEventClient, InvestmentTable
from operations.vps_investment_base import VpsInvestmentClient, VpsInvestmentSettings


def binding(tmp_path: Path) -> tuple[VpsInvestmentSettings, dict[str, str]]:
    data = {
        "doc_id": str(uuid4()),
        "tables": {
            table.value: {
                "table_id": str(uuid4()),
                **{name: str(uuid4()) for name in ("event_id", "payload", "symbol", "summary")},
            }
            for table in InvestmentTable
        },
    }
    path = tmp_path / "binding.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    environment = {
        "AI_QUANT_INVESTMENT_PROVIDER": "vps-work",
        "VPS_INVESTMENT_BINDING_FILE": str(path),
        "VPS_INVESTMENT_BINDING_SHA256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "VPS_INVESTMENT_STATE_DB": str(tmp_path / "vps.sqlite3"),
        "FEISHU_BASE_TOKEN": "disconnected-must-never-be-used",
    }
    return VpsInvestmentSettings.from_environment(environment), environment


class Runner:
    def __init__(self, settings: VpsInvestmentSettings) -> None:
        self.settings = settings
        self.rows: list[dict[str, object]] = []
        self.writes = 0
        self.timeout = False
        self.persist = True
        self.total_override: int | None = None

    def __call__(self, arguments: tuple[str, ...], stdin: str | None) -> dict[str, object]:
        if "+base-schema" in arguments:
            return {
                "base": {
                    "docId": self.settings.doc_id,
                    "tables": [
                        {
                            "id": table["table_id"],
                            "fields": [
                                {
                                    "id": value,
                                    "type": "longText"
                                    if key in {"payload", "summary"}
                                    else "singleLineText",
                                }
                                for key, value in table.items()
                                if key != "table_id"
                            ],
                        }
                        for table in self.settings.tables.values()
                    ],
                }
            }
        if "+base-create-records" in arguments:
            self.writes += 1
            assert stdin is not None
            if self.persist:
                self.rows.append({"id": "record-one", **json.loads(stdin)[0]})
            if self.timeout:
                raise FeishuBaseError("vps-work failed: timeout")
            return {"records": self.rows}
        assert "+base-records" in arguments
        return {"records": self.rows, "total": self.total_override or len(self.rows)}


def test_factory_uses_vps_without_reading_feishu(tmp_path: Path) -> None:
    _, env = binding(tmp_path)
    assert isinstance(FeishuBaseEventClient.from_environment(env), VpsInvestmentClient)
    with pytest.raises(ValueError, match="provider"):
        FeishuBaseEventClient.from_environment({"AI_QUANT_INVESTMENT_PROVIDER": "typo"})


def test_binding_rejects_changed_file_and_reused_tables(tmp_path: Path) -> None:
    _, env = binding(tmp_path)
    path = Path(env["VPS_INVESTMENT_BINDING_FILE"])
    path.write_text(path.read_text() + " ")
    with pytest.raises(ValueError, match="fingerprint"):
        VpsInvestmentSettings.from_environment(env)
    data = json.loads(path.read_text())
    data["tables"]["trade"] = data["tables"]["selection"]
    path.write_text(json.dumps(data))
    env["VPS_INVESTMENT_BINDING_SHA256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="distinct"):
        VpsInvestmentSettings.from_environment(env)


def test_write_readback_replay_and_payload_conflict(tmp_path: Path) -> None:
    settings, _ = binding(tmp_path)
    runner = Runner(settings)
    client = VpsInvestmentClient(settings, runner=runner, sleep=lambda _: None)
    assert len(client.check_access()) == 4
    fields = {"股票代码": "TEST", "执行摘要": "仅单元测试"}
    first = client.record_event(InvestmentTable.SELECTION, "event1", fields)
    assert first.startswith("vps-work:")
    assert client.record_event(InvestmentTable.SELECTION, "event1", fields) == first
    assert runner.writes == 1
    with pytest.raises(FeishuBaseError, match="mismatch|conflict"):
        client.record_event(InvestmentTable.SELECTION, "event1", {"股票代码": "OTHER"})
    assert runner.writes == 1


@pytest.mark.parametrize("persist", [True, False])
def test_timeout_never_blindly_retries_after_restart(tmp_path: Path, persist: bool) -> None:
    settings, _ = binding(tmp_path)
    runner = Runner(settings)
    runner.timeout, runner.persist = True, persist
    client = VpsInvestmentClient(settings, runner=runner, sleep=lambda _: None)
    if persist:
        receipt = client.record_event(InvestmentTable.TRADE, "event1", {"状态": "测试"})
        assert receipt.startswith("vps-work:")
    else:
        with pytest.raises(FeishuBaseError):
            client.record_event(InvestmentTable.TRADE, "event1", {"状态": "测试"})
        restarted = VpsInvestmentClient(settings, runner=runner, sleep=lambda _: None)
        with pytest.raises(FeishuBaseError, match="uncertain"):
            restarted.record_event(InvestmentTable.TRADE, "event1", {"状态": "测试"})
    assert runner.writes == 1


def test_duplicate_truncated_and_corrupted_rows_fail_closed(tmp_path: Path) -> None:
    settings, _ = binding(tmp_path)
    runner = Runner(settings)
    client = VpsInvestmentClient(settings, runner=runner, sleep=lambda _: None)
    client.record_event(InvestmentTable.REVIEW, "event1", {"执行摘要": "测试"})
    runner.rows.append(dict(runner.rows[0]))
    with pytest.raises(FeishuBaseError, match="duplicate"):
        client.record_event(InvestmentTable.REVIEW, "event1", {"执行摘要": "测试"})
    runner.rows.pop()
    runner.total_override = 201
    with pytest.raises(FeishuBaseError, match="truncated"):
        client.record_event(InvestmentTable.REVIEW, "event1", {"执行摘要": "测试"})
    runner.total_override = None
    runner.rows[0]["fields"] = {}
    with pytest.raises(FeishuBaseError):
        client.record_event(InvestmentTable.REVIEW, "event1", {"执行摘要": "测试"})
    assert runner.writes == 1


@pytest.mark.parametrize("column", ["symbol", "summary"])
def test_replay_rejects_corrupt_projection_cell(tmp_path: Path, column: str) -> None:
    settings, _ = binding(tmp_path)
    runner = Runner(settings)
    client = VpsInvestmentClient(settings, runner=runner, sleep=lambda _: None)
    fields = {"股票代码": "TEST", "执行摘要": "测试摘要"}
    client.record_event(InvestmentTable.SELECTION, "event1", fields)
    column_id = settings.tables["selection"][column]
    remote_fields = runner.rows[0]["fields"]
    assert isinstance(remote_fields, dict)
    remote_fields[column_id] = "已损坏"
    with pytest.raises(FeishuBaseError, match="record mismatch"):
        client.record_event(InvestmentTable.SELECTION, "event1", fields)
    assert runner.writes == 1


def test_healthcheck_rejects_missing_field(tmp_path: Path) -> None:
    settings, _ = binding(tmp_path)
    client = VpsInvestmentClient(
        settings,
        runner=lambda *_: {"base": {"docId": settings.doc_id, "tables": []}},
        sleep=lambda _: None,
    )
    with pytest.raises(FeishuBaseError, match="schema"):
        client.check_access()


def test_cli_stdin_hidden_and_error_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, _ = binding(tmp_path)
    client = VpsInvestmentClient(settings, command=("node", "vps-work.cjs"))

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert kwargs["shell"] is False
        assert kwargs["input"] == "[]"
        assert "creationflags" in kwargs
        return subprocess.CompletedProcess([], 1, "", "TOP_SECRET_TOKEN")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(FeishuBaseError) as error:
        client._call(("+base-create-records",), "[]")
    assert "TOP_SECRET_TOKEN" not in str(error.value)
