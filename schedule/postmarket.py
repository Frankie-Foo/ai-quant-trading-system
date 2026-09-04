"""Production one-shot postmarket orchestration for cron, systemd, or Task Scheduler."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import polars as pl

from data_plane.calendar import build_xnys_schedule
from data_plane.contracts import DatasetSnapshot
from kernel.config import load_config
from operations.feishu_base import FeishuBaseError, FeishuBaseEventClient
from operations.feishu_investment_events import record_postmarket_review
from operations.local_env import load_project_env, project_data_root
from schedule.child_process import run_child
from schedule.runtime import JsonEventLogger, LockUnavailableError, ProcessLock
from schedule.state import JobLedger

ROOT = Path(__file__).resolve().parents[1]
JOB_NAME = "postmarket_review"
JOB_VERSION = "postmarket_review.v10"


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


def _manifest(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"manifest is not an object: {path}")
    return value


def postmarket_due(
    trade_date: date,
    now_utc: datetime,
    *,
    data_grace_minutes: int | None = None,
) -> bool:
    if now_utc.tzinfo is None or now_utc.utcoffset() is None:
        raise ValueError("now_utc must be timezone-aware")
    schedule = build_xnys_schedule(trade_date, trade_date)
    if schedule.height != 1:
        return False
    close = schedule.row(0, named=True)["market_close_utc"]
    if not isinstance(close, datetime):
        raise ValueError("calendar close timestamp is invalid")
    if data_grace_minutes is None:
        data_grace_minutes = load_config(
            ROOT / "config.yaml"
        ).market_data.postmarket_data_grace_minutes
    if data_grace_minutes < 0:
        raise ValueError("postmarket data grace must not be negative")
    return now_utc.astimezone(UTC) >= close + timedelta(minutes=data_grace_minutes)


def _selection_dates(data_root: Path) -> tuple[date, ...]:
    values: set[date] = set()
    for path in (data_root / "accepted").glob("kernel.universe.selection_gates-*/data.parquet"):
        dates = (
            pl.read_parquet(path, columns=["session_date"])
            .get_column("session_date")
            .unique()
            .to_list()
        )
        if len(dates) == 1 and isinstance(dates[0], date):
            values.add(dates[0])
    return tuple(sorted(values))


def _latest_snapshot(data_root: Path, pattern: str, trade_date: date) -> DatasetSnapshot | None:
    matches: list[DatasetSnapshot] = []
    for path in (data_root / "accepted").glob(pattern):
        dates = (
            pl.read_parquet(path, columns=["session_date"])
            .get_column("session_date")
            .unique()
            .to_list()
        )
        if dates != [trade_date]:
            continue
        matches.append(DatasetSnapshot.model_validate(_manifest(path.parent / "manifest.json")))
    return max(matches, key=lambda value: value.asof_utc) if matches else None


def _latest_full_signal(data_root: Path, trade_date: date) -> DatasetSnapshot | None:
    schedule = build_xnys_schedule(trade_date, trade_date)
    if schedule.height != 1:
        return None
    close = schedule.row(0, named=True)["market_close_utc"]
    if not isinstance(close, datetime):
        return None
    matches: list[DatasetSnapshot] = []
    for path in (data_root / "accepted").glob("kernel.signals.orb5_shadow-*/data.parquet"):
        frame = pl.read_parquet(path, columns=["session_date", "data_cutoff_utc"])
        if frame.get_column("session_date").unique().to_list() != [trade_date]:
            continue
        cutoff = frame.get_column("data_cutoff_utc").min()
        if not isinstance(cutoff, datetime) or cutoff < close:
            continue
        matches.append(DatasetSnapshot.model_validate(_manifest(path.parent / "manifest.json")))
    return max(matches, key=lambda value: value.asof_utc) if matches else None


def _run_module(
    module: str,
    trade_date: date,
    *,
    data_root: Path,
    logger: JsonEventLogger,
    extra_args: tuple[str, ...] = (),
) -> str:
    command = [
        sys.executable,
        "-m",
        module,
        "--trade-date",
        trade_date.isoformat(),
        "--data-root",
        str(data_root),
        *extra_args,
    ]
    logger.emit("child_started", module=module, trade_date=trade_date.isoformat())
    result = run_child(
        command,
        cwd=ROOT,
        timeout_seconds=900,
    )
    if result.return_code != 0:
        logger.emit(
            "child_failed",
            level="error",
            module=module,
            trade_date=trade_date.isoformat(),
            return_code=result.return_code,
            elapsed_ms=result.elapsed_ms,
            stdout_lines=len(result.stdout.splitlines()),
            stderr_lines=len(result.stderr.splitlines()),
        )
        raise RuntimeError(f"{module} failed with exit code {result.return_code}")
    logger.emit(
        "child_completed",
        module=module,
        trade_date=trade_date.isoformat(),
        elapsed_ms=result.elapsed_ms,
    )
    return result.stdout


def _sync_loop_review(
    *,
    trade_date: date,
    data_root: Path,
    artifacts: tuple[str, ...],
    logger: JsonEventLogger,
) -> None:
    """Best-effort slow-loop handoff; local review success remains authoritative."""

    if not _truthy(os.environ.get("AI_QUANT_LOOP_SYNC_ENABLED")):
        return
    binding = os.environ.get("AI_QUANT_LOOP_BINDING_FILE", "").strip()
    active_policy = os.environ.get("AI_QUANT_ACTIVE_POLICY_FILE", "").strip()
    if not binding or not active_policy:
        logger.emit(
            "loop_review_sync_skipped",
            level="warning",
            trade_date=trade_date.isoformat(),
            reason="binding_or_active_policy_missing",
        )
        return
    command = [
        sys.executable,
        "-m",
        "scripts.sync_loop_daily_review",
        "--trade-date",
        trade_date.isoformat(),
        "--data-root",
        str(data_root),
        "--binding",
        binding,
        "--active-policy",
        active_policy,
    ]
    for artifact in artifacts:
        command.extend(("--artifact-id", artifact))
    result = run_child(command, cwd=ROOT, timeout_seconds=120)
    logger.emit(
        "loop_review_sync_completed" if result.return_code == 0 else "loop_review_sync_pending",
        level="info" if result.return_code == 0 else "warning",
        trade_date=trade_date.isoformat(),
        return_code=result.return_code,
        orders_submitted=0,
    )


def _sync_loop_outcomes(
    *,
    trade_date: date,
    data_root: Path,
    logger: JsonEventLogger,
) -> None:
    """Best-effort delayed labels; never weaken the local postmarket result."""

    if not _truthy(os.environ.get("AI_QUANT_LOOP_OUTCOME_SYNC_ENABLED")):
        return
    config_file = os.environ.get(
        "AI_QUANT_LOOP_OUTCOME_CONFIG_FILE", ""
    ).strip()
    if not config_file:
        logger.emit(
            "loop_outcome_sync_skipped",
            level="warning",
            trade_date=trade_date.isoformat(),
            reason="approved_outcome_config_missing",
        )
        return
    command = [
        sys.executable,
        "-m",
        "scripts.sync_loop_due_outcomes",
        "--as-of-trade-date",
        trade_date.isoformat(),
        "--data-root",
        str(data_root),
        "--config",
        config_file,
    ]
    result = run_child(command, cwd=ROOT, timeout_seconds=300)
    logger.emit(
        "loop_outcome_sync_completed"
        if result.return_code == 0
        else "loop_outcome_sync_pending",
        level="info" if result.return_code == 0 else "warning",
        trade_date=trade_date.isoformat(),
        return_code=result.return_code,
        orders_submitted=0,
    )


def _run_one(
    data_root: Path,
    trade_date: date,
    *,
    llm_mode: str,
    logger: JsonEventLogger,
) -> tuple[str, ...]:
    signal = _latest_full_signal(data_root, trade_date)
    if signal is None:
        _run_module(
            "scripts.build_orb5_signals",
            trade_date,
            data_root=data_root,
            logger=logger,
        )
        signal = _latest_full_signal(data_root, trade_date)
    if signal is None:
        raise RuntimeError("full-session signal snapshot was not produced")

    episode = _latest_snapshot(data_root, "research.trading_episodes-*/data.parquet", trade_date)
    if episode is None or signal.dataset_id not in episode.parent_snapshot_ids:
        _run_module(
            "scripts.build_postmarket_episode",
            trade_date,
            data_root=data_root,
            logger=logger,
        )
        episode = _latest_snapshot(
            data_root, "research.trading_episodes-*/data.parquet", trade_date
        )
    if episode is None or signal.dataset_id not in episode.parent_snapshot_ids:
        raise RuntimeError("accepted postmarket episode was not produced")

    review = _latest_snapshot(
        data_root,
        "research.postmarket.program_review-*/data.parquet",
        trade_date,
    )
    if review is None or episode.dataset_id not in review.parent_snapshot_ids:
        _run_module(
            "scripts.review_postmarket_episode",
            trade_date,
            data_root=data_root,
            logger=logger,
            extra_args=("--llm-mode", llm_mode),
        )
        review = _latest_snapshot(
            data_root,
            "research.postmarket.program_review-*/data.parquet",
            trade_date,
        )
    if review is None or episode.dataset_id not in review.parent_snapshot_ids:
        raise RuntimeError("programmatic postmarket review snapshot was not produced")

    opportunity_review = _latest_snapshot(
        data_root,
        "research.intraday_selection_postmortem-*/data.parquet",
        trade_date,
    )
    if opportunity_review is None:
        _run_module(
            "scripts.run_postclose_missed_movers_review",
            trade_date,
            data_root=data_root,
            logger=logger,
            extra_args=("--top", "20", "--attempts", "5"),
        )
        opportunity_review = _latest_snapshot(
            data_root,
            "research.intraday_selection_postmortem-*/data.parquet",
            trade_date,
        )
    if opportunity_review is None:
        raise RuntimeError("accepted intraday selection postmortem was not produced")

    shadow_stdout = _run_module(
        "scripts.evaluate_strategy_shadow",
        trade_date,
        data_root=data_root,
        logger=logger,
    )
    shadow_payload = json.loads(shadow_stdout)
    if not isinstance(shadow_payload, dict):
        raise RuntimeError("strategy shadow output was not a JSON object")
    shadow_dataset_id = shadow_payload.get("dataset_id")

    no_trade_review = _latest_snapshot(
        data_root,
        "research.paper_no_trade_review-*/data.parquet",
        trade_date,
    )
    autonomous_root = ROOT / "runs" / "autonomous" / trade_date.isoformat()
    paper_config = autonomous_root / "autonomous_paper.json"
    if no_trade_review is None and paper_config.exists():
        _run_module(
            "scripts.build_no_trade_review",
            trade_date,
            data_root=data_root,
            logger=logger,
            extra_args=(
                "--config",
                str(paper_config),
                "--state-db",
                str(autonomous_root / "paper.sqlite3"),
            ),
        )
        no_trade_review = _latest_snapshot(
            data_root,
            "research.paper_no_trade_review-*/data.parquet",
            trade_date,
        )
    if paper_config.exists() and no_trade_review is None:
        raise RuntimeError("Paper no-trade review snapshot was not produced")

    recovery_decision = _latest_snapshot(
        data_root,
        "research.selection_recovery_shadow-*/data.parquet",
        trade_date,
    )
    recovery_outcome = _latest_snapshot(
        data_root,
        "research.selection_recovery_shadow_outcomes-*/data.parquet",
        trade_date,
    )
    if recovery_decision is not None and (
        recovery_outcome is None
        or recovery_decision.dataset_id not in recovery_outcome.parent_snapshot_ids
    ):
        _run_module(
            "scripts.label_selection_recovery_shadow",
            trade_date,
            data_root=data_root,
            logger=logger,
        )
        recovery_outcome = _latest_snapshot(
            data_root,
            "research.selection_recovery_shadow_outcomes-*/data.parquet",
            trade_date,
        )
    if recovery_decision is not None and (
        recovery_outcome is None
        or recovery_decision.dataset_id not in recovery_outcome.parent_snapshot_ids
    ):
        raise RuntimeError("selection recovery outcome was not produced")

    pdca_stdout = _run_module(
        "scripts.run_structured_pdca",
        trade_date,
        data_root=data_root,
        logger=logger,
        extra_args=("--llm-mode", llm_mode),
    )
    pdca_payload = json.loads(pdca_stdout)
    if not isinstance(pdca_payload, dict):
        raise RuntimeError("structured PDCA output was not a JSON object")
    extra_artifacts = [
        str(value)
        for value in (
            pdca_payload.get("audit_report_id"),
            *cast(list[object], pdca_payload.get("lesson_ids", [])),
        )
        if value
    ]
    return (
        signal.dataset_id,
        episode.dataset_id,
        review.dataset_id,
        opportunity_review.dataset_id,
        *((str(shadow_dataset_id),) if shadow_dataset_id else ()),
        *((no_trade_review.dataset_id,) if no_trade_review is not None else ()),
        *((recovery_outcome.dataset_id,) if recovery_outcome is not None else ()),
        *extra_artifacts,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trade-date", type=_parse_date)
    parser.add_argument("--data-root", type=Path, default=project_data_root(ROOT))
    parser.add_argument("--state-db", type=Path, default=ROOT / "runs" / "jobs.sqlite3")
    parser.add_argument("--lock-file", type=Path, default=ROOT / "runs" / "postmarket.lock")
    parser.add_argument(
        "--llm-mode",
        choices=("off", "optional", "required"),
        default="optional",
    )
    return parser


def run(argv: list[str] | None = None) -> int:
    load_project_env(ROOT)
    args = _parser().parse_args(argv)
    logger = JsonEventLogger(service="postmarket")
    try:
        with ProcessLock(args.lock_file):
            return _run_locked(args, logger)
    except LockUnavailableError:
        logger.emit("tick_skipped_lock_held", level="warning")
        return 0


def _run_locked(args: argparse.Namespace, logger: JsonEventLogger) -> int:
    now_utc = datetime.now(UTC)
    cfg = load_config(ROOT / "config.yaml")
    data_grace_minutes = cfg.market_data.postmarket_data_grace_minutes
    candidates: tuple[date, ...]
    if args.trade_date is not None:
        candidates = (args.trade_date,)
        skipped_historical_dates = 0
    else:
        available_dates = _selection_dates(args.data_root)
        candidates = (max(available_dates),) if available_dates else ()
        skipped_historical_dates = max(0, len(available_dates) - len(candidates))
    due_dates = [
        value
        for value in candidates
        if postmarket_due(
            value,
            now_utc,
            data_grace_minutes=data_grace_minutes,
        )
    ]
    # ProcessLock excludes a healthy peer, so an inherited RUNNING lease means
    # the prior process terminated abruptly and can be recovered immediately.
    ledger = JobLedger(args.state_db, stale_after=timedelta(0))
    feishu = FeishuBaseEventClient.from_environment(os.environ)
    failures = 0
    logger.emit(
        "tick_started",
        candidate_dates=len(candidates),
        due_dates=len(due_dates),
        job_version=JOB_VERSION,
        data_grace_minutes=data_grace_minutes,
        llm_mode=args.llm_mode,
        skipped_historical_dates=skipped_historical_dates,
        orders_submitted=0,
    )
    for trade_date in due_dates:
        lease = ledger.acquire(
            JOB_NAME,
            trade_date,
            JOB_VERSION,
            max_attempts=cfg.scheduler.postmarket_max_attempts,
            retry_after=timedelta(minutes=cfg.scheduler.postmarket_retry_minutes),
        )
        if lease is None:
            record = ledger.get(JOB_NAME, trade_date, JOB_VERSION)
            logger.emit(
                "job_skipped",
                trade_date=trade_date.isoformat(),
                status=record.status.value if record is not None else "not_claimed",
                attempts=record.attempts if record is not None else 0,
            )
            continue
        logger.emit(
            "job_started",
            trade_date=trade_date.isoformat(),
            attempt=lease.attempt,
        )
        try:
            artifacts = _run_one(
                args.data_root,
                trade_date,
                llm_mode=args.llm_mode,
                logger=logger,
            )
            if feishu is not None:
                if len(artifacts) < 4:
                    raise RuntimeError("postmarket review evidence is incomplete")
                try:
                    record_postmarket_review(
                        feishu,
                        trade_date=trade_date,
                        program_review_id=artifacts[2],
                        selection_review_id=artifacts[3],
                        evidence_ids=artifacts,
                        observed_at_utc=now_utc,
                    )
                except FeishuBaseError as exc:
                    logger.emit(
                        "feishu_review_write_failed",
                        level="error",
                        error_type=type(exc).__name__,
                        trade_date=trade_date.isoformat(),
                    )
                    if _truthy(os.environ.get("FEISHU_INVESTMENT_AUDIT_REQUIRED")):
                        raise
            _sync_loop_review(
                trade_date=trade_date,
                data_root=args.data_root,
                artifacts=artifacts,
                logger=logger,
            )
            _sync_loop_outcomes(
                trade_date=trade_date,
                data_root=args.data_root,
                logger=logger,
            )
            ledger.complete(lease, artifact_ids=artifacts)
            logger.emit(
                "job_completed",
                trade_date=trade_date.isoformat(),
                attempt=lease.attempt,
                artifact_count=len(artifacts),
                orders_submitted=0,
            )
        except Exception as exc:
            failures += 1
            error_code = type(exc).__name__
            ledger.fail(lease, error_code=error_code)
            logger.emit(
                "job_failed",
                level="error",
                trade_date=trade_date.isoformat(),
                attempt=lease.attempt,
                error_code=error_code,
                orders_submitted=0,
            )
    logger.emit(
        "tick_completed" if failures == 0 else "tick_failed",
        level="info" if failures == 0 else "error",
        due_dates=len(due_dates),
        failures=failures,
        orders_submitted=0,
    )
    return 1 if failures else 0


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
