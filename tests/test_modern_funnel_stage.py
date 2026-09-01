import argparse
import json
import subprocess
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

from kernel.strategy_policy import build_strategy_policy, write_strategy_policy
from operations.autonomous_selection_handoff import create_open_confirmation
from operations.paper_state import PaperStateStore
from schedule.modern_funnel import FunnelStage
from scripts import run_modern_funnel_stage as stage_runner
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

    kept, rejected = evaluate_second_wave(
        [_candidate("GOOD"), _candidate("WIDE")], bars, quotes
    )

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
    assert context["active_version"] == active.version
    assert challenger_context["symbols"] == ["KEEP"]
    assert challenger_context["execution_eligible"] is False


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
    assert _stage_observed_at(
        date(2026, 8, 24), FunnelStage.FIRST_WAVE
    ) == datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
    assert _stage_observed_at(
        date(2026, 8, 24), FunnelStage.OPEN_CONFIRMATION
    ) == datetime(2026, 8, 24, 13, 35, tzinfo=UTC)


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
    trade_date = date(2026, 8, 24)
    run_dir = tmp_path / "runs" / "modern-momentum" / trade_date.isoformat()
    store = PaperStateStore(run_dir / "paper-state.sqlite3")
    assert store.claim_run(trade_date, owner="pid-456", observed_at_utc=NOW)
    monkeypatch.setattr(stage_runner, "ROOT", tmp_path)
    monkeypatch.setenv("AI_QUANT_PAPER_RUNTIME_CONFIRMED", "true")
    monkeypatch.setenv("AI_QUANT_PAPER_SMOKE_MAX_NOTIONAL", "100")

    def unexpected(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("active monitor must not be launched again")

    monkeypatch.setattr(subprocess, "Popen", unexpected)
    monkeypatch.setattr(stage_runner, "datetime", type("Clock", (), {"now": lambda *_: NOW}))

    assert stage_runner._launch_paper_if_confirmed(
        trade_date, tmp_path / "open_confirmation.json"
    ) == 456
