"""Build, approve, inspect, and roll back governed selection policies."""

from __future__ import annotations

import argparse
import re
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import polars as pl

from data_plane.calendar import build_xnys_schedule
from data_plane.contracts import DatasetSnapshot
from data_plane.storage import sha256_file
from kernel.strategy_policy import (
    StrategyPolicy,
    build_strategy_policy,
    load_strategy_policy,
    write_strategy_policy,
)
from schedule.runtime import ProcessLock

ROOT = Path(__file__).resolve().parents[1]
DECISION_SOURCE = "research.sandbox.rvol_champion_decision"
SHADOW_SOURCE = "research.strategy_shadow_outcome"
MINIMUM_SHADOW_SESSIONS = 20
MINIMUM_CAPTURE_RETENTION = 0.50
EASTERN = ZoneInfo("America/New_York")


def _verified_snapshot(path: Path, *, source: str, schema: str) -> DatasetSnapshot:
    snapshot = DatasetSnapshot.model_validate_json(
        (path.parent / "manifest.json").read_text(encoding="utf-8")
    ).assert_usable()
    if snapshot.source != source or snapshot.schema_version != schema:
        raise RuntimeError(f"unexpected accepted snapshot contract: {snapshot.dataset_id}")
    if sha256_file(path) != snapshot.content_sha256:
        raise RuntimeError(f"accepted snapshot hash mismatch: {snapshot.dataset_id}")
    return snapshot


def _save_history(history_dir: Path, policy: StrategyPolicy) -> None:
    path = history_dir / f"{policy.version}.json"
    if path.exists():
        if load_strategy_policy(path, required_status="active") != policy:
            raise RuntimeError(f"strategy history collision: {policy.version}")
        return
    write_strategy_policy(path, policy)


def bootstrap_active_policy(
    path: Path,
    *,
    min_rvol: float,
    version: str,
    approved_by: str,
    now_utc: datetime,
) -> StrategyPolicy:
    if path.exists():
        return load_strategy_policy(path, required_status="active")
    policy = build_strategy_policy(
        version=version,
        status="active",
        min_rvol=min_rvol,
        created_at_utc=now_utc,
        approved_by=approved_by,
        approved_at_utc=now_utc,
    )
    write_strategy_policy(path, policy)
    return policy


def _decision_snapshot(
    data_root: Path, dataset_id: str
) -> tuple[Path, DatasetSnapshot]:
    path = data_root / "accepted" / dataset_id / "data.parquet"
    if not path.is_file():
        raise FileNotFoundError(f"accepted snapshot unavailable: {dataset_id}")
    snapshot = _verified_snapshot(
        path,
        source=DECISION_SOURCE,
        schema="rvol_research_champion_decision.v1",
    )
    if snapshot.dataset_id != dataset_id:
        raise RuntimeError("accepted decision identity mismatch")
    return path, snapshot


def build_challenger(
    active_path: Path,
    challenger_path: Path,
    *,
    data_root: Path,
    decision_dataset_id: str,
    now_utc: datetime,
) -> StrategyPolicy:
    with ProcessLock(active_path.with_suffix(".policy.lock")):
        return _build_challenger_unlocked(
            active_path,
            challenger_path,
            data_root=data_root,
            decision_dataset_id=decision_dataset_id,
            now_utc=now_utc,
        )


def _build_challenger_unlocked(
    active_path: Path,
    challenger_path: Path,
    *,
    data_root: Path,
    decision_dataset_id: str,
    now_utc: datetime,
) -> StrategyPolicy:
    active = load_strategy_policy(active_path, required_status="active")
    decision_path, snapshot = _decision_snapshot(data_root, decision_dataset_id)
    decision = pl.read_parquet(decision_path)
    if decision.height != 1:
        raise RuntimeError("RVOL sandbox decision must contain exactly one row")
    row = decision.row(0, named=True)
    if row.get("status") != "research_champion_promoted":
        raise RuntimeError("RVOL sandbox did not produce a promoted research champion")
    if row.get("production_eligible") is not False:
        raise RuntimeError("research decision crossed the production boundary")
    baseline = float(row["baseline"])
    selected = float(row["selected"])
    if baseline != active.min_rvol or selected <= baseline:
        raise RuntimeError("RVOL challenger does not advance the active baseline")
    version = f"challenger-{now_utc:%Y%m}-{snapshot.content_sha256[:10]}"
    policy = build_strategy_policy(
        version=version,
        status="shadow",
        min_rvol=selected,
        created_at_utc=now_utc,
        previous_version=active.version,
        source_snapshot_ids=(snapshot.dataset_id,),
    )
    if challenger_path.exists():
        existing = load_strategy_policy(challenger_path, required_status="shadow")
        if existing == policy:
            return existing
        raise RuntimeError("an unresolved challenger already exists")
    write_strategy_policy(challenger_path, policy)
    return policy


def _shadow_rows(data_root: Path, challenger_version: str) -> pl.DataFrame:
    rows: list[pl.DataFrame] = []
    for path in (data_root / "accepted").glob(f"{SHADOW_SOURCE}-*/data.parquet"):
        snapshot = _verified_snapshot(
            path, source=SHADOW_SOURCE, schema="strategy_shadow_outcome.v1"
        )
        if snapshot.dataset_id != path.parent.name:
            raise RuntimeError("accepted shadow outcome identity mismatch")
        frame = pl.read_parquet(path)
        if frame.height != 1:
            raise RuntimeError(f"invalid shadow outcome snapshot: {snapshot.dataset_id}")
        if frame["challenger_version"][0] == challenger_version:
            rows.append(frame)
    if not rows:
        return pl.DataFrame()
    return pl.concat(rows).unique(subset=["session_date"], keep="last")


def _validate_shadow_evidence(
    frame: pl.DataFrame,
    *,
    active: StrategyPolicy,
    challenger: StrategyPolicy,
    approval_time_utc: datetime,
    state_root: Path,
) -> None:
    if frame.is_empty() or frame["session_date"].n_unique() < MINIMUM_SHADOW_SESSIONS:
        raise RuntimeError("challenger requires 20 independent shadow sessions")
    if not frame["evidence_complete"].all():
        raise RuntimeError("challenger shadow evidence is incomplete")
    required = {
        "session_date",
        "active_version",
        "active_policy_hash",
        "challenger_version",
        "challenger_policy_hash",
        "first_wave_sha256",
        "evidence_complete",
        "orders_submitted",
    }
    if missing := required - set(frame.columns):
        raise RuntimeError(f"challenger shadow evidence fields missing: {sorted(missing)}")
    if frame["active_version"].unique().to_list() != [active.version]:
        raise RuntimeError("challenger shadow evidence uses a different active baseline")
    if frame["active_policy_hash"].unique().to_list() != [active.policy_hash]:
        raise RuntimeError("challenger shadow evidence uses a different active policy hash")
    if frame["challenger_policy_hash"].unique().to_list() != [challenger.policy_hash]:
        raise RuntimeError("challenger shadow evidence uses a different policy hash")
    if "orders_submitted" not in frame.columns or frame["orders_submitted"].sum() != 0:
        raise RuntimeError("challenger shadow evidence crossed the execution boundary")
    session_dates = frame["session_date"].unique().sort().to_list()
    created_session = challenger.created_at_utc.astimezone(EASTERN).date()
    if session_dates[0] < created_session:
        raise RuntimeError("challenger shadow evidence predates challenger creation")
    if session_dates[-1] > approval_time_utc.astimezone(EASTERN).date():
        raise RuntimeError("challenger shadow evidence contains a future session")
    xnys_dates = set(
        build_xnys_schedule(session_dates[0], session_dates[-1])["trade_date"].to_list()
    )
    if set(session_dates) - xnys_dates:
        raise RuntimeError("challenger shadow evidence includes non-XNYS sessions")
    first_wave_hashes = frame.select("session_date", "first_wave_sha256").iter_rows(
        named=True
    )
    for row in first_wave_hashes:
        first_wave_path = state_root / str(row["session_date"]) / "first_wave_pool.json"
        if (
            not first_wave_path.is_file()
            or sha256_file(first_wave_path) != row["first_wave_sha256"]
        ):
            raise RuntimeError("challenger shadow evidence lost its frozen first-wave binding")
    champion_candidates = int(frame["champion_candidate_count"].sum())
    challenger_candidates = int(frame["challenger_candidate_count"].sum())
    champion_captures = int(frame["champion_capture_count"].sum())
    challenger_captures = int(frame["challenger_capture_count"].sum())
    if challenger_candidates <= 0 or champion_candidates <= 0 or champion_captures <= 0:
        raise RuntimeError("challenger shadow sample has no comparable observations")
    champion_precision = champion_captures / champion_candidates
    challenger_precision = challenger_captures / challenger_candidates
    retention = challenger_captures / champion_captures
    if challenger_precision < champion_precision or retention < MINIMUM_CAPTURE_RETENTION:
        raise RuntimeError("challenger failed shadow precision or capture-retention gates")


def approve_challenger(
    active_path: Path,
    challenger_path: Path,
    *,
    history_dir: Path,
    data_root: Path,
    approved_by: str,
    confirm_policy_hash: str,
    state_root: Path,
    now_utc: datetime,
) -> StrategyPolicy:
    with ProcessLock(active_path.with_suffix(".policy.lock")):
        return _approve_challenger_unlocked(
            active_path,
            challenger_path,
            history_dir=history_dir,
            data_root=data_root,
            approved_by=approved_by,
            confirm_policy_hash=confirm_policy_hash,
            state_root=state_root,
            now_utc=now_utc,
        )


def _approve_challenger_unlocked(
    active_path: Path,
    challenger_path: Path,
    *,
    history_dir: Path,
    data_root: Path,
    approved_by: str,
    confirm_policy_hash: str,
    state_root: Path,
    now_utc: datetime,
) -> StrategyPolicy:
    active = load_strategy_policy(active_path, required_status="active")
    challenger = load_strategy_policy(challenger_path, required_status="shadow")
    if confirm_policy_hash != challenger.policy_hash:
        raise RuntimeError("explicit challenger policy-hash confirmation is required")
    if challenger.previous_version != active.version:
        raise RuntimeError("challenger baseline no longer matches the active policy")
    _validate_shadow_evidence(
        _shadow_rows(data_root, challenger.version),
        active=active,
        challenger=challenger,
        approval_time_utc=now_utc,
        state_root=state_root,
    )
    promoted = build_strategy_policy(
        version=challenger.version.replace("challenger-", "selection-", 1),
        status="active",
        min_rvol=challenger.min_rvol,
        created_at_utc=challenger.created_at_utc,
        previous_version=active.version,
        source_snapshot_ids=challenger.source_snapshot_ids,
        approved_by=approved_by,
        approved_at_utc=now_utc,
    )
    _save_history(history_dir, active)
    challenger_path.unlink()
    write_strategy_policy(active_path, promoted)
    _save_history(history_dir, promoted)
    return promoted


def rollback_policy(
    active_path: Path,
    *,
    history_dir: Path,
    target_version: str,
    approved_by: str,
    now_utc: datetime,
) -> StrategyPolicy:
    with ProcessLock(active_path.with_suffix(".policy.lock")):
        return _rollback_policy_unlocked(
            active_path,
            history_dir=history_dir,
            target_version=target_version,
            approved_by=approved_by,
            now_utc=now_utc,
        )


def _rollback_policy_unlocked(
    active_path: Path,
    *,
    history_dir: Path,
    target_version: str,
    approved_by: str,
    now_utc: datetime,
) -> StrategyPolicy:
    if re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,63}", target_version) is None:
        raise ValueError("target strategy version is invalid")
    active = load_strategy_policy(active_path, required_status="active")
    history_root = history_dir.resolve()
    target_path = (history_root / f"{target_version}.json").resolve()
    if target_path.parent != history_root:
        raise ValueError("rollback target escapes strategy history")
    target = load_strategy_policy(target_path, required_status="active")
    if target.version != target_version:
        raise RuntimeError("rollback target identity mismatch")
    _save_history(history_dir, active)
    rolled_back = build_strategy_policy(
        version=f"rollback-{now_utc:%Y%m%d}-{target.version}"[:64],
        status="active",
        min_rvol=target.min_rvol,
        created_at_utc=now_utc,
        previous_version=active.version,
        source_snapshot_ids=target.source_snapshot_ids,
        approved_by=approved_by,
        approved_at_utc=now_utc,
    )
    write_strategy_policy(active_path, rolled_back)
    _save_history(history_dir, rolled_back)
    return rolled_back


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("bootstrap", "build-challenger", "approve", "rollback", "status")
    )
    parser.add_argument("--active", type=Path, default=ROOT / "runs/strategy/active.json")
    parser.add_argument(
        "--challenger", type=Path, default=ROOT / "runs/strategy/challenger.json"
    )
    parser.add_argument("--history-dir", type=Path, default=ROOT / "runs/strategy/history")
    parser.add_argument("--data-root", type=Path, default=ROOT / "data")
    parser.add_argument("--state-root", type=Path, default=ROOT / "runs/autonomous")
    parser.add_argument("--approved-by")
    parser.add_argument("--confirm-policy-hash")
    parser.add_argument("--decision-dataset-id")
    parser.add_argument("--target-version")
    parser.add_argument("--min-rvol", type=float, default=3.0)
    parser.add_argument("--version", default="selection-baseline")
    return parser


def main() -> None:
    args = _parser().parse_args()
    now = datetime.now(UTC)
    if args.command == "bootstrap":
        if not args.approved_by:
            raise SystemExit("bootstrap requires --approved-by")
        policy = bootstrap_active_policy(
            args.active,
            min_rvol=args.min_rvol,
            version=args.version,
            approved_by=args.approved_by,
            now_utc=now,
        )
    elif args.command == "build-challenger":
        if not args.decision_dataset_id:
            raise SystemExit("build-challenger requires --decision-dataset-id")
        policy = build_challenger(
            args.active,
            args.challenger,
            data_root=args.data_root,
            decision_dataset_id=args.decision_dataset_id,
            now_utc=now,
        )
    elif args.command == "approve":
        if not args.approved_by or not args.confirm_policy_hash:
            raise SystemExit("approve requires --approved-by and --confirm-policy-hash")
        policy = approve_challenger(
            args.active,
            args.challenger,
            history_dir=args.history_dir,
            data_root=args.data_root,
            approved_by=args.approved_by,
            confirm_policy_hash=args.confirm_policy_hash,
            state_root=args.state_root,
            now_utc=now,
        )
    elif args.command == "rollback":
        if not args.approved_by or not args.target_version:
            raise SystemExit("rollback requires --approved-by and --target-version")
        policy = rollback_policy(
            args.active,
            history_dir=args.history_dir,
            target_version=args.target_version,
            approved_by=args.approved_by,
            now_utc=now,
        )
    else:
        policy = load_strategy_policy(args.active, required_status="active")
    print(policy.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
