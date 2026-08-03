"""Snapshot-backed evidence projection for the local trading desktop."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import polars as pl

from data_plane.calendar import build_xnys_schedule
from data_plane.contracts import DatasetSnapshot
from operations.runtime_agent_safety import (
    RuntimeAgentRole,
    load_runtime_agent_assessment,
)

BEIJING = ZoneInfo("Asia/Shanghai")
SELECTION_SOURCE = "kernel.universe.selection_gates"
REVIEW_SOURCE = "research.intraday_selection_postmortem"
CURRENT_JOB_NAMES = {
    "premarket_catalyst_lock",
    "premarket_final_selection",
    "premarket_multisignal_shadow",
    "postmarket_review",
}
MATURITY_FIELDS = (
    "asof_utc",
    "paper_trading_sessions",
    "point_in_time_history_sessions",
    "net_labeled_trade_count",
    "quote_cost_coverage",
    "purged_oos_fold_count",
    "duplicate_order_count",
    "reconciliation_match_rate",
)


@dataclass(frozen=True)
class SnapshotFrame:
    snapshot: DatasetSnapshot
    frame: pl.DataFrame
    session_date: date


class TradingDeskEvidence:
    """Build one honest client projection from immutable and durable evidence."""

    def __init__(
        self,
        *,
        data_root: Path,
        runs_root: Path,
        selection_time_beijing: time = time(20, 0),
    ):
        self.data_root = data_root
        self.runs_root = runs_root
        self.selection_time_beijing = selection_time_beijing

    def snapshot(self, observed_at_utc: datetime | None = None) -> dict[str, object]:
        observed_at = observed_at_utc or datetime.now(UTC)
        _require_utc(observed_at)
        target_date = _target_session(observed_at)
        phase = market_phase(
            observed_at,
            selection_time_beijing=self.selection_time_beijing,
        )
        jobs = self._jobs(target_date)
        selection = self._selection(
            target_date=target_date,
            observed_at_utc=observed_at,
            jobs=jobs,
        )
        pipeline_status = _pipeline_status(selection, jobs)
        return {
            "schema_version": "trading_desk_evidence.v1",
            "observed_at_utc": observed_at.isoformat(),
            "target_trade_date": target_date.isoformat(),
            "market_phase": phase,
            "stage": "research_only",
            "pipeline_status": pipeline_status,
            "orders_authorized": False,
            "paper_eligible": False,
            "live_eligible": False,
            "selection": selection,
            "review": self._review(target_date),
            "jobs": jobs,
            "agents": self._agents(target_date, observed_at),
            "maturity": self._maturity(),
        }

    def _selection(
        self,
        *,
        target_date: date,
        observed_at_utc: datetime,
        jobs: list[dict[str, object]],
    ) -> dict[str, object]:
        latest = self._latest_snapshot(
            source=SELECTION_SOURCE,
            date_column="session_date",
            not_after=target_date,
        )
        current = latest is not None and latest.session_date == target_date
        failed_lock = _failed_job(jobs, "premarket_catalyst_lock", target_date)
        failed_selection = _failed_job(
            jobs,
            "premarket_final_selection",
            target_date,
        )
        if current:
            status = "ready"
            blocker = None
        elif failed_lock is not None:
            status = "blocked"
            blocker = (
                f"premarket_catalyst_lock:"
                f"{failed_lock.get('error_code') or 'failed'}"
            )
        elif failed_selection is not None:
            status = "blocked"
            blocker = (
                f"premarket_final_selection:"
                f"{failed_selection.get('error_code') or 'failed'}"
            )
        else:
            selection_at = datetime.combine(
                target_date,
                self.selection_time_beijing,
                BEIJING,
            ).astimezone(UTC)
            status = "waiting" if observed_at_utc < selection_at else "missing"
            blocker = None

        candidates = [] if latest is None else _selection_candidates(latest.frame)
        return {
            "status": status,
            "blocker": blocker,
            "target_trade_date": target_date.isoformat(),
            "expected_at_beijing": self.selection_time_beijing.strftime("%H:%M"),
            "session_date": (
                None if latest is None else latest.session_date.isoformat()
            ),
            "stale": bool(latest is not None and not current),
            "snapshot_id": (
                None if latest is None else latest.snapshot.dataset_id
            ),
            "asof_utc": (
                None
                if latest is None
                else latest.snapshot.asof_utc.isoformat()
            ),
            "pass_count": len(candidates),
            "candidates": candidates,
        }

    def _review(self, target_date: date) -> dict[str, object]:
        latest = self._latest_snapshot(
            source=REVIEW_SOURCE,
            date_column="session_date",
            not_after=target_date,
        )
        if latest is None:
            return {
                "status": "unavailable",
                "session_date": None,
                "stale": True,
                "snapshot_id": None,
                "opportunities": [],
            }
        rows = (
            latest.frame.sort("opportunity_rank")
            if "opportunity_rank" in latest.frame.columns
            else latest.frame
        )
        opportunities: list[dict[str, object]] = []
        for row in rows.head(12).iter_rows(named=True):
            opportunities.append(
                {
                    "rank": _integer(row.get("opportunity_rank")),
                    "symbol": _text(row.get("symbol")),
                    "close_return": _number(row.get("close_return")),
                    "mfe": _number(row.get("mfe_from_previous_close")),
                    "mae": _number(row.get("mae_from_previous_close")),
                    "selection_status": _text(row.get("selection_status")),
                    "decision_outcome": _text(row.get("decision_outcome")),
                    "root_cause": _text(row.get("root_cause")),
                    "research_action": _text(row.get("research_action")),
                    "production_change_allowed": bool(
                        row.get("production_change_allowed", False)
                    ),
                }
            )
        return {
            "status": "ready",
            "session_date": latest.session_date.isoformat(),
            "stale": latest.session_date < target_date,
            "snapshot_id": latest.snapshot.dataset_id,
            "asof_utc": latest.snapshot.asof_utc.isoformat(),
            "opportunity_count": latest.frame.height,
            "opportunities": opportunities,
        }

    def _jobs(self, target_date: date) -> list[dict[str, object]]:
        path = self.runs_root / "jobs.sqlite3"
        if not path.is_file():
            return []
        try:
            with sqlite3.connect(path) as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    """
                    SELECT job_name, trade_date, job_version, status, attempts,
                           started_at_utc, finished_at_utc, error_code
                    FROM job_runs
                    WHERE trade_date <= ?
                    ORDER BY trade_date DESC, started_at_utc DESC
                    LIMIT 40
                    """,
                    (target_date.isoformat(),),
                ).fetchall()
        except sqlite3.Error:
            return []
        latest: dict[tuple[str, str], sqlite3.Row] = {}
        for row in rows:
            name = str(row["job_name"])
            trade_date_value = str(row["trade_date"])
            if name not in CURRENT_JOB_NAMES:
                continue
            latest.setdefault((name, trade_date_value), row)
        output: list[dict[str, object]] = []
        for row in latest.values():
            output.append(
                {
                    "job_name": str(row["job_name"]),
                    "trade_date": str(row["trade_date"]),
                    "version": str(row["job_version"]),
                    "status": str(row["status"]),
                    "attempts": int(row["attempts"]),
                    "started_at_utc": str(row["started_at_utc"]),
                    "finished_at_utc": (
                        None
                        if row["finished_at_utc"] is None
                        else str(row["finished_at_utc"])
                    ),
                    "error_code": (
                        None
                        if row["error_code"] is None
                        else str(row["error_code"])
                    ),
                }
            )
        return sorted(
            output,
            key=lambda row: (
                str(row["trade_date"]),
                str(row["job_name"]),
            ),
            reverse=True,
        )

    def _agents(
        self,
        target_date: date,
        observed_at_utc: datetime,
    ) -> list[dict[str, object]]:
        root = self.runs_root / "runtime-agents" / target_date.isoformat()
        output: list[dict[str, object]] = []
        for role in RuntimeAgentRole:
            assessments = []
            invalid_count = 0
            for path in root.glob(f"*/{role.value}.json"):
                try:
                    assessments.append(load_runtime_agent_assessment(path))
                except (OSError, ValueError):
                    invalid_count += 1
            current = [
                assessment
                for assessment in assessments
                if assessment.is_current(observed_at_utc)
            ]
            if not assessments and invalid_count == 0:
                status = "unavailable"
            elif invalid_count or len(current) != len(assessments):
                status = "stale_or_invalid"
            elif all(assessment.healthy for assessment in current):
                status = (
                    "blocked"
                    if any(assessment.material_negative for assessment in current)
                    else "healthy"
                )
            else:
                status = "unhealthy"
            latest_generated = max(
                (assessment.generated_at_utc for assessment in assessments),
                default=None,
            )
            output.append(
                {
                    "role": role.value,
                    "status": status,
                    "symbol_count": len(assessments),
                    "current_count": len(current),
                    "invalid_count": invalid_count,
                    "latest_generated_at_utc": (
                        None
                        if latest_generated is None
                        else latest_generated.isoformat()
                    ),
                }
            )
        return output

    def _maturity(self) -> dict[str, object]:
        path = self.runs_root / "maturity-evidence.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        result = {
            name: payload.get(name)
            for name in MATURITY_FIELDS
        }
        result.update(
            {
                "stage": "research_only",
                "paper_eligible": False,
                "live_eligible": False,
            }
        )
        return result

    def _latest_snapshot(
        self,
        *,
        source: str,
        date_column: str,
        not_after: date,
    ) -> SnapshotFrame | None:
        matches: list[SnapshotFrame] = []
        for path in (self.data_root / "accepted").glob(f"{source}-*/data.parquet"):
            try:
                snapshot = DatasetSnapshot.model_validate_json(
                    (path.parent / "manifest.json").read_text(encoding="utf-8")
                )
                snapshot.assert_usable()
                date_frame = pl.read_parquet(path, columns=[date_column])
                dates = date_frame.get_column(date_column).drop_nulls().unique().to_list()
                if len(dates) != 1 or not isinstance(dates[0], date):
                    continue
                session_date = dates[0]
                if session_date > not_after:
                    continue
                matches.append(
                    SnapshotFrame(
                        snapshot=snapshot,
                        frame=pl.read_parquet(path),
                        session_date=session_date,
                    )
                )
            except (OSError, ValueError, pl.exceptions.PolarsError):
                continue
        if not matches:
            return None
        return max(
            matches,
            key=lambda item: (item.session_date, item.snapshot.asof_utc),
        )


def _selection_candidates(frame: pl.DataFrame) -> list[dict[str, object]]:
    if "pass_gate" not in frame.columns:
        return []
    passing = frame.filter(pl.col("pass_gate"))
    if "selection_rank" in passing.columns:
        passing = passing.sort("selection_rank", nulls_last=True)
    output: list[dict[str, object]] = []
    for row in passing.iter_rows(named=True):
        output.append(
            {
                "rank": _integer(row.get("selection_rank")),
                "symbol": _text(row.get("symbol")),
                "route": "catalyst",
                "catalyst_categories": _string_list(
                    row.get("catalyst_categories")
                ),
                "event_count": _integer(row.get("event_count")),
                "earnings_evidence_layers": _integer(
                    row.get("earnings_evidence_layers")
                ),
                "earnings_intensity_score": _number(
                    row.get("earnings_intensity_score")
                ),
                "earnings_strength_confirmed": _boolean(
                    row.get("earnings_strength_confirmed")
                ),
                "rvol": _number(row.get("rvol")),
                "premarket_gap_return": _number(
                    row.get("premarket_gap_return")
                ),
                "premarket_return": _number(row.get("premarket_return")),
                "premarket_close": _number(row.get("premarket_close")),
                "premarket_vwap": _number(row.get("premarket_vwap")),
                "premarket_close_location": _number(
                    row.get("premarket_close_location")
                ),
                "premarket_above_vwap": _boolean(
                    row.get("premarket_above_vwap")
                ),
                "directional_volume_confirmed": _boolean(
                    row.get("directional_volume_confirmed")
                ),
                "market_cap": _number(row.get("market_cap")),
                "adv_usd": _number(row.get("adv_usd")),
                "atr_pct": _number(row.get("atr_pct")),
            }
        )
    return output


def market_phase(
    observed_at_utc: datetime,
    *,
    selection_time_beijing: time = time(20, 0),
    postmarket_data_grace_minutes: int = 20,
) -> dict[str, object]:
    """Return the one desktop action allowed by the exchange-clock window."""

    _require_utc(observed_at_utc)
    if postmarket_data_grace_minutes < 0:
        raise ValueError("postmarket_data_grace_minutes must not be negative")
    local_date = observed_at_utc.astimezone(BEIJING).date()
    schedule = build_xnys_schedule(
        local_date - timedelta(days=7),
        local_date + timedelta(days=10),
    )
    rows = list(schedule.iter_rows(named=True))
    upcoming = next(
        (
            row
            for row in rows
            if isinstance(row.get("trade_date"), date)
            and isinstance(row.get("market_close_utc"), datetime)
            and observed_at_utc <= row["market_close_utc"]
        ),
        None,
    )
    completed = [
        row
        for row in rows
        if isinstance(row.get("trade_date"), date)
        and isinstance(row.get("market_close_utc"), datetime)
        and row["market_close_utc"] < observed_at_utc
    ]
    previous = completed[-1] if completed else None
    if upcoming is None:
        return {"kind": "waiting", "trade_date": None, "next_at_utc": None}

    trade_date = upcoming["trade_date"]
    close = upcoming["market_close_utc"]
    assert isinstance(trade_date, date)
    assert isinstance(close, datetime)
    selection_at = datetime.combine(
        trade_date, selection_time_beijing, BEIJING
    ).astimezone(UTC)
    if selection_at <= observed_at_utc <= close:
        return {
            "kind": "selection",
            "trade_date": trade_date.isoformat(),
            "next_at_utc": close.isoformat(),
        }

    if previous is not None:
        review_date = previous["trade_date"]
        review_close = previous["market_close_utc"]
        assert isinstance(review_date, date)
        assert isinstance(review_close, datetime)
        review_at = review_close + timedelta(minutes=postmarket_data_grace_minutes)
        review_ends = min(
            selection_at,
            review_close + timedelta(hours=6),
        )
        if review_at <= observed_at_utc < review_ends:
            return {
                "kind": "post_close_review",
                "trade_date": review_date.isoformat(),
                "next_at_utc": review_ends.isoformat(),
            }

    return {
        "kind": "waiting",
        "trade_date": trade_date.isoformat(),
        "next_at_utc": selection_at.isoformat(),
    }


def _target_session(observed_at_utc: datetime) -> date:
    local_date = observed_at_utc.astimezone(BEIJING).date()
    schedule = build_xnys_schedule(
        local_date - timedelta(days=4),
        date.fromordinal(local_date.toordinal() + 10),
    )
    for row in schedule.iter_rows(named=True):
        session = row.get("trade_date")
        market_close = row.get("market_close_utc")
        if (
            isinstance(session, date)
            and isinstance(market_close, datetime)
            and observed_at_utc <= market_close
        ):
            return session
    return local_date


def _failed_job(
    jobs: list[dict[str, object]],
    job_name: str,
    trade_date_value: date,
) -> dict[str, object] | None:
    for row in jobs:
        if (
            row.get("job_name") == job_name
            and row.get("trade_date") == trade_date_value.isoformat()
            and row.get("status") == "failed"
        ):
            return row
    return None


def _pipeline_status(
    selection: dict[str, object],
    jobs: list[dict[str, object]],
) -> str:
    if selection.get("status") == "blocked":
        return "degraded"
    target = selection.get("target_trade_date")
    for row in jobs:
        if (
            row.get("trade_date") == target
            and row.get("status") == "failed"
            and row.get("job_name") != "premarket_multisignal_shadow"
        ):
            return "degraded"
    if selection.get("status") == "ready":
        return "ready"
    return "waiting"


def _boolean(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _integer(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _text(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _require_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("client evidence timestamp must be UTC")
