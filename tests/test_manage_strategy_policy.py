from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl
import pytest

from data_plane.calendar import build_xnys_schedule
from data_plane.storage import persist_snapshot
from kernel.strategy_policy import load_strategy_policy
from scripts.manage_strategy_policy import (
    approve_challenger,
    bootstrap_active_policy,
    build_challenger,
    rollback_policy,
)

NOW = datetime(2026, 8, 27, 1, 0, tzinfo=UTC)
CHALLENGER_CREATED = datetime(2026, 6, 1, 1, 0, tzinfo=UTC)


def _decision(
    data_root: Path, *, status: str = "research_champion_promoted"
) -> str:
    snapshot, _ = persist_snapshot(
        pl.DataFrame(
            {
                "baseline": [3.0],
                "selected": [4.0],
                "status": [status],
                "production_eligible": [False],
            }
        ),
        root=data_root,
        source="research.sandbox.rvol_champion_decision",
        schema_version="rvol_research_champion_decision.v1",
        checks=(),
    )
    return snapshot.dataset_id


def _shadow_outcome(
    data_root: Path,
    state_root: Path,
    day: date,
    *,
    active_policy_hash: str,
    challenger_version: str,
    challenger_policy_hash: str,
    orders_submitted: int = 0,
) -> None:
    first_wave = state_root / day.isoformat() / "first_wave_pool.json"
    first_wave.parent.mkdir(parents=True, exist_ok=True)
    first_wave.write_text(
        json.dumps({"trade_date": day.isoformat(), "candidates": []}),
        encoding="utf-8",
    )
    persist_snapshot(
        pl.DataFrame(
            {
                "session_date": [day],
                "active_version": ["selection-baseline"],
                "active_policy_hash": [active_policy_hash],
                "challenger_version": [challenger_version],
                "challenger_policy_hash": [challenger_policy_hash],
                "first_wave_sha256": [hashlib.sha256(first_wave.read_bytes()).hexdigest()],
                "champion_candidate_count": [10],
                "challenger_candidate_count": [5],
                "champion_capture_count": [2],
                "challenger_capture_count": [2],
                "evidence_complete": [True],
                "orders_submitted": [orders_submitted],
            }
        ),
        root=data_root,
        source="research.strategy_shadow_outcome",
        schema_version="strategy_shadow_outcome.v1",
        checks=(),
    )


def test_bootstrap_is_idempotent_and_never_overwrites_active(tmp_path: Path) -> None:
    active = tmp_path / "active.json"
    first = bootstrap_active_policy(
        active,
        min_rvol=3.0,
        version="selection-baseline",
        approved_by="owner",
        now_utc=NOW,
    )
    second = bootstrap_active_policy(
        active,
        min_rvol=5.0,
        version="selection-other",
        approved_by="owner",
        now_utc=NOW,
    )

    assert first == second
    assert load_strategy_policy(active).min_rvol == 3.0


def test_only_a_promoted_oos_decision_can_build_challenger(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    active = tmp_path / "active.json"
    challenger = tmp_path / "challenger.json"
    bootstrap_active_policy(
        active,
        min_rvol=3.0,
        version="selection-baseline",
        approved_by="owner",
        now_utc=NOW,
    )
    decision_id = _decision(data_root, status="champion_retained")

    with pytest.raises(RuntimeError, match="promoted"):
        build_challenger(
            active,
            challenger,
            data_root=data_root,
            decision_dataset_id=decision_id,
            now_utc=NOW,
        )


def test_challenger_requires_the_exact_verified_decision_snapshot(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    active = tmp_path / "active.json"
    challenger = tmp_path / "challenger.json"
    bootstrap_active_policy(
        active,
        min_rvol=3.0,
        version="selection-baseline",
        approved_by="owner",
        now_utc=NOW,
    )
    decision_id = _decision(data_root)
    decision_path = data_root / "accepted" / decision_id / "data.parquet"
    decision_path.write_bytes(decision_path.read_bytes() + b"tampered")

    with pytest.raises(RuntimeError, match="hash mismatch"):
        build_challenger(
            active,
            challenger,
            data_root=data_root,
            decision_dataset_id=decision_id,
            now_utc=NOW,
        )


def test_approval_requires_twenty_independent_shadow_sessions(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    active = tmp_path / "active.json"
    challenger = tmp_path / "challenger.json"
    history = tmp_path / "history"
    state_root = tmp_path / "runs/autonomous"
    bootstrap_active_policy(
        active,
        min_rvol=3.0,
        version="selection-baseline",
        approved_by="owner",
        now_utc=CHALLENGER_CREATED,
    )
    decision_id = _decision(data_root)
    shadow = build_challenger(
        active,
        challenger,
        data_root=data_root,
        decision_dataset_id=decision_id,
        now_utc=CHALLENGER_CREATED,
    )
    sessions = build_xnys_schedule(date(2026, 7, 1), date(2026, 8, 1))[
        "trade_date"
    ].to_list()
    for day in sessions[:19]:
        _shadow_outcome(
            data_root,
            state_root,
            day,
            active_policy_hash=load_strategy_policy(active).policy_hash,
            challenger_version=shadow.version,
            challenger_policy_hash=shadow.policy_hash,
        )

    with pytest.raises(RuntimeError, match="20 independent"):
        approve_challenger(
            active,
            challenger,
            history_dir=history,
            data_root=data_root,
            approved_by="owner",
            confirm_policy_hash=shadow.policy_hash,
            state_root=state_root,
            now_utc=NOW,
        )

    _shadow_outcome(
        data_root,
        state_root,
        sessions[19],
        active_policy_hash=load_strategy_policy(active).policy_hash,
        challenger_version=shadow.version,
        challenger_policy_hash=shadow.policy_hash,
    )
    promoted = approve_challenger(
        active,
        challenger,
        history_dir=history,
        data_root=data_root,
        approved_by="owner",
        confirm_policy_hash=shadow.policy_hash,
        state_root=state_root,
        now_utc=NOW,
    )

    assert promoted.status == "active"
    assert promoted.min_rvol == 4.0
    assert (history / "selection-baseline.json").is_file()


def test_approval_rejects_shadow_orders_or_wrong_active_baseline(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    active = tmp_path / "active.json"
    challenger = tmp_path / "challenger.json"
    state_root = tmp_path / "runs/autonomous"
    bootstrap_active_policy(
        active,
        min_rvol=3.0,
        version="selection-baseline",
        approved_by="owner",
        now_utc=CHALLENGER_CREATED,
    )
    decision_id = _decision(data_root)
    shadow = build_challenger(
        active,
        challenger,
        data_root=data_root,
        decision_dataset_id=decision_id,
        now_utc=CHALLENGER_CREATED,
    )
    sessions = build_xnys_schedule(date(2026, 7, 1), date(2026, 8, 1))[
        "trade_date"
    ].to_list()
    for offset, day in enumerate(sessions[:20]):
        _shadow_outcome(
            data_root,
            state_root,
            day,
            active_policy_hash=load_strategy_policy(active).policy_hash,
            challenger_version=shadow.version,
            challenger_policy_hash=shadow.policy_hash,
            orders_submitted=1 if offset == 19 else 0,
        )

    with pytest.raises(RuntimeError, match="execution boundary"):
        approve_challenger(
            active,
            challenger,
            history_dir=tmp_path / "history",
            data_root=data_root,
            approved_by="owner",
            confirm_policy_hash=shadow.policy_hash,
            state_root=state_root,
            now_utc=NOW,
        )


def test_approval_requires_exact_challenger_hash(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    active = tmp_path / "active.json"
    challenger = tmp_path / "challenger.json"
    bootstrap_active_policy(
        active,
        min_rvol=3.0,
        version="selection-baseline",
        approved_by="owner",
        now_utc=NOW,
    )
    decision_id = _decision(data_root)
    build_challenger(
        active,
        challenger,
        data_root=data_root,
        decision_dataset_id=decision_id,
        now_utc=NOW,
    )

    with pytest.raises(RuntimeError, match="policy-hash confirmation"):
        approve_challenger(
            active,
            challenger,
            history_dir=tmp_path / "history",
            data_root=data_root,
            approved_by="owner",
            confirm_policy_hash="0" * 64,
            state_root=tmp_path / "runs/autonomous",
            now_utc=NOW,
        )

def test_rollback_uses_verified_history_and_creates_a_new_audit_version(
    tmp_path: Path,
) -> None:
    active = tmp_path / "active.json"
    history = tmp_path / "history"
    baseline = bootstrap_active_policy(
        active,
        min_rvol=3.0,
        version="selection-baseline",
        approved_by="owner",
        now_utc=NOW,
    )
    history.mkdir()
    from kernel.strategy_policy import write_strategy_policy

    write_strategy_policy(history / f"{baseline.version}.json", baseline)

    rolled_back = rollback_policy(
        active,
        history_dir=history,
        target_version="selection-baseline",
        approved_by="owner",
        now_utc=NOW,
    )

    assert rolled_back.status == "active"
    assert rolled_back.min_rvol == 3.0
    assert rolled_back.version.startswith("rollback-20260827-")


def test_rollback_rejects_path_traversal(tmp_path: Path) -> None:
    active = tmp_path / "active.json"
    bootstrap_active_policy(
        active,
        min_rvol=3.0,
        version="selection-baseline",
        approved_by="owner",
        now_utc=NOW,
    )

    with pytest.raises(ValueError, match="invalid|escapes"):
        rollback_policy(
            active,
            history_dir=tmp_path / "history",
            target_version="../outside",
            approved_by="owner",
            now_utc=NOW,
        )
