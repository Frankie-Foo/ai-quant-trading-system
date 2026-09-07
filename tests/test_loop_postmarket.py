"""Offline scheduler receipts; never initialize production services."""

from datetime import date
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from operations.feishu_base import FeishuBaseEventClient
from schedule import postmarket
from schedule.child_process import ChildProcessResult
from schedule.runtime import JsonEventLogger
from schedule.state import JobLedger


@pytest.mark.parametrize("with_provider", [False, True, "native"])
def test_completed_local_review_retries_blocked_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    with_provider: bool | str,
) -> None:
    target = date(2026, 7, 20)
    stream = StringIO()
    monkeypatch.setattr(postmarket, "JsonEventLogger", lambda **kw: JsonEventLogger(
        stream=stream, **kw,
    ))
    ledger = JobLedger(tmp_path / "jobs.sqlite3")
    lease = ledger.acquire(postmarket.JOB_NAME, target, postmarket.JOB_VERSION)
    assert lease is not None
    ledger.complete(lease, artifact_ids=("signal", "episode", "review", "opportunity"))
    monkeypatch.setattr(postmarket, "load_project_env", lambda *_: None)
    monkeypatch.setattr(postmarket, "load_config", lambda *_: SimpleNamespace(
        market_data=SimpleNamespace(postmarket_data_grace_minutes=20),
        scheduler=SimpleNamespace(postmarket_max_attempts=5, postmarket_retry_minutes=30),
    ))
    monkeypatch.setattr(FeishuBaseEventClient, "from_environment", lambda *_: None)
    monkeypatch.setenv("AI_QUANT_LOOP_SYNC_ENABLED", "true")
    monkeypatch.setenv("AI_QUANT_LOOP_BINDING_FILE", "fixture-binding.json")
    monkeypatch.setenv("AI_QUANT_ACTIVE_POLICY_FILE", "fixture-active.json")
    monkeypatch.delenv("AI_QUANT_LOOP_OUTCOME_SYNC_ENABLED", raising=False)
    monkeypatch.delenv("AI_QUANT_LOOP_EXECUTION_INDEX_FILE", raising=False)
    monkeypatch.delenv("AI_QUANT_LOOP_EXECUTION_INDEX_SHA256", raising=False)
    monkeypatch.delenv("AI_QUANT_LOOP_NATIVE_RUN_ROOT", raising=False)
    if with_provider == "native":
        monkeypatch.setenv("AI_QUANT_LOOP_NATIVE_RUN_ROOT", str(tmp_path / "runs"))
        monkeypatch.delenv("AI_QUANT_LOOP_PROVIDER_CONFIG_FILE", raising=False)
        monkeypatch.delenv("AI_QUANT_LOOP_PROVIDER_CONFIG_SHA256", raising=False)
    elif with_provider:
        monkeypatch.setenv("AI_QUANT_LOOP_PROVIDER_CONFIG_FILE", "fixture-provider.json")
        monkeypatch.setenv("AI_QUANT_LOOP_PROVIDER_CONFIG_SHA256", "a" * 64)
    else:
        monkeypatch.delenv("AI_QUANT_LOOP_PROVIDER_CONFIG_FILE", raising=False)
        monkeypatch.delenv("AI_QUANT_LOOP_PROVIDER_CONFIG_SHA256", raising=False)
    responses = iter([
        '{"status":"blocked_precondition","event_id":"fixture"}',
        '{"status":"delivered","task_id":"task","run_id":"run"}',
    ])

    prepared: list[bool] = []

    def child(command: list[str], **kwargs: object) -> ChildProcessResult:
        if "scripts.prepare_loop_execution" in command or "scripts.produce_loop_daily" in command:
            prepared.append(True)
            return ChildProcessResult(return_code=0, stdout=(
                '{"status":"prepared","execution_index_path":"fixture-index.json",'
                '"execution_index_sha256":"' + "b" * 64 + '"}'
            ), stderr="", elapsed_ms=1)
        if with_provider:
            assert "--execution-index" in command and "fixture-index.json" in command
        return ChildProcessResult(return_code=0, stdout=next(responses), stderr="", elapsed_ms=1)

    monkeypatch.setattr(postmarket, "run_child", child)
    args = ["--trade-date", str(target), "--data-root", str(tmp_path),
            "--state-db", str(ledger.path), "--lock-file", str(tmp_path / "lock")]
    assert postmarket.run(args) == 1
    first = stream.getvalue()
    assert '"loop_review_sync_pending"' in first
    assert '"loop_review_sync_completed"' not in first
    assert postmarket.run(args) == 0
    assert '"loop_review_sync_completed"' in stream.getvalue()
    record = ledger.get(postmarket.JOB_NAME, target, postmarket.JOB_VERSION)
    assert record is not None and record.attempts == 1
    assert len(prepared) == (2 if with_provider else 0)


def test_missing_daily_plan_does_not_block_historical_outcomes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = date(2026, 7, 20)
    ledger = JobLedger(tmp_path / "jobs.sqlite3")
    lease = ledger.acquire(postmarket.JOB_NAME, target, postmarket.JOB_VERSION)
    assert lease is not None
    ledger.complete(lease, artifact_ids=("s", "e", "r", "o"))
    monkeypatch.setattr(postmarket, "load_project_env", lambda *_: None)
    monkeypatch.setattr(postmarket, "load_config", lambda *_: SimpleNamespace(
        market_data=SimpleNamespace(postmarket_data_grace_minutes=20),
        scheduler=SimpleNamespace(postmarket_max_attempts=5, postmarket_retry_minutes=30),
    ))
    monkeypatch.setattr(FeishuBaseEventClient, "from_environment", lambda *_: None)
    for key, value in {
        "AI_QUANT_LOOP_SYNC_ENABLED": "true", "AI_QUANT_LOOP_OUTCOME_SYNC_ENABLED": "true",
        "AI_QUANT_LOOP_PROVIDER_CONFIG_FILE": "missing-plan.json",
        "AI_QUANT_LOOP_PROVIDER_CONFIG_SHA256": "a" * 64,
        "AI_QUANT_LOOP_OUTCOME_CONFIG_FILE": "approved-costs.json",
        "AI_QUANT_LOOP_OUTCOME_EXECUTION_INDEX_FILE": "historical-index.json",
        "AI_QUANT_LOOP_OUTCOME_EXECUTION_INDEX_SHA256": "b" * 64,
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("AI_QUANT_LOOP_EXECUTION_INDEX_FILE", raising=False)
    monkeypatch.delenv("AI_QUANT_LOOP_EXECUTION_INDEX_SHA256", raising=False)
    outcome_calls: list[list[str]] = []

    def child(command: list[str], **kwargs: object) -> ChildProcessResult:
        if "scripts.prepare_loop_execution" in command:
            return ChildProcessResult(return_code=2, stdout='{"status":"blocked"}',
                                      stderr="", elapsed_ms=1)
        assert "scripts.sync_loop_due_outcomes" in command
        assert "historical-index.json" in command
        outcome_calls.append(command)
        return ChildProcessResult(return_code=0, stdout=(
            '{"status":"completed","due":1,"delivered":1,"pending":[]}'
        ), stderr="", elapsed_ms=1)

    monkeypatch.setattr(postmarket, "run_child", child)
    assert postmarket.run([
        "--trade-date", str(target), "--data-root", str(tmp_path),
        "--state-db", str(ledger.path), "--lock-file", str(tmp_path / "lock"),
    ]) == 1
    assert len(outcome_calls) == 1
