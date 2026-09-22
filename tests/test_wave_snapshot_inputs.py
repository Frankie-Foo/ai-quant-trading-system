"""Offline public-boundary checks for immutable per-wave data handoffs."""

import json
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

from data_plane import snapshot_queries
from data_plane.candidate_pools import load_premarket_pool
from data_plane.catalysts import empty_catalyst_frame
from data_plane.storage import persist_snapshot
from scripts import build_catalyst_snapshot as catalyst_cli
from scripts import build_selection_gates as gates_cli
from scripts import refresh_event_sip_market_caps as caps_cli
from scripts import run_modern_funnel_stage as stage_cli
from scripts.prepare_modern_momentum_forward import SOURCE


def test_open_pool_uses_explicit_final_source_not_newer_snapshot(tmp_path: Path) -> None:
    day = date(2026, 9, 22)
    source = SOURCE
    selected, _ = persist_snapshot(
        pl.DataFrame({"symbol": ["SELECTED"], "session_date": [day], "forward_rank": [1]}),
        root=tmp_path, source=source, schema_version="test.v1", checks=(),
    )
    persist_snapshot(
        pl.DataFrame({"symbol": ["WRONG"], "session_date": [day], "forward_rank": [1]}),
        root=tmp_path, source=source, schema_version="test.v1", checks=(),
    )
    frozen = stage_cli._freeze_final_pool(
        tmp_path, day, ("SELECTED",), source_snapshot_id=selected.dataset_id,
    )
    frame, _ = snapshot_queries.load_snapshot_by_id(
        tmp_path, frozen.dataset_id, source=source, required_parents=(selected.dataset_id,),
    )
    assert frame["symbol"].to_list() == ["SELECTED"]


def test_market_caps_keep_explicit_empty_rvol_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    snapshot, _ = persist_snapshot(
        pl.DataFrame(schema={"symbol": pl.String, "session_date": pl.Date}),
        root=tmp_path, source="kernel.premarket.rvol_candidates",
        schema_version="test.v1", checks=(),
    )
    monkeypatch.setattr(caps_cli, "sip_monitoring_window", lambda *_: True)
    monkeypatch.setattr(sys, "argv", [
        "caps", "--trade-date", "2026-09-22", "--data-root", str(tmp_path),
        "--rvol-snapshot", snapshot.dataset_id,
    ])
    assert caps_cli.main() == 0
    receipt = json.loads(capsys.readouterr().out)
    frame, caps = snapshot_queries.load_snapshot_by_id(
        tmp_path, receipt["dataset_id"], source="event.sip_market_cap",
        required_parents=(snapshot.dataset_id,),
    )
    assert frame.is_empty()
    assert caps.parent_snapshot_ids == (snapshot.dataset_id,)


def test_explicit_snapshot_keeps_empty_pool_and_rejects_tampering(tmp_path: Path) -> None:
    source = "kernel.catalysts.wave_candidates"
    empty, path = persist_snapshot(
        pl.DataFrame(schema={"symbol": pl.String}), root=tmp_path,
        source=source, schema_version="test.v1", checks=(),
    )
    persist_snapshot(
        pl.DataFrame({"symbol": ["NEWER"]}), root=tmp_path,
        source=source, schema_version="test.v1", checks=(),
    )
    frame, snapshot = snapshot_queries.load_snapshot_by_id(
        tmp_path, empty.dataset_id, source=source, available_by=datetime.now(UTC),
    )
    assert frame.is_empty()
    assert snapshot.dataset_id == empty.dataset_id
    pl.DataFrame({"symbol": ["TAMPERED"]}).write_parquet(path)
    with pytest.raises(ValueError, match="hash"):
        snapshot_queries.load_snapshot_by_id(tmp_path, empty.dataset_id, source=source)


@pytest.mark.parametrize("no_events", [False, True])
def test_wave_news_cli_discovers_new_symbol_without_overwriting_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], no_events: bool,
) -> None:
    day = date(2026, 9, 22)
    cutoff = datetime(2026, 9, 22, 12, 30, tzinfo=UTC)
    universe = pl.DataFrame({
        "symbol": ["NEW", "LATE"], "asof_date": [date(2026, 9, 21)] * 2,
        "precheck_pass": [True, True],
    })
    for source, frame in (
        ("kernel.universe.daily_precheck", universe),
        ("massive.reference_tickers.cs", universe.with_columns(pl.lit("1").alias("cik"))),
        ("kernel.catalysts.overnight_candidates", pl.DataFrame({
            "symbol": ["OLD"], "session_date": [day],
        })),
    ):
        persist_snapshot(frame, root=tmp_path, source=source, schema_version="test.v1", checks=())
    lock_path = next((tmp_path / "accepted").glob("kernel.catalysts.overnight*/data.parquet"))
    lock_before = lock_path.read_bytes()
    rows = []
    for symbol in ("NEW", "LATE"):
        rows.append({
            "source": "alpaca.news.benzinga", "source_event_id": symbol,
            "event_type": "news", "symbols": [symbol],
            "published_utc": cutoff - timedelta(minutes=15),
            "updated_utc": cutoff + timedelta(minutes=1) if symbol == "LATE" else None,
            "retrieved_utc": cutoff + timedelta(seconds=5),
            "headline": f"{symbol} announces a major customer contract",
            "summary": "A signed multi year agreement secures additional revenue and production "
                       "capacity for the company with detailed delivery milestones and committed "
                       "minimum purchase volumes over the coming years.",
            "provenance": f"test:{symbol}",
        })
    news = empty_catalyst_frame() if no_events else pl.DataFrame(
        rows, schema=empty_catalyst_frame().schema,
    )
    monkeypatch.setattr(catalyst_cli, "load_project_env", lambda *_: None)
    monkeypatch.setenv("DESKTOP_MARKET_DATA_PROVIDER", "alpaca_direct")
    monkeypatch.setattr(catalyst_cli, "fetch_alpaca_news_direct", lambda *a, **k: news)
    monkeypatch.setattr(catalyst_cli, "fetch_massive_news", lambda *a, **k: empty_catalyst_frame())
    monkeypatch.setattr(catalyst_cli, "fetch_live_candidate_filings",
                        lambda *a, **k: empty_catalyst_frame())
    monkeypatch.setattr(sys, "argv", [
        "news", "--trade-date", day.isoformat(), "--asof", cutoff.isoformat(),
        "--data-root", str(tmp_path), "--wave",
    ])
    catalyst_cli.main()
    receipt = json.loads(capsys.readouterr().out)
    frame, snapshot = snapshot_queries.load_snapshot_by_id(
        tmp_path, receipt["candidate_dataset_id"], source="kernel.catalysts.wave_candidates",
    )
    assert frame["symbol"].to_list() == ([] if no_events else ["NEW"])
    assert lock_path.read_bytes() == lock_before
    assert any(c.name == "wave_context" and c.observed == day.isoformat() for c in snapshot.checks)
    loaded = load_premarket_pool(
        tmp_path, day, pool="catalyst", snapshot_id=snapshot.dataset_id,
        decision_cutoff=cutoff,
    )
    assert loaded.frame["symbol"].to_list() == ([] if no_events else ["NEW"])
    with pytest.raises(ValueError, match="context"):
        load_premarket_pool(
            tmp_path, day, pool="catalyst", snapshot_id=snapshot.dataset_id,
            decision_cutoff=cutoff + timedelta(minutes=30),
        )
    if not no_events:
        wrong_rvol, _ = persist_snapshot(
            frame.with_columns(pl.lit(3.0).alias("rvol")), root=tmp_path,
            source="kernel.premarket.rvol_candidates", schema_version="test.v1",
            checks=(), parent_snapshot_ids=("some-other-candidate-pool",),
        )
        monkeypatch.setattr(gates_cli, "load_project_env", lambda *_: None)
        monkeypatch.setattr(sys, "argv", [
            "gates", "--trade-date", day.isoformat(), "--data-root", str(tmp_path),
            "--candidate-snapshot", snapshot.dataset_id,
            "--rvol-snapshot", wrong_rvol.dataset_id, "--market-cap-snapshot", "wrong-cap",
        ])
        with pytest.raises(ValueError, match="parent"):
            gates_cli.main()
        stale_rvol, _ = persist_snapshot(
            frame.with_columns(pl.lit(cutoff - timedelta(minutes=5)).alias("decision_asof_utc")),
            root=tmp_path, source="kernel.premarket.rvol_candidates", schema_version="test.v1",
            checks=(), parent_snapshot_ids=(snapshot.dataset_id,),
        )
        monkeypatch.setattr(sys, "argv", [
            "gates", "--trade-date", day.isoformat(), "--data-root", str(tmp_path),
            "--candidate-snapshot", snapshot.dataset_id,
            "--rvol-snapshot", stale_rvol.dataset_id, "--market-cap-snapshot", "wrong-cap",
        ])
        with pytest.raises(ValueError, match="cutoff"):
            gates_cli.main()
