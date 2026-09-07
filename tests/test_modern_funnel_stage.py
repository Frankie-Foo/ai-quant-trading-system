import argparse
import io
import json
import subprocess
import sys
import time
import traceback
from contextlib import redirect_stdout
from datetime import UTC, date, datetime, timedelta, tzinfo
from pathlib import Path

import polars as pl
import pytest

from execution.alpaca_paper import BrokerOrder, PaperPosition
from kernel.strategy_policy import build_strategy_policy, write_strategy_policy
from operations.autonomous_selection_handoff import create_open_confirmation
from operations.feishu_base import FeishuBaseEventClient
from operations.paper_runtime_policy import PaperRuntimePolicy
from operations.paper_state import PaperStateStore
from schedule.modern_funnel import (
    FunnelStage,
    ProductionFunnelExecutor,
    run_tick,
)
from scripts import run_modern_funnel_stage as stage_runner
from scripts.monitor_modern_momentum_paper import (
    STRATEGY_VERSION as PAPER_STRATEGY_VERSION,
)
from scripts.monitor_modern_momentum_paper import approved_strategy_matches
from scripts.run_modern_funnel_stage import (
    _execution_summary,
    _first_wave_message,
    _open_plan_lines,
    _selection_event_fields,
    _stage_observed_at,
    _strategy_context,
    evaluate_open_confirmation,
    evaluate_second_wave,
)

NOW = datetime(2026, 8, 24, 13, 25, tzinfo=UTC)


def _clock(monkeypatch: pytest.MonkeyPatch, now: datetime) -> None:
    class Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> "Clock":
            return cls.fromtimestamp(now.timestamp(), now.astimezone(tz).tzinfo)

    monkeypatch.setattr(stage_runner, "datetime", Clock)


def _authorization(tmp_path: Path, trade_date: date) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text("{}", encoding="utf-8")
    path = tmp_path / "open_confirmation.json"
    create_open_confirmation(
        confirmation_path=path,
        config_path=plan_path,
        trade_date=trade_date,
        selection_snapshot_id="snapshot-1",
        candidate_pool=("PASS",),
        feishu_record_ids=("rec-1",),
        livermore_message_id="msg-1",
        strategy_version=stage_runner.STRATEGY_VERSION,
        generated_at_utc=datetime(
            trade_date.year,
            trade_date.month,
            trade_date.day,
            9,
            35,
            tzinfo=stage_runner.EASTERN,
        ).astimezone(UTC),
    )
    return path


def _integrated_executor(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    commands: list[list[str]],
) -> ProductionFunnelExecutor:
    """Run the real open-stage CLI in place of its subprocess; no selection prerequisites."""
    monkeypatch.setattr(stage_runner, "ROOT", root)
    monkeypatch.setattr(stage_runner, "load_project_env", lambda _: None)

    def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if "open_confirmation" not in command:
            return subprocess.CompletedProcess(
                command, 0, '{"ok": true, "receipt_id": "prerequisite"}', ""
            )
        output = io.StringIO()
        with monkeypatch.context() as scoped, redirect_stdout(output):
            scoped.setattr(sys, "argv", ["stage", *command[3:]])
            try:
                returncode = stage_runner.main()
            except Exception:
                return subprocess.CompletedProcess(
                    command, 1, output.getvalue(), traceback.format_exc()
                )
        return subprocess.CompletedProcess(command, returncode, output.getvalue(), "")

    return ProductionFunnelExecutor(root=root, runner=runner)


def _process_identity(
    monkeypatch: pytest.MonkeyPatch,
    trade_date: date,
    confirmation_path: Path,
    *,
    module: str = "scripts.monitor_modern_momentum_paper",
) -> None:
    command_line = subprocess.list2cmdline(
        [
            sys.executable,
            "-m",
            module,
            "--trade-date",
            trade_date.isoformat(),
            "--confirmation-path",
            str(confirmation_path),
            "--arm-paper",
        ]
    )
    # OS process inspection is the boundary; launcher identity matching stays real.
    monkeypatch.setattr(
        stage_runner,
        "_process_command_line",
        lambda _: command_line,
        raising=False,
    )


def _candidate(symbol: str) -> dict[str, object]:
    return {
        "symbol": symbol,
        "forward_market_cap": 2_000_000_000,
        "premarket_return": 0.05,
    }


def test_first_wave_message_is_short_chinese_and_human_readable() -> None:
    candidates = [
        {
            "symbol": "ZM",
            "catalyst_categories": ["earnings"],
            "rvol": 4.3930717568,
            "premarket_return": 0.0160093918,
        },
        {
            "symbol": "COIN",
            "catalyst_categories": ["general_news"],
            "rvol": 3.1712339874,
            "premarket_return": 0.0111224545,
        },
        {
            "symbol": "AAOI",
            "catalyst_categories": [
                "contract_partnership",
                "financing_dilution",
            ],
            "rvol": 1.7501167031,
            "premarket_return": -0.0040455581,
        },
    ]

    assert _first_wave_message(candidates) == (
        "第一波观察池：\n\n"
        "1. ZM：财报，RVOL 4.39，盘前 +1.60%\n"
        "2. COIN：加密行业消息，RVOL 3.17，盘前 +1.11%\n"
        "3. AAOI：合同/融资，RVOL 1.75，盘前 -0.40%"
    )


def test_second_wave_keeps_only_liquid_tight_names_above_vwap() -> None:
    bars = pl.DataFrame(
        {
            "symbol": ["GOOD", "GOOD", "WIDE"],
            "ts_utc": [NOW - timedelta(minutes=2), NOW - timedelta(minutes=1), NOW],
            "close": [10.0, 10.2, 10.0],
            "volume": [100_000, 100_000, 200_000],
        }
    )
    quotes = pl.DataFrame(
        {
            "symbol": ["GOOD", "WIDE"],
            "ts_utc": [NOW, NOW],
            "bid_price": [10.19, 9.90],
            "ask_price": [10.20, 10.10],
        }
    )

    kept, rejected = evaluate_second_wave([_candidate("GOOD"), _candidate("WIDE")], bars, quotes)

    assert [row["symbol"] for row in kept] == ["GOOD"]
    assert [row["symbol"] for row in rejected] == ["WIDE"]
    assert "点差" in str(rejected[0]["reasons"])


def test_second_wave_allows_a_point_two_percent_observation_spread() -> None:
    bars = pl.DataFrame(
        {
            "symbol": ["WATCH", "WATCH"],
            "ts_utc": [NOW - timedelta(minutes=2), NOW - timedelta(minutes=1)],
            "close": [10.0, 10.2],
            "volume": [100_000, 100_000],
        }
    )
    quotes = pl.DataFrame(
        {
            "symbol": ["WATCH"],
            "ts_utc": [NOW],
            "bid_price": [10.18],
            "ask_price": [10.20],
        }
    )

    kept, rejected = evaluate_second_wave([_candidate("WATCH")], bars, quotes)

    assert [row["symbol"] for row in kept] == ["WATCH"]
    assert rejected == []


def test_second_wave_keeps_soft_vwap_and_spread_warnings_for_open_review() -> None:
    bars = pl.DataFrame(
        {
            "symbol": ["WATCH", "WATCH"],
            "ts_utc": [NOW - timedelta(minutes=2), NOW - timedelta(minutes=1)],
            "close": [10.2, 10.0],
            "volume": [100_000, 100_000],
        }
    )
    quotes = pl.DataFrame(
        {
            "symbol": ["WATCH"],
            "ts_utc": [NOW],
            "bid_price": [9.96],
            "ask_price": [10.04],
        }
    )

    kept, rejected = evaluate_second_wave([_candidate("WATCH")], bars, quotes)

    assert [row["symbol"] for row in kept] == ["WATCH"]
    assert rejected == []
    assert kept[0]["watch_reasons"] == [
        "盘前点差0.80%偏宽，09:35及入场前复核",
        "暂未站上盘前VWAP，等待开盘确认",
    ]


def test_second_wave_rejects_non_finite_market_facts() -> None:
    bars = pl.DataFrame(
        {
            "symbol": ["BAD"],
            "ts_utc": [NOW],
            "close": [float("nan")],
            "volume": [200_000],
        }
    )
    quotes = pl.DataFrame(
        {
            "symbol": ["BAD"],
            "ts_utc": [NOW],
            "bid_price": [10.0],
            "ask_price": [10.01],
        }
    )

    kept, rejected = evaluate_second_wave([_candidate("BAD")], bars, quotes)

    assert kept == []
    assert rejected[0]["reasons"] == ["Alpaca SIP盘前行情数值无效"]


def test_second_wave_rejects_null_in_any_vwap_input_bar() -> None:
    bars = pl.DataFrame(
        {
            "symbol": ["BAD", "BAD"],
            "ts_utc": [NOW - timedelta(minutes=1), NOW],
            "close": [None, 10.2],
            "volume": [200_000, 200_000],
        }
    )
    quotes = pl.DataFrame(
        {
            "symbol": ["BAD"],
            "ts_utc": [NOW],
            "bid_price": [10.19],
            "ask_price": [10.20],
        }
    )

    kept, rejected = evaluate_second_wave([_candidate("BAD")], bars, quotes)

    assert kept == []
    assert rejected[0]["reasons"] == ["Alpaca SIP盘前行情数值无效"]


def test_second_wave_capacity_rejection_has_an_explicit_ranking_reason() -> None:
    symbols = [f"S{i}" for i in range(7)]
    bars = pl.DataFrame(
        {
            "symbol": symbols,
            "ts_utc": [NOW] * 7,
            "close": [10.0] * 7,
            "volume": [200_000] * 7,
        }
    )
    quotes = pl.DataFrame(
        {
            "symbol": symbols,
            "ts_utc": [NOW] * 7,
            "bid_price": [9.995] * 7,
            "ask_price": [10.005] * 7,
        }
    )

    kept, rejected = evaluate_second_wave([_candidate(s) for s in symbols], bars, quotes)

    assert len(kept) == 6
    assert len(rejected) == 1
    assert rejected[0]["reasons"] == ["观察池容量落选（排序第7，上限6只）"]


def test_strategy_context_builds_a_non_executable_challenger_subset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 8, 27, 1, 0, tzinfo=UTC)
    active_path = tmp_path / "active.json"
    challenger_path = tmp_path / "challenger.json"
    active = build_strategy_policy(
        version="selection-baseline",
        status="active",
        min_rvol=3.0,
        created_at_utc=now,
        approved_by="owner",
        approved_at_utc=now,
    )
    challenger = build_strategy_policy(
        version="challenger-202609-test",
        status="shadow",
        min_rvol=4.0,
        created_at_utc=now,
        previous_version=active.version,
        source_snapshot_ids=("sandbox",),
    )
    write_strategy_policy(active_path, active)
    write_strategy_policy(challenger_path, challenger)
    monkeypatch.setenv("AI_QUANT_ACTIVE_POLICY_FILE", str(active_path))
    monkeypatch.setenv("AI_QUANT_CHALLENGER_POLICY_FILE", str(challenger_path))

    context = _strategy_context(
        [
            {"symbol": "KEEP", "rvol": 4.5},
            {"symbol": "DROP", "rvol": 3.5},
        ]
    )

    challenger_context = context["challenger"]
    assert isinstance(challenger_context, dict)
    assert context["active_version"] == PAPER_STRATEGY_VERSION == "modern-h15.v1"
    assert context["legacy_version"] == active.version
    assert context["legacy_policy_role"] == "selection_data_lineage_only"
    manifest = context["modern_strategy_manifest"]
    assert isinstance(manifest, dict)
    assert manifest["effective_config"]["minimum_premarket_rvol"] == 1.5
    assert challenger_context["symbols"] == ["KEEP"]
    assert challenger_context["execution_eligible"] is False

    plan = stage_runner._plan_payload(
        now.date(),
        [_candidate("KEEP")],
        strategy_version=str(context["active_version"]),
    )
    assert plan["modern_strategy_manifest"] == manifest
    assert approved_strategy_matches(plan)
    plan_path = tmp_path / "modern-plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    authorization = create_open_confirmation(
        confirmation_path=tmp_path / "open_confirmation.json",
        config_path=plan_path,
        trade_date=now.date(),
        selection_snapshot_id="snapshot-1",
        candidate_pool=("KEEP",),
        feishu_record_ids=("rec-1",),
        livermore_message_id="msg-1",
        strategy_version=str(plan["strategy_version"]),
        generated_at_utc=now,
    )
    assert authorization.strategy_version == "modern-h15.v1"
    PaperRuntimePolicy().validate_arming(
        trade_date=now.date(),
        broker_write_enabled=True,
        trading_kill_switch=False,
        broker_base_url="https://paper-api.alpaca.markets",
        authorization=authorization,
        expected_candidate_pool=("KEEP",),
        expected_strategy_version=PAPER_STRATEGY_VERSION,
    )


def test_open_confirmation_requires_complete_positive_accepted_five_minutes() -> None:
    rows: list[dict[str, object]] = []
    for index in range(5):
        rows.append(
            {
                "symbol": "PASS",
                "ts_utc": NOW + timedelta(minutes=index),
                "open": 10.0 + index * 0.05,
                "high": 10.2 + index * 0.05,
                "low": 9.95 + index * 0.05,
                "close": 10.15 + index * 0.05,
                "volume": 100_000,
            }
        )
        rows.append(
            {
                "symbol": "FAIL",
                "ts_utc": NOW + timedelta(minutes=index),
                "open": 10.0 - index * 0.05,
                "high": 10.05 - index * 0.05,
                "low": 9.8 - index * 0.05,
                "close": 9.85 - index * 0.05,
                "volume": 100_000,
            }
        )

    kept, rejected = evaluate_open_confirmation(
        [_candidate("PASS"), _candidate("FAIL")], pl.DataFrame(rows)
    )

    assert [row["symbol"] for row in kept] == ["PASS"]
    assert [row["symbol"] for row in rejected] == ["FAIL"]
    assert rejected[0]["reasons"]


def test_open_plan_contains_every_execution_gate_in_chinese() -> None:
    body = "\n".join(_open_plan_lines([_candidate("PASS")]))

    for required in (
        "09:56 ET后",
        "H15",
        "VWAP",
        "0.25%",
        "全包止损不超过2%",
        "3R",
        "15:00后禁止新仓",
        "15:50前全部清仓",
        "单票0.5%",
        "首次/二次尝试60%/40%",
    ):
        assert required in body


def test_open_stage_feishu_summary_contains_complete_plan() -> None:
    summary = _execution_summary(
        FunnelStage.OPEN_CONFIRMATION,
        _candidate("PASS"),
    )

    assert "PASS 预案" in summary
    assert "H15" in summary
    assert "0.25%" in summary
    assert "全包止损不超过2%" in summary
    assert "3R" in summary
    assert "15:50前全部清仓" in summary


def test_rejected_name_is_a_feishu_state_transition_with_reason() -> None:
    fields = _selection_event_fields(
        trade_date=date(2026, 8, 24),
        stage=FunnelStage.OPEN_CONFIRMATION,
        row={"symbol": "FAIL", "reasons": ["跌破VWAP", "量能不足"]},
        kept=False,
        observed_at_utc=NOW,
    )

    assert fields["模拟动作"] == "不操作"
    assert fields["状态"] == "已失效"
    assert fields["触发理由"] == "跌破VWAP、量能不足"
    assert fields["下一动作"] == "已剔除；纳入无成交复盘"


def test_stage_event_time_is_deterministic_and_dst_aware() -> None:
    assert _stage_observed_at(date(2026, 8, 24), FunnelStage.FIRST_WAVE) == datetime(
        2026, 8, 24, 12, 0, tzinfo=UTC
    )
    assert _stage_observed_at(date(2026, 8, 24), FunnelStage.OPEN_CONFIRMATION) == datetime(
        2026, 8, 24, 13, 35, tzinfo=UTC
    )


def test_paper_plan_uses_the_execution_strategy_version() -> None:
    plan = stage_runner._plan_payload(
        date(2026, 8, 24),
        [_candidate("PASS")],
    )

    assert plan["strategy_version"] == stage_runner.STRATEGY_VERSION
    manifest = plan["modern_strategy_manifest"]
    assert isinstance(manifest, dict)
    assert manifest["effective_config"]["minimum_premarket_rvol"] == 1.5
    assert len(manifest["config_sha256"]) == 64


def test_first_wave_retry_reuses_frozen_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    day_root = tmp_path / "2026-08-24"
    day_root.mkdir()
    path = day_root / "first_wave_pool.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "modern_funnel.first_wave.v1",
                "trade_date": "2026-08-24",
                "generated_at_utc": "2026-08-24T12:00:00+00:00",
                "candidates": [_candidate("PASS")],
            }
        ),
        encoding="utf-8",
    )
    args = argparse.Namespace(
        trade_date=date(2026, 8, 24),
        data_root=tmp_path / "data",
        state_root=tmp_path,
    )

    def unexpected(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("retry must not regenerate the frozen pool")

    monkeypatch.setattr(stage_runner, "_run_module", unexpected)
    monkeypatch.setattr(stage_runner, "_latest_pool", unexpected)
    monkeypatch.setattr(stage_runner, "_publish_stage", lambda **_kwargs: (("rec-1",), "msg-1"))

    receipt = stage_runner._first_wave(args, day_root)

    assert receipt["livermore_message_id"] == "msg-1"
    assert json.loads(path.read_text(encoding="utf-8"))["generated_at_utc"] == (
        "2026-08-24T12:00:00+00:00"
    )


def test_second_wave_retry_does_not_refetch_market_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    day_root = tmp_path / "2026-08-24"
    day_root.mkdir()
    (day_root / "first_wave_pool.json").write_text(
        json.dumps({"candidates": [_candidate("PASS")]}), encoding="utf-8"
    )
    (day_root / "second_wave_pool.json").write_text(
        json.dumps(
            {
                "candidates": [_candidate("PASS")],
                "rejected": [],
                "generated_at_utc": "2026-08-24T13:25:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    args = argparse.Namespace(
        trade_date=date(2026, 8, 24),
        data_root=tmp_path / "data",
        state_root=tmp_path,
    )

    def unexpected(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("retry must not refetch market data")

    monkeypatch.setattr(stage_runner, "fetch_bars", unexpected)
    monkeypatch.setattr(stage_runner, "fetch_quotes", unexpected)
    monkeypatch.setattr(stage_runner, "_publish_stage", lambda **_kwargs: (("rec-1",), "msg-1"))

    receipt = stage_runner._second_wave(args, day_root)

    assert receipt["livermore_message_id"] == "msg-1"


def test_open_retry_reuses_frozen_no_trade_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clock(monkeypatch, datetime(2026, 8, 24, 13, 35, tzinfo=UTC))
    day_root = tmp_path / "2026-08-24"
    day_root.mkdir()
    (day_root / "second_wave_pool.json").write_text(
        json.dumps({"candidates": [_candidate("FAIL")]}), encoding="utf-8"
    )
    (day_root / "open_decision.json").write_text(
        json.dumps(
            {
                "candidates": [],
                "rejected": [{"symbol": "FAIL", "reasons": ["开盘承接失败"]}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    args = argparse.Namespace(
        trade_date=date(2026, 8, 24),
        data_root=tmp_path / "data",
        state_root=tmp_path,
    )

    def unexpected(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("retry must not refetch the opening window")

    monkeypatch.setattr(stage_runner, "fetch_bars", unexpected)
    monkeypatch.setattr(stage_runner, "_publish_stage", lambda **_kwargs: (("rec-1",), "msg-1"))

    receipt = stage_runner._open_confirmation(args, day_root)

    assert receipt["livermore_message_id"] == "msg-1"


def test_open_retry_with_authorization_does_not_recreate_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    day_root = tmp_path / "2026-08-24"
    day_root.mkdir()
    plan_path = day_root / "modern_h15_paper_plan.json"
    plan_path.write_text("{}", encoding="utf-8")
    confirmation_path = day_root / "open_confirmation.json"
    authorization = create_open_confirmation(
        confirmation_path=confirmation_path,
        config_path=plan_path,
        trade_date=date(2026, 8, 24),
        selection_snapshot_id="snapshot-1",
        candidate_pool=("PASS",),
        feishu_record_ids=("rec-1",),
        livermore_message_id="msg-1",
        strategy_version=stage_runner.STRATEGY_VERSION,
        generated_at_utc=NOW,
    )
    args = argparse.Namespace(
        trade_date=date(2026, 8, 24),
        data_root=tmp_path / "data",
        state_root=tmp_path,
    )

    def unexpected(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("authorized retry must not repeat market or snapshot work")

    monkeypatch.setattr(stage_runner, "_read_json", unexpected)
    monkeypatch.setattr(stage_runner, "fetch_bars", unexpected)
    monkeypatch.setattr(stage_runner, "_freeze_final_pool", unexpected)
    monkeypatch.setattr(stage_runner, "_launch_paper_if_confirmed", lambda *_args: 123)

    receipt = stage_runner._open_confirmation(args, day_root)

    assert receipt["authorization_id"] == authorization.open_confirmation_id
    assert receipt["paper_pid"] == "123"


def test_paper_launcher_reuses_active_monitor_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trade_date = date(2026, 9, 3)
    now = datetime(2026, 9, 3, 14, 0, tzinfo=UTC)
    confirmation_path = _authorization(tmp_path, trade_date)
    run_dir = tmp_path / "runs" / "modern-momentum" / trade_date.isoformat()
    store = PaperStateStore(run_dir / "paper-state.sqlite3")
    assert store.claim_run(trade_date, owner="pid-456", observed_at_utc=now)
    monkeypatch.setattr(stage_runner, "ROOT", tmp_path)
    monkeypatch.setenv("AI_QUANT_PAPER_RUNTIME_CONFIRMED", "true")
    monkeypatch.setenv("AI_QUANT_PAPER_SMOKE_MAX_NOTIONAL", "100")

    def unexpected(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("active monitor must not be launched again")

    monkeypatch.setattr(subprocess, "Popen", unexpected)
    _clock(monkeypatch, now)
    monkeypatch.setattr(stage_runner, "_process_running", lambda _: True, raising=False)
    _process_identity(monkeypatch, trade_date, confirmation_path)

    assert stage_runner._launch_paper_if_confirmed(trade_date, confirmation_path) == 456


def test_scheduler_blocks_live_monitor_with_expired_lease_without_relaunch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trade_date = date(2026, 9, 3)
    now = datetime(2026, 9, 3, 13, 35, tzinfo=UTC)
    confirmation_path = _authorization(
        tmp_path / "runs" / "autonomous" / trade_date.isoformat(),
        trade_date,
    )
    store = PaperStateStore(
        tmp_path / "runs" / "modern-momentum" / trade_date.isoformat() / "paper-state.sqlite3"
    )
    assert store.claim_run(trade_date, owner="pid-456", observed_at_utc=now - timedelta(minutes=1))
    _clock(monkeypatch, now)
    monkeypatch.setenv("AI_QUANT_PAPER_RUNTIME_CONFIRMED", "true")
    monkeypatch.setenv("AI_QUANT_PAPER_SMOKE_MAX_NOTIONAL", "100")
    monkeypatch.setattr(stage_runner, "_process_running", lambda _: True)
    _process_identity(monkeypatch, trade_date, confirmation_path)
    effects: list[str] = []

    def unexpected(*_args: object, **_kwargs: object) -> None:
        effects.append("broker_or_launch")
        raise AssertionError("live monitor must not be relaunched or contact broker")

    monkeypatch.setattr(stage_runner, "DirectAlpacaPaperBroker", unexpected)
    monkeypatch.setattr(subprocess, "Popen", unexpected)
    commands: list[list[str]] = []
    executor = _integrated_executor(tmp_path, monkeypatch, commands)
    ledger = tmp_path / "funnel.sqlite3"
    for prior in (now.replace(hour=12, minute=0), now.replace(minute=25)):
        run_tick(ledger_path=ledger, executor=executor, now_utc=prior)
    result = run_tick(ledger_path=ledger, executor=executor, now_utc=now)

    assert result.status.value == "blocked"
    assert "lease_expired" in result.detail
    assert effects == []


@pytest.mark.parametrize("expired", [False, True])
def test_scheduler_blocks_reused_pid_with_wrong_monitor_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    expired: bool,
) -> None:
    trade_date = date(2026, 9, 3)
    now = datetime(2026, 9, 3, 13, 35, tzinfo=UTC)
    confirmation_path = _authorization(
        tmp_path / "runs" / "autonomous" / trade_date.isoformat(),
        trade_date,
    )
    store = PaperStateStore(
        tmp_path / "runs" / "modern-momentum" / trade_date.isoformat() / "paper-state.sqlite3"
    )
    assert store.claim_run(
        trade_date,
        owner="pid-456",
        observed_at_utc=now - timedelta(minutes=1) if expired else now,
    )
    _clock(monkeypatch, now)
    monkeypatch.setenv("AI_QUANT_PAPER_RUNTIME_CONFIRMED", "true")
    monkeypatch.setenv("AI_QUANT_PAPER_SMOKE_MAX_NOTIONAL", "100")
    monkeypatch.setattr(stage_runner, "_process_running", lambda _: True)
    _process_identity(
        monkeypatch,
        trade_date,
        confirmation_path,
        module="scripts.unrelated_monitor",
    )
    commands: list[list[str]] = []
    executor = _integrated_executor(tmp_path, monkeypatch, commands)
    ledger = tmp_path / "funnel.sqlite3"
    for prior in (now.replace(hour=12, minute=0), now.replace(minute=25)):
        run_tick(ledger_path=ledger, executor=executor, now_utc=prior)

    result = run_tick(ledger_path=ledger, executor=executor, now_utc=now)
    assert result.status.value == "blocked"
    assert "identity_mismatch" in result.detail


def test_resume_only_cannot_create_a_missing_open_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = argparse.Namespace(
        trade_date=date(2026, 9, 3),
        data_root=tmp_path / "data",
        state_root=tmp_path,
        resume_only=True,
    )
    with pytest.raises(RuntimeError, match="existing.*authorization"):
        stage_runner._open_confirmation(args, tmp_path / "2026-09-03")


def test_open_stage_outside_selection_window_cannot_start_a_new_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clock(monkeypatch, datetime(2026, 9, 3, 14, 0, tzinfo=UTC))
    args = argparse.Namespace(trade_date=date(2026, 9, 3), resume_only=False)
    with pytest.raises(RuntimeError, match="selection window"):
        stage_runner._open_confirmation(args, tmp_path)


@pytest.mark.parametrize("hour, day", [(20, 3), (14, 4)])
def test_paper_handoff_never_starts_at_session_close_or_on_another_day(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    hour: int,
    day: int,
) -> None:
    trade_date = date(2026, 9, 3)
    confirmation_path = _authorization(tmp_path, trade_date)
    monkeypatch.setattr(stage_runner, "ROOT", tmp_path)
    monkeypatch.setenv("AI_QUANT_PAPER_RUNTIME_CONFIRMED", "true")
    monkeypatch.setenv("AI_QUANT_PAPER_SMOKE_MAX_NOTIONAL", "100")
    _clock(monkeypatch, datetime(2026, 9, day, hour, 0, tzinfo=UTC))

    def unexpected(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("expired authorization must not launch or contact broker")

    monkeypatch.setattr(subprocess, "Popen", unexpected)
    monkeypatch.setattr(stage_runner, "DirectAlpacaPaperBroker", unexpected, raising=False)
    assert stage_runner._launch_paper_if_confirmed(trade_date, confirmation_path) is None


@pytest.mark.parametrize(
    "trade_date,hour",
    [(date(2026, 9, 3), 0), (date(2026, 9, 5), 10), (date(2026, 9, 7), 10)],
)
def test_launcher_never_starts_at_midnight_weekend_or_exchange_holiday(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trade_date: date,
    hour: int,
) -> None:
    confirmation_path = _authorization(tmp_path, trade_date)
    monkeypatch.setattr(stage_runner, "ROOT", tmp_path)
    monkeypatch.setenv("AI_QUANT_PAPER_RUNTIME_CONFIRMED", "true")
    monkeypatch.setenv("AI_QUANT_PAPER_SMOKE_MAX_NOTIONAL", "100")
    _clock(
        monkeypatch,
        datetime(
            trade_date.year,
            trade_date.month,
            trade_date.day,
            hour,
            tzinfo=stage_runner.EASTERN,
        ).astimezone(UTC),
    )

    def unexpected(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("out-of-session handoff must not launch or contact broker")

    monkeypatch.setattr(subprocess, "Popen", unexpected)
    monkeypatch.setattr(stage_runner, "DirectAlpacaPaperBroker", unexpected)
    assert stage_runner._launch_paper_if_confirmed(trade_date, confirmation_path) is None


@pytest.mark.parametrize(
    "trade_date,hour,minute,expected,initial_failure",
    [
        (date(2026, 9, 3), 15, 0, "monitoring", False),
        (date(2026, 9, 3), 15, 59, "monitoring", False),
        (date(2026, 9, 3), 16, 0, "not_due", False),
        (date(2026, 11, 27), 12, 59, "monitoring", False),
        (date(2026, 11, 27), 13, 0, "not_due", False),
        (date(2026, 9, 3), 15, 30, "monitoring", True),
    ],
)
def test_scheduler_launcher_recovery_uses_frozen_authorization_until_session_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trade_date: date,
    hour: int,
    minute: int,
    expected: str,
    initial_failure: bool,
) -> None:
    confirmation_path = _authorization(
        tmp_path / "runs" / "autonomous" / trade_date.isoformat(),
        trade_date,
    )
    original = confirmation_path.read_bytes()
    commands: list[list[str]] = []
    executor = _integrated_executor(tmp_path, monkeypatch, commands)
    ledger = tmp_path / "funnel.sqlite3"
    monkeypatch.setenv("AI_QUANT_PAPER_RUNTIME_CONFIRMED", "true" if initial_failure else "false")
    monkeypatch.setenv("AI_QUANT_PAPER_SMOKE_MAX_NOTIONAL", "100")
    events: list[str] = []
    fail_startup = initial_failure

    class Broker:
        def __init__(self, **kwargs: object) -> None:
            assert kwargs["writes_enabled"] is False

        def list_open_orders(self) -> tuple[()]:
            events.append("orders")
            return ()

        def list_positions(self) -> tuple[()]:
            events.append("positions")
            return ()

        def close(self) -> None:
            events.append("close")

    class Process:
        pid = 123

        def poll(self) -> int | None:
            return 7 if fail_startup else None

    def launch(command: list[str], **_kwargs: object) -> Process:
        assert events == ["orders", "positions", "close"]
        assert command[2] == "scripts.monitor_modern_momentum_paper"
        assert command[-3:] == ["--confirmation-path", str(confirmation_path), "--arm-paper"]
        events.append("launch")
        return Process()

    def unexpected(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("recovery must not fetch a new selection or republish")

    monkeypatch.setattr(stage_runner, "DirectAlpacaPaperBroker", Broker)
    monkeypatch.setattr(subprocess, "Popen", launch)
    monkeypatch.setattr(time, "sleep", lambda _: None)
    monkeypatch.setattr(stage_runner, "fetch_bars", unexpected)
    monkeypatch.setattr(FeishuBaseEventClient, "from_environment", unexpected)
    monkeypatch.setattr(stage_runner, "_process_running", lambda _: False)

    def tick(at_hour: int, at_minute: int) -> str:
        now = datetime(
            trade_date.year,
            trade_date.month,
            trade_date.day,
            at_hour,
            at_minute,
            tzinfo=stage_runner.EASTERN,
        ).astimezone(UTC)
        _clock(monkeypatch, now)
        return run_tick(ledger_path=ledger, executor=executor, now_utc=now).status.value

    tick(8, 0)
    tick(9, 25)
    assert tick(9, 35) == ("failed" if initial_failure else "handoff_pending")
    events.clear()
    fail_startup = False
    monkeypatch.setenv("AI_QUANT_PAPER_RUNTIME_CONFIRMED", "true")
    assert tick(hour, minute) == expected
    assert events == (
        ["orders", "positions", "close", "launch"] if expected == "monitoring" else []
    )
    if expected == "monitoring":
        assert "--resume-only" in commands[-1]
    assert confirmation_path.read_bytes() == original


@pytest.mark.parametrize("exit_code", [None, 0, 7])
def test_authorized_handoff_reconciles_before_launch_and_never_republishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exit_code: int | None,
) -> None:
    trade_date = date(2026, 9, 3)
    confirmation_path = _authorization(tmp_path, trade_date)
    original = confirmation_path.read_bytes()
    now = datetime(2026, 9, 3, 14, 0, tzinfo=UTC)
    _clock(monkeypatch, now)
    monkeypatch.setattr(stage_runner, "ROOT", tmp_path)
    monkeypatch.setenv("AI_QUANT_PAPER_RUNTIME_CONFIRMED", "true")
    monkeypatch.setenv("AI_QUANT_PAPER_SMOKE_MAX_NOTIONAL", "100")
    events: list[str] = []

    class Broker:
        def __init__(self, **kwargs: object) -> None:
            assert kwargs["writes_enabled"] is False

        def list_open_orders(self) -> tuple[()]:
            events.append("orders")
            return ()

        def list_positions(self) -> tuple[()]:
            events.append("positions")
            return ()

        def close(self) -> None:
            events.append("close")

    class Process:
        pid = 123

        def poll(self) -> int | None:
            return exit_code

    def launch(command: list[str], **kwargs: object) -> Process:
        assert events == ["orders", "positions", "close"]
        assert kwargs["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
        events.append("launch")
        return Process()

    def unexpected(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("authorized handoff must not select or publish again")

    monkeypatch.setattr(stage_runner, "DirectAlpacaPaperBroker", Broker, raising=False)
    monkeypatch.setattr(subprocess, "Popen", launch)
    monkeypatch.setattr(time, "sleep", lambda _: None)
    monkeypatch.setattr(stage_runner, "_process_running", lambda _: True, raising=False)
    _process_identity(monkeypatch, trade_date, confirmation_path)
    monkeypatch.setattr(stage_runner, "fetch_bars", unexpected)
    monkeypatch.setattr(stage_runner, "_publish_stage", unexpected)
    monkeypatch.setattr(stage_runner, "_freeze_final_pool", unexpected)
    args = argparse.Namespace(trade_date=trade_date, resume_only=True)
    if exit_code is not None:
        with pytest.raises(RuntimeError, match="startup.*exit code"):
            stage_runner._open_confirmation(args, tmp_path)
    else:
        receipt = stage_runner._open_confirmation(args, tmp_path)
        replay = stage_runner._open_confirmation(args, tmp_path)
        assert receipt == replay
        assert receipt["paper_started"] == "true"
        assert events.count("launch") == 1
    assert confirmation_path.read_bytes() == original


@pytest.mark.parametrize("bound,foreign", [(False, False), (True, False), (True, True)])
def test_launcher_proves_unstored_children_from_readonly_same_day_entry_parent_queries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bound: bool,
    foreign: bool,
) -> None:
    trade_date = date(2026, 9, 3)
    now = datetime(2026, 9, 3, 14, 0, tzinfo=UTC)
    confirmation_path = _authorization(
        tmp_path / "runs" / "autonomous" / trade_date.isoformat(),
        trade_date,
    )
    original_confirmation = confirmation_path.read_bytes()
    store = PaperStateStore(
        tmp_path / "runs" / "modern-momentum" / trade_date.isoformat() / "paper-state.sqlite3"
    )
    for client_id, order_day, role in (
        ("entry-today", trade_date, "entry"),
        ("not-submitted", trade_date, "entry"),
        ("entry-yesterday", trade_date - timedelta(days=1), "entry"),
        ("exit-today", trade_date, "exit"),
    ):
        store.record_order_intent(
            trade_date=order_day,
            client_order_id=client_id,
            symbol="PASS",
            attempt=1,
            role=role,
            quantity=1,
            payload={"side": "buy" if role == "entry" else "sell"},
            observed_at_utc=now,
        )
    if bound:
        store.attach_broker_order(
            client_order_id="entry-today",
            broker_order_id="parent-broker",
            status="filled",
            observed_at_utc=now,
        )
    store.save_symbol_state(
        trade_date=trade_date,
        symbol="PASS",
        state={"phase": "active", "entry_client_id": "entry-today"},
        observed_at_utc=now,
    )
    orders_before = store.list_orders()
    states_before = store.load_symbol_states(trade_date)
    child = BrokerOrder(
        id="stop-broker",
        client_order_id="stop-child",
        symbol="PASS",
        qty=1,
        filled_qty="0",
        status="new",
        side="sell",
        type="stop",
    )
    parent = BrokerOrder(
        id="parent-broker",
        client_order_id="entry-today",
        symbol="PASS",
        qty=1,
        filled_qty="1",
        status="filled",
        side="buy",
        legs=(child,),
    )
    lookups: list[str] = []
    launches: list[list[str]] = []

    class Broker:
        def __init__(self, **kwargs: object) -> None:
            assert kwargs["writes_enabled"] is False

        def get_order_by_client_id(self, client_id: str) -> BrokerOrder | None:
            lookups.append(client_id)
            return parent if client_id == "entry-today" else None

        def list_open_orders(self) -> tuple[BrokerOrder, ...]:
            if foreign:
                return (
                    child.model_copy(update={"id": "foreign-broker", "client_order_id": "foreign"}),
                )
            return (child,)

        def list_positions(self) -> tuple[PaperPosition, ...]:
            return (PaperPosition(symbol="PASS", qty="1", side="long", market_value="10"),)

        def close(self) -> None:
            pass

    class Process:
        pid = 123

        def poll(self) -> None:
            return None

    def launch(command: list[str], **_kwargs: object) -> Process:
        launches.append(command)
        return Process()

    _clock(monkeypatch, now)
    monkeypatch.setenv("AI_QUANT_PAPER_RUNTIME_CONFIRMED", "true")
    monkeypatch.setenv("AI_QUANT_PAPER_SMOKE_MAX_NOTIONAL", "100")
    monkeypatch.setattr(stage_runner, "DirectAlpacaPaperBroker", Broker)
    monkeypatch.setattr(subprocess, "Popen", launch)
    monkeypatch.setattr(time, "sleep", lambda _: None)
    commands: list[list[str]] = []
    executor = _integrated_executor(tmp_path, monkeypatch, commands)
    ledger = tmp_path / "funnel.sqlite3"
    for prior in (now.replace(hour=12, minute=0), now.replace(hour=13, minute=25)):
        run_tick(ledger_path=ledger, executor=executor, now_utc=prior)
    result = run_tick(
        ledger_path=ledger, executor=executor, now_utc=now.replace(hour=13, minute=35)
    )

    assert result.status.value == ("failed" if foreign else "monitoring")
    assert lookups == ["entry-today", "not-submitted"]
    assert len(launches) == (0 if foreign else 1)
    assert store.list_orders() == orders_before
    assert store.load_symbol_states(trade_date) == states_before
    assert confirmation_path.read_bytes() == original_confirmation
