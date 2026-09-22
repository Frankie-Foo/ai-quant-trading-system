"""Offline regressions for the production funnel's child-process boundaries."""

import argparse
import subprocess
import sys
from datetime import UTC, date, datetime, tzinfo
from pathlib import Path
from types import SimpleNamespace
from typing import Self
from zoneinfo import ZoneInfo

import polars as pl
import pytest

from data_plane import candidate_pools, storage
from data_plane.contracts import DataQualityCheck, QualitySeverity
from data_plane.storage import persist_snapshot
from operations.local_env import sip_monitoring_window
from schedule import child_process, premarket
from schedule.modern_funnel import FunnelStage, ProductionFunnelExecutor
from schedule.runtime import JsonEventLogger
from scripts import run_modern_funnel_stage as stage
from scripts.prepare_modern_momentum_forward import SOURCE

EASTERN = ZoneInfo("America/New_York")


@pytest.mark.parametrize("month", [9, 12])
def test_sip_available_at_first_wave_in_both_dst_seasons(month: int) -> None:
    assert sip_monitoring_window(datetime(2026, month, 3, 8, 30, tzinfo=EASTERN))
    assert not sip_monitoring_window(datetime(2026, month, 3, 8, 29, tzinfo=EASTERN))


@pytest.mark.parametrize("hour,minute", [(8, 30), (9, 0), (9, 33)])
def test_wave_passes_current_complete_minute_to_rvol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hour: int, minute: int,
) -> None:
    current = datetime(2026, 9, 22, hour, minute, 42, tzinfo=EASTERN)

    class Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            return cls.fromtimestamp(current.timestamp(), tz=tz)

    calls: list[tuple[str, tuple[str, ...]]] = []

    ids: dict[str, str] = {}
    def run(
        module: str, day: date, root: Path, *, extra_args: tuple[str, ...] = (),
    ) -> dict[str, str]:
        calls.append((module, extra_args))
        sources = {
            "scripts.build_catalyst_snapshot": "kernel.catalysts.wave_candidates",
            "scripts.build_premarket_rvol": "kernel.premarket.rvol_candidates",
            "scripts.refresh_event_sip_market_caps": "event.sip_market_cap",
            "scripts.build_selection_gates": "kernel.universe.selection_gates",
        }
        if module not in sources:
            return {}
        checks: tuple[DataQualityCheck, ...] = ()
        if module == "scripts.build_catalyst_snapshot":
            checks = (DataQualityCheck(
                name="wave_context", severity=QualitySeverity.CRITICAL, passed=True,
                observed=day.isoformat(), expected=extra_args[extra_args.index("--asof") + 1],
                provenance="test",
            ),)
        snapshot, _ = persist_snapshot(
            pl.DataFrame({"symbol": ["NEW"], "session_date": [day]}), root=root,
            source=sources[module], schema_version="test.v1", checks=checks,
            parent_snapshot_ids=tuple(ids.values()),
        )
        ids[module] = snapshot.dataset_id
        return {"dataset_id": snapshot.dataset_id, "candidate_dataset_id": snapshot.dataset_id}

    monkeypatch.setattr(stage, "datetime", Clock)
    monkeypatch.setattr(storage, "datetime", Clock)
    monkeypatch.setattr(candidate_pools, "datetime", Clock)
    monkeypatch.setattr(stage, "_run_module", run)
    stage._refresh_selection_inputs(
        argparse.Namespace(trade_date=current.date(), data_root=tmp_path), include_lock=True,
    )
    assert calls[0] == ("schedule.premarket", ("--reference-only",))
    rvol_args = dict(calls)["scripts.build_premarket_rvol"]
    cutoff = datetime.fromisoformat(rvol_args[rvol_args.index("--decision-asof") + 1])
    expected = min(current.replace(second=0), current.replace(hour=9, minute=30, second=0))
    assert cutoff == expected.astimezone(UTC)
    assert cutoff <= current
    assert rvol_args[rvol_args.index("--candidate-snapshot") + 1] == ids[
        "scripts.build_catalyst_snapshot"
    ]
    gate_args = dict(calls)["scripts.build_selection_gates"]
    assert gate_args[gate_args.index("--rvol-snapshot") + 1] == ids["scripts.build_premarket_rvol"]


def test_reference_lock_does_not_require_live_rvol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def run(arguments: list[str], **kwargs: object) -> tuple[str, ...]:
        calls.append(arguments)
        return ("reference-snapshot",)

    monkeypatch.setattr(premarket, "_run", run)
    monkeypatch.setattr(premarket, "_has_reference_snapshot", lambda *_: True)
    premarket._lock_stage(date(2026, 9, 22), tmp_path, JsonEventLogger(service="test"))
    assert [args[1] for args in calls] == [
        "data_plane.cli", "scripts.build_daily_universe", "scripts.build_catalyst_snapshot",
    ]


def test_wave_reference_preparation_does_not_depend_on_old_overnight_news(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(premarket, "load_project_env", lambda *_: None)
    monkeypatch.setattr(premarket, "_has_reference_snapshot", lambda *_: True)

    def run(arguments: list[str], **kwargs: object) -> tuple[str, ...]:
        calls.append(arguments[1])
        assert arguments[1] != "scripts.build_catalyst_snapshot"
        return ("reference",)

    monkeypatch.setattr(premarket, "_run", run)
    assert premarket.run([
        "--trade-date", "2026-09-22", "--data-root", str(tmp_path), "--reference-only",
        "--state-db", str(tmp_path / "jobs.sqlite3"), "--lock-file", str(tmp_path / "job.lock"),
    ], now_utc=datetime(2026, 9, 22, 12, 30, tzinfo=UTC)) == 0
    assert calls == ["data_plane.cli", "scripts.build_daily_universe"]


def test_lock_only_never_runs_legacy_selection_even_when_due(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(premarket, "load_project_env", lambda *_: None)
    monkeypatch.setattr(premarket, "_lock_stage", lambda *_: ("snapshot",))

    def unexpected(*args: object, **kwargs: object) -> None:
        pytest.fail("modern funnel must own live selection and publication")

    monkeypatch.setattr(premarket, "_selection_stage", unexpected)
    monkeypatch.setattr(premarket, "_project_selection_event", unexpected)
    assert premarket.run(
        ["--trade-date", "2026-09-22", "--data-root", str(tmp_path),
         "--state-db", str(tmp_path / "jobs.sqlite3"),
         "--lock-file", str(tmp_path / "job.lock"), "--lock-only"],
        now_utc=datetime(2026, 9, 22, 13, 0, tzinfo=UTC),
    ) == 0


@pytest.mark.parametrize("caller", ["wave", "scheduler", "executor"])
def test_windows_children_are_hidden_and_decode_chinese_utf8(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caller: str,
) -> None:
    calls: list[dict[str, object]] = []

    def run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(kwargs)
        return subprocess.CompletedProcess([], 0, '{"ok":true,"receipt_id":"中文"}', "")

    monkeypatch.setattr(subprocess, "run", run)
    if caller == "wave":
        stage._run_module("scripts.build_premarket_rvol", date(2026, 9, 22), tmp_path)
    elif caller == "executor":
        ProductionFunnelExecutor(root=tmp_path, runner=run).execute(
            FunnelStage.FIRST_WAVE, date(2026, 9, 22),
        )
    else:
        child_process.run_child(["python", "-m", "example"], cwd=tmp_path, timeout_seconds=10)
    assert calls[0].get("creationflags") == getattr(subprocess, "CREATE_NO_WINDOW", 0)
    assert calls[0].get("encoding") == "utf-8"


def test_real_child_preserves_chinese_output(tmp_path: Path) -> None:
    result = child_process.run_child(
        [sys.executable, "-c", "print(chr(0x4e2d) + chr(0x6587))"],
        cwd=tmp_path, timeout_seconds=10,
    )
    assert result.return_code == 0
    assert result.stdout.strip() == "中文"


def test_repeat_bonus_applies_before_top20_truncation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    day = date(2026, 9, 22)
    rows = [{"symbol": f"S{i:02}", "forward_rank": i} for i in range(1, 22)]
    gate_path = tmp_path / "gates.parquet"
    pl.DataFrame({"pass_gate": [True] * 21, "rvol": [2.0] * 21}).write_parquet(gate_path)
    requested: list[str] = []
    monkeypatch.setattr(stage, "_refresh_selection_inputs", lambda *a, **k: (
        pl.read_parquet(gate_path), SimpleNamespace(dataset_id="gates"),
    ))

    def prepare(*a: object, extra_args: tuple[str, ...] = ()) -> dict[str, str]:
        requested.extend(extra_args)
        snapshot, _ = persist_snapshot(
            pl.DataFrame(rows if "--all-eligible" in requested else rows[:20]),
            root=tmp_path, source=SOURCE, schema_version="test.v1", checks=(),
            parent_snapshot_ids=("gates",),
        )
        return {"dataset_id": snapshot.dataset_id}

    monkeypatch.setattr(stage, "_run_module", prepare)
    selected, rejected, _ = stage._rank_live_pool(
        argparse.Namespace(trade_date=day, data_root=tmp_path),
        prior_waves=({"candidates": [{"symbol": "S21"}]},),
        limit=20, include_lock=False,
    )
    assert "S21" in [row["symbol"] for row in selected]
    assert [row["symbol"] for row in rejected] == ["S20"]


def test_first_wave_records_capacity_rejections_without_failing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    publications: list[object] = []
    monkeypatch.setattr(stage, "_rank_live_pool", lambda *a, **k: (
        [{"symbol": "PASS"}], [{"symbol": "OVERFLOW", "reasons": ["容量落选"]}], "pool",
    ))
    monkeypatch.setattr(stage, "_strategy_context", lambda *a: {})

    def publish(**kwargs: object) -> tuple[tuple[str, ...], str]:
        publications.append(kwargs["rejected"])
        return (), "message"

    monkeypatch.setattr(stage, "_publish_stage", publish)
    args = argparse.Namespace(
        trade_date=date(2026, 9, 22), data_root=tmp_path, state_root=tmp_path,
    )
    result = stage._first_wave(args, tmp_path)
    assert result["livermore_message_id"] == "message"
    payload = stage._read_json(tmp_path / "first_wave_pool.json")
    assert payload["rejected"][0]["symbol"] == "OVERFLOW"
    stage._first_wave(args, tmp_path)
    assert publications == [payload["rejected"], payload["rejected"]]


@pytest.mark.parametrize("all_eligible,expected", [(True, 51), (False, 10)])
def test_forward_builder_all_eligible_preserves_hard_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, all_eligible: bool, expected: int,
) -> None:
    from scripts import prepare_modern_momentum_forward as builder

    day = date(2026, 9, 22)
    count = 53
    gates = pl.DataFrame({
        "symbol": [f"S{i:02}" for i in range(count)],
        "session_date": [day] * count,
        "asof_date": [date(2026, 9, 21)] * count,
        "pass_gate": [True] * count,
        "market_cap": [2e9] * 52 + [5e8],
        "market_cap_provenance": ["shares|alpaca.sip.rest"] * count,
        "rvol": [2.0] * count,
        "premarket_return": [0.01] * count,
        "current_halt": [False] * 51 + [True, False],
        "luld_risk": [False] * count,
        "catalyst_categories": [["earnings"]] * count,
    })
    gate_path = tmp_path / "gate.parquet"
    snapshot, gate_path = persist_snapshot(
        gates, root=tmp_path, source="kernel.universe.selection_gates",
        schema_version="test.v1", checks=(),
    )
    monkeypatch.setattr(builder, "load_project_env", lambda *_: None)
    def unexpected(*args: object) -> None:
        pytest.fail("explicit gate ID must not discover latest snapshot")
    monkeypatch.setattr(builder, "latest_gate_paths", unexpected)
    monkeypatch.setattr(sys, "argv", [
        "prepare", "--trade-date", day.isoformat(), "--data-root", str(tmp_path),
        "--gate-snapshot", snapshot.dataset_id,
        *(["--all-eligible"] if all_eligible else []),
    ])
    builder.main()
    path = next(
        (tmp_path / "accepted").glob("research.modern_momentum.forward_pool-*/data.parquet")
    )
    pool = pl.read_parquet(path)
    assert pool.height == expected
    assert not {"S51", "S52"} & set(pool["symbol"])
