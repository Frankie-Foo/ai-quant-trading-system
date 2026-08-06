from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl

from data_plane.storage import persist_snapshot
from operations.client_desk import TradingDeskEvidence


def _selection_snapshot(
    data_root: Path,
    *,
    session_date: date,
    missing_boolean_facts: bool = False,
) -> str:
    frame = pl.DataFrame(
        {
            "session_date": [session_date, session_date, session_date],
            "symbol": ["AAA", "BBB", "CCC"],
            "pass_gate": [True, True, False],
            "selection_rank": [2, 1, None],
            "reject_reason": ["", "", "missing_rvol"],
            "catalyst_categories": [
                ["earnings"],
                ["earnings", "general_news"],
                ["earnings"],
            ],
            "event_count": [1, 3, 1],
            "earnings_evidence_layers": [1, 3, 1],
            "earnings_intensity_score": [25.0, 92.0, 8.0],
            "earnings_strength_confirmed": (
                [None, None, False]
                if missing_boolean_facts
                else [False, True, False]
            ),
            "rvol": [8.0, 21.0, None],
            "premarket_gap_return": [0.04, 0.09, None],
            "premarket_return": [0.02, 0.05, None],
            "premarket_close": [52.0, 109.0, None],
            "premarket_vwap": [51.4, 106.5, None],
            "premarket_close_location": [0.8, 0.95, None],
            "premarket_above_vwap": (
                [None, None, False]
                if missing_boolean_facts
                else [True, True, False]
            ),
            "directional_volume_confirmed": (
                [None, None, False]
                if missing_boolean_facts
                else [True, True, False]
            ),
            "market_cap": [2_000_000_000.0, 30_000_000_000.0, 500_000_000.0],
            "adv_usd": [20_000_000.0, 800_000_000.0, 5_000_000.0],
            "atr_pct": [0.05, 0.04, 0.08],
            "gate_asof_utc": [
                datetime(2026, 7, 30, 12, 32, tzinfo=UTC),
                datetime(2026, 7, 30, 12, 32, tzinfo=UTC),
                datetime(2026, 7, 30, 12, 32, tzinfo=UTC),
            ],
        }
    )
    snapshot, _ = persist_snapshot(
        frame,
        root=data_root,
        source="kernel.universe.selection_gates",
        schema_version="selection_gates.v2",
        checks=(),
    )
    return snapshot.dataset_id


def _jobs_db(
    path: Path,
    *,
    trade_date: date,
    lock_status: str = "succeeded",
) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE job_runs (
                job_name TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                job_version TEXT NOT NULL,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL,
                started_at_utc TEXT NOT NULL,
                finished_at_utc TEXT,
                error_code TEXT,
                artifact_ids_json TEXT NOT NULL,
                run_token TEXT,
                PRIMARY KEY (job_name, trade_date, job_version)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO job_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "premarket_catalyst_lock",
                trade_date.isoformat(),
                "premarket_catalyst_lock.v4",
                lock_status,
                2,
                "2026-07-30T04:00:00+00:00",
                "2026-07-30T04:05:00+00:00",
                None if lock_status == "succeeded" else "RuntimeError",
                "[]",
                "opaque-token",
            ),
        )


def test_desk_exposes_ranked_selection_jobs_and_maturity_without_secrets(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    snapshot_id = _selection_snapshot(
        data_root,
        session_date=date(2026, 7, 30),
    )
    _jobs_db(runs_root / "jobs.sqlite3", trade_date=date(2026, 7, 30))
    (runs_root / "maturity-evidence.json").write_text(
        json.dumps(
            {
                "asof_utc": "2026-07-30T12:00:00Z",
                "paper_trading_sessions": 1,
                "point_in_time_history_sessions": 252,
                "net_labeled_trade_count": 0,
                "quote_cost_coverage": 0.0,
                "purged_oos_fold_count": 0,
            }
        ),
        encoding="utf-8",
    )

    result = TradingDeskEvidence(
        data_root=data_root,
        runs_root=runs_root,
    ).snapshot(datetime(2026, 7, 30, 13, 0, tzinfo=UTC))

    assert result["schema_version"] == "trading_desk_evidence.v1"
    assert result["stage"] == "research_only"
    assert result["orders_authorized"] is False
    selection = result["selection"]
    assert isinstance(selection, dict)
    assert selection["status"] == "ready"
    assert selection["snapshot_id"] == snapshot_id
    assert selection["pass_count"] == 2
    candidates = selection["candidates"]
    assert isinstance(candidates, list)
    assert [row["symbol"] for row in candidates] == ["BBB", "AAA"]
    assert candidates[0]["rvol"] == 21.0
    maturity = result["maturity"]
    assert isinstance(maturity, dict)
    assert maturity["paper_trading_sessions"] == 1
    agents = result["agents"]
    assert isinstance(agents, list)
    assert isinstance(agents[0], dict)
    assert agents[0]["status"] == "unavailable"

    serialized = json.dumps(result).lower()
    assert "opaque-token" not in serialized
    assert "api_key" not in serialized
    assert "secret" not in serialized


def test_failed_current_lock_marks_previous_selection_stale_and_blocked(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    _selection_snapshot(data_root, session_date=date(2026, 7, 30))
    _jobs_db(
        runs_root / "jobs.sqlite3",
        trade_date=date(2026, 7, 31),
        lock_status="failed",
    )

    result = TradingDeskEvidence(
        data_root=data_root,
        runs_root=runs_root,
    ).snapshot(datetime(2026, 7, 31, 2, 0, tzinfo=UTC))

    selection = result["selection"]
    assert isinstance(selection, dict)
    assert selection["target_trade_date"] == "2026-07-31"
    assert selection["session_date"] == "2026-07-30"
    assert selection["status"] == "blocked"
    assert selection["stale"] is True
    assert selection["blocker"] == "premarket_catalyst_lock:RuntimeError"


def test_beijing_after_midnight_stays_on_open_us_session(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    _selection_snapshot(data_root, session_date=date(2026, 7, 31))

    during_session = TradingDeskEvidence(
        data_root=data_root,
        runs_root=runs_root,
    ).snapshot(datetime(2026, 7, 31, 17, 0, tzinfo=UTC))
    after_close = TradingDeskEvidence(
        data_root=data_root,
        runs_root=runs_root,
    ).snapshot(datetime(2026, 7, 31, 21, 0, tzinfo=UTC))

    assert during_session["target_trade_date"] == "2026-07-31"
    during_selection = during_session["selection"]
    assert isinstance(during_selection, dict)
    assert during_selection["status"] == "ready"
    assert after_close["target_trade_date"] == "2026-08-03"
    after_selection = after_close["selection"]
    assert isinstance(after_selection, dict)
    assert after_selection["stale"] is True



def test_desk_preserves_missing_candidate_booleans_as_unknown(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    _selection_snapshot(
        data_root,
        session_date=date(2026, 7, 30),
        missing_boolean_facts=True,
    )

    result = TradingDeskEvidence(
        data_root=data_root,
        runs_root=runs_root,
    ).snapshot(datetime(2026, 7, 30, 13, 0, tzinfo=UTC))

    candidates = result["selection"]["candidates"]
    assert candidates[0]["earnings_strength_confirmed"] is None
    assert candidates[0]["premarket_above_vwap"] is None
    assert candidates[0]["directional_volume_confirmed"] is None
