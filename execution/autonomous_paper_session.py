"""Fail-closed orchestration for the bounded autonomous Alpaca Paper session."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal, InvalidOperation
from enum import Enum, StrEnum
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from execution.account_guardian import (
    AccountGuardian,
    AccountGuardianLedger,
    AccountGuardianStatus,
)
from execution.alpaca_paper import (
    BrokerOrder,
    PaperAccount,
    PaperCloseRequest,
    PaperExtendedLimitRequest,
    PaperOrderRequest,
    PaperPosition,
    PaperStopRequest,
)
from execution.premarket_entry import (
    PremarketEntryAction,
    PremarketEntryEngine,
    PremarketEntryPlan,
    PremarketEntryRuntime,
    PremarketEntrySnapshot,
)
from execution.synthetic_stop import (
    StopAction,
    SyntheticStopPlan,
    SyntheticStopSnapshot,
)
from execution.synthetic_stop_controller import (
    SyntheticStopController,
    SyntheticStopExecutionLedger,
    SyntheticStopExecutionResult,
)
from kernel.intraday_policy import (
    IntradayPolicy,
    PolicyAction,
    PolicyDecision,
    PolicySnapshot,
    TailEvidence,
    TailMode,
)


class SessionAction(StrEnum):
    OBSERVE = "observe"
    DATA_BLOCKED = "data_blocked"
    WRITES_BLOCKED = "writes_blocked"
    SOFT_LOSS_BLOCK = "soft_loss_block"
    ENTRY_SUBMITTED = "entry_submitted"
    REDUCE_SUBMITTED = "reduce_submitted"
    EXIT_SUBMITTED = "exit_submitted"
    STOP_EXIT_SUBMITTED = "stop_exit_submitted"
    PROTECTION_SUBMITTED = "protection_submitted"
    ACCOUNT_GUARDIAN_LOCK = "account_guardian_lock"
    HARD_LOSS_FLATTEN = "hard_loss_flatten"
    DAY_LOCKED = "day_locked"


@dataclass(frozen=True)
class AutonomousPaperPlan:
    plan_id: str
    symbol: str
    trade_date: date
    reference_price: Decimal
    hard_stop: Decimal
    max_notional_fraction: Decimal
    full_risk_fraction: Decimal
    source_snapshot_ids: tuple[str, ...]
    provenance: str
    max_spread_ratio: Decimal = Decimal("0.0025")
    take_profit_1: Decimal | None = None
    take_profit_2: Decimal | None = None

    def __post_init__(self) -> None:
        if not self.plan_id.strip():
            raise ValueError("autonomous Paper plan_id is required")
        if not self.symbol or self.symbol != self.symbol.strip().upper():
            raise ValueError("autonomous Paper symbol must be normalized uppercase")
        for name, price_value in (
            ("reference_price", self.reference_price),
            ("hard_stop", self.hard_stop),
            ("max_notional_fraction", self.max_notional_fraction),
            ("full_risk_fraction", self.full_risk_fraction),
            ("max_spread_ratio", self.max_spread_ratio),
        ):
            if not price_value.is_finite() or price_value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        for name, optional_price in (
            ("take_profit_1", self.take_profit_1),
            ("take_profit_2", self.take_profit_2),
        ):
            if optional_price is not None and (
                not optional_price.is_finite() or optional_price <= 0
            ):
                raise ValueError(f"{name} must be finite and positive when available")
        if self.hard_stop >= self.reference_price:
            raise ValueError("hard_stop must be below reference_price for long-only plans")
        if self.take_profit_1 is not None and self.take_profit_1 <= self.reference_price:
            raise ValueError("take_profit_1 must be above reference_price")
        if self.take_profit_2 is not None and self.take_profit_2 <= self.reference_price:
            raise ValueError("take_profit_2 must be above reference_price")
        if (
            self.take_profit_1 is not None
            and self.take_profit_2 is not None
            and self.take_profit_2 < self.take_profit_1
        ):
            raise ValueError("take_profit_2 must not be below take_profit_1")
        if self.max_notional_fraction > Decimal("1"):
            raise ValueError("max_notional_fraction cannot exceed one")
        if self.full_risk_fraction > Decimal("0.0035"):
            raise ValueError("full_risk_fraction cannot exceed 0.35%")
        if self.max_spread_ratio > Decimal("0.01"):
            raise ValueError("max_spread_ratio cannot exceed 1%")
        if not self.source_snapshot_ids or any(
            not item.strip() for item in self.source_snapshot_ids
        ):
            raise ValueError("source snapshot IDs are required")
        if not self.provenance.strip():
            raise ValueError("autonomous Paper plan provenance is required")


@dataclass(frozen=True)
class PaperSessionSnapshot:
    policy: PolicySnapshot
    bid: Decimal
    ask: Decimal
    quote_asof_utc: datetime
    quote_provenance: str
    last_trade: Decimal | None = None
    last_trade_asof_utc: datetime | None = None
    trade_provenance: str | None = None
    halt_risk: bool = False
    below_anchored_vwap_5m_bars: int = 0
    failed_vwap_reclaim: bool = False
    chandelier_stop_hit: bool = False
    tail_hard_breakdown: bool = False

    def __post_init__(self) -> None:
        _require_utc(self.quote_asof_utc)
        if self.quote_asof_utc > self.policy.observed_at_utc:
            raise ValueError("Paper quote cannot be from the future")
        if (
            not self.bid.is_finite()
            or not self.ask.is_finite()
            or self.bid <= 0
            or self.ask < self.bid
        ):
            raise ValueError("Paper NBBO is invalid")
        if not self.quote_provenance.strip():
            raise ValueError("Paper quote provenance is required")
        if self.last_trade_asof_utc is not None:
            _require_utc(self.last_trade_asof_utc)
            if self.last_trade_asof_utc > self.policy.observed_at_utc:
                raise ValueError("Paper trade cannot be from the future")
        if (self.last_trade is None) != (self.last_trade_asof_utc is None):
            raise ValueError("Paper last trade and timestamp must be provided together")
        if self.last_trade is not None and (
            not self.last_trade.is_finite() or self.last_trade <= 0
        ):
            raise ValueError("Paper last trade is invalid")
        if self.last_trade is not None and not (self.trade_provenance or "").strip():
            raise ValueError("Paper trade provenance is required")
        if self.below_anchored_vwap_5m_bars < 0:
            raise ValueError("Paper below-VWAP bar count cannot be negative")


@dataclass(frozen=True)
class PaperSessionResult:
    action: SessionAction
    decision: PolicyDecision | None
    daily_return: Decimal
    day_locked: bool
    new_entries_allowed: bool
    cancelled_order_ids: tuple[str, ...]
    flatten_order_ids: tuple[str, ...]
    reasons: tuple[str, ...]
    provenance: str
    submitted_order_ids: tuple[str, ...] = ()


class AutonomousPaperBroker(Protocol):
    writes_enabled: bool

    def get_account(self) -> PaperAccount: ...

    def list_positions(self) -> tuple[PaperPosition, ...]: ...

    def list_open_orders(self) -> tuple[BrokerOrder, ...]: ...

    def get_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None: ...

    def cancel_order(self, order_id: str) -> bool: ...

    def submit_close_order_idempotent(self, request: PaperCloseRequest) -> BrokerOrder: ...

    def submit_order_idempotent(self, request: PaperOrderRequest) -> BrokerOrder: ...

    def submit_stop_order_idempotent(self, request: PaperStopRequest) -> BrokerOrder: ...

    def submit_extended_limit_idempotent(
        self, request: PaperExtendedLimitRequest
    ) -> BrokerOrder: ...


@dataclass(frozen=True)
class _CommandRecord:
    command_id: str
    broker_order_id: str | None
    completed: bool


@dataclass(frozen=True)
class _PremarketRecord:
    plan: PremarketEntryPlan
    runtime: PremarketEntryRuntime
    active_client_order_id: str | None
    active_broker_order_id: str | None


@dataclass(frozen=True)
class _TailRuntimeState:
    maximum_favorable_excursion_r: float
    order_flow_below_45_seconds: int
    last_observed_at_utc: datetime
    order_flow_below_active: bool


@dataclass(frozen=True)
class PaperPlanEvaluationSummary:
    plan_id: str
    symbol: str
    trade_date: date
    evaluation_count: int
    observe_count: int
    data_blocked_count: int
    runtime_failure_count: int
    submitted_order_count: int
    first_observed_at_utc: datetime
    last_observed_at_utc: datetime
    last_action: str
    last_reasons: tuple[str, ...]


class PaperSessionLedger:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS paper_session_days (
                    trade_date TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_session_commands (
                    command_id TEXT PRIMARY KEY,
                    trade_date TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    broker_order_id TEXT,
                    completed INTEGER NOT NULL,
                    updated_at_utc TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_premarket_entries (
                    plan_id TEXT PRIMARY KEY,
                    plan_json TEXT NOT NULL,
                    runtime_json TEXT NOT NULL,
                    active_client_order_id TEXT,
                    active_broker_order_id TEXT,
                    updated_at_utc TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_position_lifecycle (
                    plan_id TEXT PRIMARY KEY,
                    main_profit_realized INTEGER NOT NULL,
                    updated_at_utc TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_tail_runtime (
                    plan_id TEXT PRIMARY KEY,
                    maximum_favorable_excursion_r REAL NOT NULL,
                    order_flow_below_45_seconds INTEGER NOT NULL,
                    last_observed_at_utc TEXT NOT NULL,
                    order_flow_below_active INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_autopilot_audit_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    occurred_at_utc TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL UNIQUE
                );
                CREATE INDEX IF NOT EXISTS idx_paper_autopilot_audit_run_sequence
                    ON paper_autopilot_audit_events (run_id, sequence);
                CREATE TABLE IF NOT EXISTS paper_plan_evaluation_summary (
                    plan_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    evaluation_count INTEGER NOT NULL,
                    observe_count INTEGER NOT NULL,
                    data_blocked_count INTEGER NOT NULL,
                    runtime_failure_count INTEGER NOT NULL,
                    submitted_order_count INTEGER NOT NULL,
                    first_observed_at_utc TEXT NOT NULL,
                    last_observed_at_utc TEXT NOT NULL,
                    last_action TEXT NOT NULL,
                    last_reasons_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_paper_plan_evaluation_trade_date
                    ON paper_plan_evaluation_summary (trade_date, symbol);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def day_locked(self, trade_date: date) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM paper_session_days WHERE trade_date=?",
                (trade_date.isoformat(),),
            ).fetchone()
        return row is not None and str(row["status"]) == "locked"

    def lock_day(self, trade_date: date, *, reason: str, at_utc: datetime) -> None:
        _require_utc(at_utc)
        if not reason.strip():
            raise ValueError("Paper day-lock reason is required")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO paper_session_days (
                    trade_date, status, reason, updated_at_utc
                ) VALUES (?, 'locked', ?, ?)
                ON CONFLICT(trade_date) DO UPDATE SET
                    status='locked',
                    reason=excluded.reason,
                    updated_at_utc=excluded.updated_at_utc
                """,
                (trade_date.isoformat(), reason, at_utc.isoformat()),
            )

    def record_plan_evaluation(
        self,
        plan: AutonomousPaperPlan,
        *,
        action: SessionAction,
        reasons: tuple[str, ...],
        degraded_reasons: tuple[str, ...],
        submitted_order_ids: tuple[str, ...],
        at_utc: datetime,
    ) -> None:
        """Aggregate runtime evidence without persisting per-second market facts."""

        _require_utc(at_utc)
        reason_values = tuple(dict.fromkeys((*reasons, *degraded_reasons)))
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO paper_plan_evaluation_summary (
                    plan_id, symbol, trade_date, evaluation_count, observe_count,
                    data_blocked_count, runtime_failure_count, submitted_order_count,
                    first_observed_at_utc, last_observed_at_utc, last_action,
                    last_reasons_json
                ) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(plan_id) DO UPDATE SET
                    evaluation_count=evaluation_count + 1,
                    observe_count=observe_count + excluded.observe_count,
                    data_blocked_count=data_blocked_count + excluded.data_blocked_count,
                    runtime_failure_count=(
                        runtime_failure_count + excluded.runtime_failure_count
                    ),
                    submitted_order_count=(
                        submitted_order_count + excluded.submitted_order_count
                    ),
                    last_observed_at_utc=excluded.last_observed_at_utc,
                    last_action=excluded.last_action,
                    last_reasons_json=excluded.last_reasons_json
                """,
                (
                    plan.plan_id,
                    plan.symbol,
                    plan.trade_date.isoformat(),
                    int(action is SessionAction.OBSERVE),
                    int(action is SessionAction.DATA_BLOCKED),
                    int(bool(degraded_reasons)),
                    len(submitted_order_ids),
                    at_utc.isoformat(),
                    at_utc.isoformat(),
                    action.value,
                    json.dumps(reason_values, ensure_ascii=False),
                ),
            )

    def plan_evaluation_summaries(self, trade_date: date) -> tuple[PaperPlanEvaluationSummary, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM paper_plan_evaluation_summary
                WHERE trade_date=? ORDER BY symbol, plan_id
                """,
                (trade_date.isoformat(),),
            ).fetchall()
        return tuple(
            PaperPlanEvaluationSummary(
                plan_id=str(row["plan_id"]),
                symbol=str(row["symbol"]),
                trade_date=date.fromisoformat(str(row["trade_date"])),
                evaluation_count=int(row["evaluation_count"]),
                observe_count=int(row["observe_count"]),
                data_blocked_count=int(row["data_blocked_count"]),
                runtime_failure_count=int(row["runtime_failure_count"]),
                submitted_order_count=int(row["submitted_order_count"]),
                first_observed_at_utc=datetime.fromisoformat(str(row["first_observed_at_utc"])),
                last_observed_at_utc=datetime.fromisoformat(str(row["last_observed_at_utc"])),
                last_action=str(row["last_action"]),
                last_reasons=tuple(json.loads(str(row["last_reasons_json"]))),
            )
            for row in rows
        )

    def record_audit_event(
        self,
        *,
        run_id: str,
        event_type: str,
        at_utc: datetime,
        payload: dict[str, object],
    ) -> dict[str, object]:
        """Append one durable, secret-free event to the Paper audit chain."""

        _require_utc(at_utc)
        if not run_id.strip() or not event_type.strip():
            raise ValueError("Paper audit identity is required")
        _reject_audit_secrets(payload)
        try:
            payload_json = json.dumps(
                _audit_json_safe(payload),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("Paper audit payload is not JSON-safe") from exc
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                """
                SELECT event_hash FROM paper_autopilot_audit_events
                ORDER BY sequence DESC LIMIT 1
                """
            ).fetchone()
            previous_hash = "GENESIS" if prior is None else str(prior["event_hash"])
            event_hash = hashlib.sha256(
                "|".join(
                    (
                        previous_hash,
                        run_id,
                        event_type,
                        at_utc.isoformat(),
                        payload_json,
                    )
                ).encode("utf-8")
            ).hexdigest()
            cursor = connection.execute(
                """
                INSERT INTO paper_autopilot_audit_events (
                    run_id, event_type, occurred_at_utc, payload_json,
                    previous_hash, event_hash
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    event_type,
                    at_utc.isoformat(),
                    payload_json,
                    previous_hash,
                    event_hash,
                ),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("Paper audit insert did not return a sequence")
            sequence = int(cursor.lastrowid)
        return {
            "sequence": sequence,
            "run_id": run_id,
            "event_type": event_type,
            "occurred_at_utc": at_utc.isoformat(),
            "payload": json.loads(payload_json),
            "previous_hash": previous_hash,
            "event_hash": event_hash,
        }

    def audit_events(self, *, run_id: str, limit: int = 1_000) -> tuple[dict[str, object], ...]:
        if not run_id.strip():
            raise ValueError("Paper audit run ID is required")
        if limit < 1 or limit > 10_000:
            raise ValueError("Paper audit limit must be in [1, 10000]")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT sequence, run_id, event_type, occurred_at_utc, payload_json,
                       previous_hash, event_hash
                FROM paper_autopilot_audit_events
                WHERE run_id=?
                ORDER BY sequence
                LIMIT ?
                """,
                (run_id, limit),
            ).fetchall()
        return tuple(
            {
                "sequence": int(row["sequence"]),
                "run_id": str(row["run_id"]),
                "event_type": str(row["event_type"]),
                "occurred_at_utc": str(row["occurred_at_utc"]),
                "payload": json.loads(str(row["payload_json"])),
                "previous_hash": str(row["previous_hash"]),
                "event_hash": str(row["event_hash"]),
            }
            for row in rows
        )

    def ensure_command(
        self,
        command_id: str,
        *,
        trade_date: date,
        kind: str,
        symbol: str,
        at_utc: datetime,
    ) -> _CommandRecord:
        _require_utc(at_utc)
        if not all(value.strip() for value in (command_id, kind, symbol)):
            raise ValueError("Paper command identity is incomplete")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT OR IGNORE INTO paper_session_commands (
                    command_id, trade_date, kind, symbol,
                    broker_order_id, completed, updated_at_utc
                ) VALUES (?, ?, ?, ?, NULL, 0, ?)
                """,
                (
                    command_id,
                    trade_date.isoformat(),
                    kind,
                    symbol,
                    at_utc.isoformat(),
                ),
            )
            row = connection.execute(
                "SELECT * FROM paper_session_commands WHERE command_id=?",
                (command_id,),
            ).fetchone()
        if (
            row is None
            or str(row["trade_date"]) != trade_date.isoformat()
            or str(row["kind"]) != kind
            or str(row["symbol"]) != symbol
        ):
            raise RuntimeError("Paper command identity conflict")
        return _CommandRecord(
            command_id=command_id,
            broker_order_id=(
                str(row["broker_order_id"]) if row["broker_order_id"] is not None else None
            ),
            completed=bool(row["completed"]),
        )

    def complete_command(
        self,
        command_id: str,
        *,
        at_utc: datetime,
        broker_order_id: str | None = None,
    ) -> None:
        _require_utc(at_utc)
        with self._connect() as connection:
            changed = connection.execute(
                """
                UPDATE paper_session_commands
                SET completed=1,
                    broker_order_id=COALESCE(?, broker_order_id),
                    updated_at_utc=?
                WHERE command_id=?
                """,
                (broker_order_id, at_utc.isoformat(), command_id),
            ).rowcount
        if changed != 1:
            raise KeyError(f"unknown Paper command: {command_id}")

    def ensure_premarket(
        self,
        plan: PremarketEntryPlan,
        *,
        at_utc: datetime,
    ) -> _PremarketRecord:
        _require_utc(at_utc)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT OR IGNORE INTO paper_premarket_entries (
                    plan_id, plan_json, runtime_json,
                    active_client_order_id, active_broker_order_id,
                    updated_at_utc
                ) VALUES (?, ?, ?, NULL, NULL, ?)
                """,
                (
                    plan.plan_id,
                    _premarket_plan_json(plan),
                    _premarket_runtime_json(PremarketEntryRuntime.initial()),
                    at_utc.isoformat(),
                ),
            )
            row = connection.execute(
                "SELECT * FROM paper_premarket_entries WHERE plan_id=?",
                (plan.plan_id,),
            ).fetchone()
        record = _premarket_record(row)
        if record.plan != plan:
            raise RuntimeError("premarket entry plan identity conflict")
        return record

    def get_premarket(self, plan_id: str) -> _PremarketRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM paper_premarket_entries WHERE plan_id=?",
                (plan_id,),
            ).fetchone()
        return None if row is None else _premarket_record(row)

    def save_premarket(
        self,
        plan_id: str,
        *,
        runtime: PremarketEntryRuntime,
        active_client_order_id: str | None,
        active_broker_order_id: str | None,
        at_utc: datetime,
    ) -> _PremarketRecord:
        _require_utc(at_utc)
        with self._connect() as connection:
            changed = connection.execute(
                """
                UPDATE paper_premarket_entries
                SET runtime_json=?, active_client_order_id=?,
                    active_broker_order_id=?, updated_at_utc=?
                WHERE plan_id=?
                """,
                (
                    _premarket_runtime_json(runtime),
                    active_client_order_id,
                    active_broker_order_id,
                    at_utc.isoformat(),
                    plan_id,
                ),
            ).rowcount
            row = connection.execute(
                "SELECT * FROM paper_premarket_entries WHERE plan_id=?",
                (plan_id,),
            ).fetchone()
        if changed != 1:
            raise KeyError(f"unknown premarket entry plan: {plan_id}")
        return _premarket_record(row)

    def entry_component_client_ids(
        self,
        *,
        trade_date: date,
        symbol: str,
        component: str,
    ) -> tuple[str, ...]:
        if component not in {"main", "tail"}:
            raise ValueError("entry component must be main or tail")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT command_id
                FROM paper_session_commands
                WHERE trade_date=?
                  AND symbol=?
                  AND completed=1
                  AND kind IN (?, ?)
                ORDER BY command_id
                """,
                (
                    trade_date.isoformat(),
                    symbol,
                    f"regular_{PolicyAction.ENTER_PROBE.value}_{component}",
                    f"regular_{PolicyAction.UPGRADE.value}_{component}",
                ),
            ).fetchall()
        return tuple(str(row["command_id"]) for row in rows)

    def main_profit_realized(self, plan_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT main_profit_realized
                FROM paper_position_lifecycle
                WHERE plan_id=?
                """,
                (plan_id,),
            ).fetchone()
        return row is not None and bool(row["main_profit_realized"])

    def mark_main_profit_realized(
        self,
        plan_id: str,
        *,
        at_utc: datetime,
    ) -> None:
        _require_utc(at_utc)
        if not plan_id.strip():
            raise ValueError("Paper lifecycle plan ID is required")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO paper_position_lifecycle (
                    plan_id, main_profit_realized, updated_at_utc
                ) VALUES (?, 1, ?)
                ON CONFLICT(plan_id) DO UPDATE SET
                    main_profit_realized=1,
                    updated_at_utc=excluded.updated_at_utc
                """,
                (plan_id, at_utc.isoformat()),
            )

    def advance_tail_runtime(
        self,
        plan_id: str,
        *,
        observed_at_utc: datetime,
        current_r: float,
        order_flow_score: float | None,
    ) -> _TailRuntimeState:
        _require_utc(observed_at_utc)
        if not plan_id.strip():
            raise ValueError("Paper tail runtime plan ID is required")
        if not Decimal(str(current_r)).is_finite():
            raise ValueError("Paper tail current R must be finite")
        below_active = order_flow_score is not None and order_flow_score < 45.0
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM paper_tail_runtime WHERE plan_id=?",
                (plan_id,),
            ).fetchone()
            if row is None:
                maximum = current_r
                seconds = 0
            else:
                prior_observed = datetime.fromisoformat(str(row["last_observed_at_utc"]))
                if observed_at_utc < prior_observed:
                    raise ValueError("Paper tail observation cannot move backwards")
                maximum = max(
                    float(row["maximum_favorable_excursion_r"]),
                    current_r,
                )
                gap = int((observed_at_utc - prior_observed).total_seconds())
                if below_active and bool(row["order_flow_below_active"]) and 0 <= gap <= 30:
                    seconds = int(row["order_flow_below_45_seconds"]) + gap
                else:
                    seconds = 0
            connection.execute(
                """
                INSERT INTO paper_tail_runtime (
                    plan_id, maximum_favorable_excursion_r,
                    order_flow_below_45_seconds, last_observed_at_utc,
                    order_flow_below_active
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(plan_id) DO UPDATE SET
                    maximum_favorable_excursion_r=excluded.maximum_favorable_excursion_r,
                    order_flow_below_45_seconds=excluded.order_flow_below_45_seconds,
                    last_observed_at_utc=excluded.last_observed_at_utc,
                    order_flow_below_active=excluded.order_flow_below_active
                """,
                (
                    plan_id,
                    maximum,
                    seconds,
                    observed_at_utc.isoformat(),
                    int(below_active),
                ),
            )
        return _TailRuntimeState(
            maximum_favorable_excursion_r=maximum,
            order_flow_below_45_seconds=seconds,
            last_observed_at_utc=observed_at_utc,
            order_flow_below_active=below_active,
        )


class PaperSessionOrchestrator:
    _SOFT_DAILY_LOSS = Decimal("-0.015")
    _HARD_DAILY_LOSS = Decimal("-0.02")

    def __init__(
        self,
        *,
        broker: AutonomousPaperBroker,
        ledger: PaperSessionLedger,
        paper_authorized: bool,
        owned_symbols: frozenset[str],
        policy: IntradayPolicy | None = None,
    ):
        if not owned_symbols or any(symbol != symbol.strip().upper() for symbol in owned_symbols):
            raise ValueError("owned Paper symbols must be normalized and non-empty")
        self.broker = broker
        self.ledger = ledger
        self.paper_authorized = paper_authorized
        self.owned_symbols = owned_symbols
        self.policy = policy or IntradayPolicy()
        self.premarket_entry_engine = PremarketEntryEngine()
        self.synthetic_stop_controller = SyntheticStopController(
            broker=broker,
            ledger=SyntheticStopExecutionLedger(ledger.path),
            paper_authorized=paper_authorized,
        )
        self.guardian = AccountGuardian(
            broker=broker,
            ledger=AccountGuardianLedger(ledger.path),
            paper_authorized=paper_authorized,
        )

    def fail_closed(
        self,
        plan: AutonomousPaperPlan,
        *,
        observed_at_utc: datetime,
        reason: str,
        exit_bid: Decimal | None = None,
        quote_asof_utc: datetime | None = None,
        quote_provenance: str | None = None,
    ) -> PaperSessionResult:
        """Block entries and close an owned position when upstream evaluation fails."""

        _require_utc(observed_at_utc)
        if plan.symbol not in self.owned_symbols:
            raise ValueError("fail-closed plan symbol is not owned by this session")
        if not reason.strip():
            raise ValueError("fail-closed reason is required")
        account = self.broker.get_account()
        daily_return = _daily_return(account)
        positions = tuple(
            item for item in self.broker.list_positions() if item.symbol == plan.symbol
        )
        if len(positions) > 1:
            raise RuntimeError("Paper broker returned duplicate symbol positions")
        if not positions:
            open_orders = tuple(
                order for order in self.broker.list_open_orders() if order.symbol == plan.symbol
            )
            if open_orders and (not self.paper_authorized or not self.broker.writes_enabled):
                return PaperSessionResult(
                    action=SessionAction.WRITES_BLOCKED,
                    decision=None,
                    daily_return=daily_return,
                    day_locked=self.ledger.day_locked(plan.trade_date),
                    new_entries_allowed=False,
                    cancelled_order_ids=(),
                    flatten_order_ids=(),
                    reasons=(reason, "paper_cancel_writes_not_authorized"),
                    provenance=("execution.autonomous_paper.runtime_failure_blocked.v1"),
                )
            cancelled = tuple(
                order.id
                for order in open_orders
                if self._cancel_order_once(
                    trade_date=plan.trade_date,
                    order=order,
                    now_utc=observed_at_utc,
                )
            )
            return PaperSessionResult(
                action=SessionAction.DATA_BLOCKED,
                decision=None,
                daily_return=daily_return,
                day_locked=self.ledger.day_locked(plan.trade_date),
                new_entries_allowed=False,
                cancelled_order_ids=cancelled,
                flatten_order_ids=(),
                reasons=(reason,),
                provenance="execution.autonomous_paper.runtime_failure_flat.v1",
            )
        if not self.paper_authorized or not self.broker.writes_enabled:
            return PaperSessionResult(
                action=SessionAction.WRITES_BLOCKED,
                decision=None,
                daily_return=daily_return,
                day_locked=self.ledger.day_locked(plan.trade_date),
                new_entries_allowed=False,
                cancelled_order_ids=(),
                flatten_order_ids=(),
                reasons=(reason, "paper_exit_writes_not_authorized"),
                provenance="execution.autonomous_paper.runtime_failure_blocked.v1",
            )

        local_time = observed_at_utc.astimezone(ZoneInfo("America/New_York")).time()
        regular = time(9, 30) <= local_time < time(16)
        if not regular and not _fresh_exit_quote(
            observed_at_utc=observed_at_utc,
            bid=exit_bid,
            quote_asof_utc=quote_asof_utc,
            quote_provenance=quote_provenance,
        ):
            return PaperSessionResult(
                action=SessionAction.DATA_BLOCKED,
                decision=None,
                daily_return=daily_return,
                day_locked=self.ledger.day_locked(plan.trade_date),
                new_entries_allowed=False,
                cancelled_order_ids=(),
                flatten_order_ids=(),
                reasons=(reason, "extended_exit_quote_unavailable"),
                provenance="execution.autonomous_paper.runtime_failure_no_quote.v1",
            )

        cancelled = tuple(
            order.id
            for order in self.broker.list_open_orders()
            if order.symbol == plan.symbol
            and self._cancel_order_once(
                trade_date=plan.trade_date,
                order=order,
                now_utc=observed_at_utc,
            )
        )
        client_id = _fail_closed_client_order_id(plan, reason=reason)
        command = self.ledger.ensure_command(
            client_id,
            trade_date=plan.trade_date,
            kind="runtime_fail_closed",
            symbol=plan.symbol,
            at_utc=observed_at_utc,
        )
        if command.completed:
            broker_order_id = command.broker_order_id
        else:
            quantity = _whole_long_quantity(positions[0])
            if regular:
                order = self.broker.submit_close_order_idempotent(
                    PaperCloseRequest(
                        client_order_id=client_id,
                        symbol=plan.symbol,
                        qty=quantity,
                    )
                )
            else:
                if exit_bid is None:
                    raise RuntimeError("validated extended exit bid disappeared")
                order = self.broker.submit_extended_limit_idempotent(
                    PaperExtendedLimitRequest(
                        client_order_id=client_id,
                        symbol=plan.symbol,
                        qty=quantity,
                        side="sell",
                        limit_price=f"{_marketable_exit_limit(exit_bid):.2f}",
                    )
                )
            broker_order_id = order.id
            self.ledger.complete_command(
                client_id,
                at_utc=observed_at_utc,
                broker_order_id=broker_order_id,
            )
        return PaperSessionResult(
            action=SessionAction.EXIT_SUBMITTED,
            decision=None,
            daily_return=daily_return,
            day_locked=self.ledger.day_locked(plan.trade_date),
            new_entries_allowed=False,
            cancelled_order_ids=cancelled,
            flatten_order_ids=((broker_order_id,) if broker_order_id is not None else ()),
            reasons=(reason,),
            provenance="execution.autonomous_paper.runtime_failure_exit.v1",
            submitted_order_ids=((broker_order_id,) if broker_order_id is not None else ()),
        )

    def tick(
        self,
        plan: AutonomousPaperPlan,
        snapshot: PaperSessionSnapshot,
    ) -> PaperSessionResult:
        self._validate_identity(plan, snapshot)
        account = self.broker.get_account()
        daily_return = _daily_return(account)
        guardian = self.guardian.reconcile(
            trade_date=plan.trade_date,
            now_utc=snapshot.policy.observed_at_utc,
            owned_position_symbols=self.owned_symbols,
        )
        if guardian.status is not AccountGuardianStatus.CLEAR:
            return PaperSessionResult(
                action=(
                    SessionAction.ACCOUNT_GUARDIAN_LOCK
                    if guardian.status is AccountGuardianStatus.DAY_LOCKED
                    and guardian.reasons != ("day_lock_already_active",)
                    else SessionAction.DAY_LOCKED
                ),
                decision=None,
                daily_return=daily_return,
                day_locked=guardian.status is AccountGuardianStatus.DAY_LOCKED,
                new_entries_allowed=False,
                cancelled_order_ids=guardian.cancelled_order_ids,
                flatten_order_ids=guardian.flatten_order_ids,
                reasons=guardian.reasons,
                provenance=guardian.provenance,
            )
        locked = self.ledger.day_locked(plan.trade_date)
        if daily_return <= self._HARD_DAILY_LOSS:
            if not self.paper_authorized or not self.broker.writes_enabled:
                return PaperSessionResult(
                    action=SessionAction.WRITES_BLOCKED,
                    decision=None,
                    daily_return=daily_return,
                    day_locked=locked,
                    new_entries_allowed=False,
                    cancelled_order_ids=(),
                    flatten_order_ids=(),
                    reasons=("daily_hard_loss_writes_not_authorized",),
                    provenance="execution.autonomous_paper.hard_loss_blocked.v1",
                )
            cancelled, flattened = self._cancel_and_flatten(
                plan=plan,
                snapshot=snapshot,
            )
            self.ledger.lock_day(
                plan.trade_date,
                reason="daily_hard_loss_limit_reached",
                at_utc=snapshot.policy.observed_at_utc,
            )
            return PaperSessionResult(
                action=SessionAction.HARD_LOSS_FLATTEN,
                decision=None,
                daily_return=daily_return,
                day_locked=True,
                new_entries_allowed=False,
                cancelled_order_ids=cancelled,
                flatten_order_ids=flattened,
                reasons=("daily_hard_loss_limit_reached",),
                provenance="execution.autonomous_paper.hard_loss_flatten.v1",
            )
        if locked:
            return PaperSessionResult(
                action=SessionAction.DAY_LOCKED,
                decision=None,
                daily_return=daily_return,
                day_locked=True,
                new_entries_allowed=False,
                cancelled_order_ids=(),
                flatten_order_ids=(),
                reasons=("persisted_day_lock",),
                provenance="execution.autonomous_paper.persisted_day_lock.v1",
            )
        snapshot = self._with_lifecycle_state(
            plan=plan,
            snapshot=snapshot,
            account=account,
        )
        decision = self.policy.evaluate(snapshot.policy)
        if (
            snapshot.policy.has_position
            and plan.take_profit_2 is not None
            and snapshot.bid >= plan.take_profit_2
        ):
            decision = PolicyDecision(
                action=PolicyAction.EXIT,
                target_position_fraction=0.0,
                max_account_risk_fraction=0.0,
                reasons=("take_profit_2_reached",),
                blockers=(),
            )
        soft_loss_active = daily_return <= self._SOFT_DAILY_LOSS
        if soft_loss_active and decision.action in {
            PolicyAction.ENTER_PROBE,
            PolicyAction.UPGRADE,
        }:
            return PaperSessionResult(
                action=SessionAction.SOFT_LOSS_BLOCK,
                decision=decision,
                daily_return=daily_return,
                day_locked=False,
                new_entries_allowed=False,
                cancelled_order_ids=(),
                flatten_order_ids=(),
                reasons=("daily_soft_loss_new_entries_disabled",),
                provenance="execution.autonomous_paper.soft_loss_block.v1",
            )
        if decision.action is PolicyAction.EXIT:
            if not self.paper_authorized or not self.broker.writes_enabled:
                return PaperSessionResult(
                    action=SessionAction.WRITES_BLOCKED,
                    decision=decision,
                    daily_return=daily_return,
                    day_locked=False,
                    new_entries_allowed=False,
                    cancelled_order_ids=(),
                    flatten_order_ids=(),
                    reasons=decision.reasons + ("paper_exit_writes_not_authorized",),
                    provenance="execution.autonomous_paper.exit_blocked.v1",
                )
            cancelled, flattened = self._cancel_symbol_and_close(
                plan=plan,
                decision=decision,
                snapshot=snapshot,
            )
            return PaperSessionResult(
                action=SessionAction.EXIT_SUBMITTED,
                decision=decision,
                daily_return=daily_return,
                day_locked=False,
                new_entries_allowed=not soft_loss_active,
                cancelled_order_ids=cancelled,
                flatten_order_ids=flattened,
                reasons=decision.reasons,
                provenance="execution.autonomous_paper.policy_exit.v1",
            )
        protection_required, protective_order_id = self._ensure_premarket_regular_stop(
            plan=plan,
            snapshot=snapshot,
        )
        if protection_required:
            return PaperSessionResult(
                action=SessionAction.WRITES_BLOCKED,
                decision=decision,
                daily_return=daily_return,
                day_locked=False,
                new_entries_allowed=False,
                cancelled_order_ids=(),
                flatten_order_ids=(),
                reasons=("premarket_position_protection_not_authorized",),
                provenance=("execution.autonomous_paper.premarket_protection_blocked.v1"),
            )
        if protective_order_id is not None:
            return PaperSessionResult(
                action=SessionAction.PROTECTION_SUBMITTED,
                decision=decision,
                daily_return=daily_return,
                day_locked=False,
                new_entries_allowed=False,
                cancelled_order_ids=(),
                flatten_order_ids=(),
                reasons=("premarket_regular_stop_attached",),
                provenance=("execution.autonomous_paper.premarket_protection.v1"),
                submitted_order_ids=(protective_order_id,),
            )
        if decision.action in {PolicyAction.ENTER_PROBE, PolicyAction.UPGRADE}:
            quote_age = (snapshot.policy.observed_at_utc - snapshot.quote_asof_utc).total_seconds()
            if not 0 <= quote_age <= 30:
                return PaperSessionResult(
                    action=SessionAction.DATA_BLOCKED,
                    decision=decision,
                    daily_return=daily_return,
                    day_locked=False,
                    new_entries_allowed=False,
                    cancelled_order_ids=(),
                    flatten_order_ids=(),
                    reasons=decision.reasons + ("quote_stale",),
                    provenance="execution.autonomous_paper.stale_quote_block.v1",
                )
            if not self.paper_authorized or not self.broker.writes_enabled:
                return PaperSessionResult(
                    action=SessionAction.WRITES_BLOCKED,
                    decision=decision,
                    daily_return=daily_return,
                    day_locked=False,
                    new_entries_allowed=False,
                    cancelled_order_ids=(),
                    flatten_order_ids=(),
                    reasons=decision.reasons + ("paper_entry_writes_not_authorized",),
                    provenance="execution.autonomous_paper.entry_blocked.v1",
                )
            broker_order_ids = self._submit_entry(
                plan=plan,
                snapshot=snapshot,
                decision=decision,
                account=account,
            )
            if broker_order_ids:
                return PaperSessionResult(
                    action=SessionAction.ENTRY_SUBMITTED,
                    decision=decision,
                    daily_return=daily_return,
                    day_locked=False,
                    new_entries_allowed=True,
                    cancelled_order_ids=(),
                    flatten_order_ids=(),
                    reasons=decision.reasons,
                    provenance="execution.autonomous_paper.entry.v1",
                    submitted_order_ids=broker_order_ids,
                )
        if decision.action in {
            PolicyAction.TRIM_TO_TAIL,
            PolicyAction.REDUCE_TAIL,
        }:
            if not self.paper_authorized or not self.broker.writes_enabled:
                return PaperSessionResult(
                    action=SessionAction.WRITES_BLOCKED,
                    decision=decision,
                    daily_return=daily_return,
                    day_locked=False,
                    new_entries_allowed=False,
                    cancelled_order_ids=(),
                    flatten_order_ids=(),
                    reasons=decision.reasons + ("paper_reduction_writes_not_authorized",),
                    provenance="execution.autonomous_paper.reduction_blocked.v1",
                )
            cancelled, broker_order_id = self._reduce_to_target(
                plan=plan,
                snapshot=snapshot,
                decision=decision,
                account=account,
            )
            if broker_order_id is not None:
                return PaperSessionResult(
                    action=SessionAction.REDUCE_SUBMITTED,
                    decision=decision,
                    daily_return=daily_return,
                    day_locked=False,
                    new_entries_allowed=not soft_loss_active,
                    cancelled_order_ids=cancelled,
                    flatten_order_ids=(broker_order_id,),
                    reasons=decision.reasons,
                    provenance="execution.autonomous_paper.policy_reduction.v1",
                    submitted_order_ids=(broker_order_id,),
                )
        continued_entry_order_id: str | None = None
        active_premarket = self.ledger.get_premarket(f"{plan.plan_id}:premarket-probe")
        local_time = snapshot.policy.observed_at_utc.astimezone(ZoneInfo("America/New_York")).time()
        if (
            active_premarket is not None
            and active_premarket.runtime.completed_at_utc is None
            and time(7) <= local_time < time(9, 25)
        ):
            continued_entry_order_id = self._run_premarket_entry(
                plan=plan,
                snapshot=snapshot,
                target_qty=active_premarket.plan.target_qty,
                filled_qty=_position_quantity(
                    plan.symbol,
                    self.broker.list_positions(),
                ),
            )
        stop_result = self._run_extended_hours_stop(
            plan=plan,
            snapshot=snapshot,
            account=account,
        )
        if stop_result is not None:
            return PaperSessionResult(
                action=SessionAction.STOP_EXIT_SUBMITTED,
                decision=decision,
                daily_return=daily_return,
                day_locked=False,
                new_entries_allowed=not soft_loss_active,
                cancelled_order_ids=(),
                flatten_order_ids=(),
                reasons=stop_result.reasons,
                provenance="execution.autonomous_paper.synthetic_stop.v1",
                submitted_order_ids=(
                    (stop_result.broker_order_id,)
                    if stop_result.broker_order_id is not None
                    else ()
                ),
            )
        if continued_entry_order_id is not None:
            return PaperSessionResult(
                action=SessionAction.ENTRY_SUBMITTED,
                decision=decision,
                daily_return=daily_return,
                day_locked=False,
                new_entries_allowed=not soft_loss_active,
                cancelled_order_ids=(),
                flatten_order_ids=(),
                reasons=("premarket_entry_lifecycle_active",),
                provenance="execution.autonomous_paper.premarket_entry.v1",
                submitted_order_ids=(continued_entry_order_id,),
            )
        return PaperSessionResult(
            action=SessionAction.OBSERVE,
            decision=decision,
            daily_return=daily_return,
            day_locked=False,
            new_entries_allowed=not soft_loss_active,
            cancelled_order_ids=(),
            flatten_order_ids=(),
            reasons=decision.reasons,
            provenance="execution.autonomous_paper.policy_observed.v1",
        )

    def _with_lifecycle_state(
        self,
        *,
        plan: AutonomousPaperPlan,
        snapshot: PaperSessionSnapshot,
        account: PaperAccount,
    ) -> PaperSessionSnapshot:
        if not snapshot.policy.has_position:
            return snapshot
        if snapshot.policy.main_profit_realized or self.ledger.main_profit_realized(plan.plan_id):
            enriched = replace(
                snapshot,
                policy=replace(
                    snapshot.policy,
                    main_profit_realized=True,
                ),
            )
            return self._with_tail_evidence(plan=plan, snapshot=enriched)
        main_client_ids = self.ledger.entry_component_client_ids(
            trade_date=plan.trade_date,
            symbol=plan.symbol,
            component="main",
        )
        if not main_client_ids:
            return snapshot
        main_orders = tuple(
            self.broker.get_order_by_client_id(client_id) for client_id in main_client_ids
        )
        if any(order is None or not _entry_order_filled(order) for order in main_orders):
            return snapshot
        equity = _positive_decimal(account.equity, name="equity")
        tail_qty = _target_quantity(
            plan,
            equity=equity,
            target_fraction=_tail_fraction(snapshot.policy),
        )
        current_qty = _position_quantity(
            plan.symbol,
            self.broker.list_positions(),
        )
        if current_qty <= 0 or current_qty > tail_qty:
            return snapshot
        self.ledger.mark_main_profit_realized(
            plan.plan_id,
            at_utc=snapshot.policy.observed_at_utc,
        )
        enriched = replace(
            snapshot,
            policy=replace(
                snapshot.policy,
                main_profit_realized=True,
            ),
        )
        return self._with_tail_evidence(plan=plan, snapshot=enriched)

    def _with_tail_evidence(
        self,
        *,
        plan: AutonomousPaperPlan,
        snapshot: PaperSessionSnapshot,
    ) -> PaperSessionSnapshot:
        policy = snapshot.policy
        if (
            not policy.main_profit_realized
            or policy.average_entry_price is None
            or policy.last_price is None
        ):
            return snapshot
        tail_mode, initial_fraction = _tail_mode_and_fraction(policy)
        if policy.position_fraction > initial_fraction:
            return snapshot
        risk_per_share = policy.average_entry_price - float(plan.hard_stop)
        if risk_per_share <= 0:
            raise RuntimeError("tail risk per share is not positive")
        current_r = (policy.last_price - policy.average_entry_price) / risk_per_share
        state = self.ledger.advance_tail_runtime(
            plan.plan_id,
            observed_at_utc=policy.observed_at_utc,
            current_r=current_r,
            order_flow_score=policy.order_flow.value,
        )
        reduction_stage = int(policy.position_fraction <= (initial_fraction / 2))
        return replace(
            snapshot,
            policy=replace(
                policy,
                tail=TailEvidence(
                    mode=tail_mode,
                    initial_fraction=initial_fraction,
                    reduction_stage=reduction_stage,
                    fifteen_minute_structure_valid=(policy.technical_structure_valid),
                    below_anchored_vwap_5m_bars=(snapshot.below_anchored_vwap_5m_bars),
                    order_flow_below_45_seconds=(state.order_flow_below_45_seconds),
                    failed_reclaim=snapshot.failed_vwap_reclaim,
                    current_r=current_r,
                    maximum_favorable_excursion_r=(state.maximum_favorable_excursion_r),
                    chandelier_stop_hit=snapshot.chandelier_stop_hit,
                    hard_breakdown=snapshot.tail_hard_breakdown,
                ),
            ),
        )

    def _ensure_premarket_regular_stop(
        self,
        *,
        plan: AutonomousPaperPlan,
        snapshot: PaperSessionSnapshot,
    ) -> tuple[bool, str | None]:
        now_utc = snapshot.policy.observed_at_utc
        local_time = now_utc.astimezone(ZoneInfo("America/New_York")).time()
        if not time(9, 30) <= local_time < time(16):
            return False, None
        premarket = self.ledger.get_premarket(f"{plan.plan_id}:premarket-probe")
        if premarket is None:
            return False, None
        position_qty = _position_quantity(
            plan.symbol,
            self.broker.list_positions(),
        )
        if position_qty <= 0:
            return False, None
        protected_qty = min(position_qty, premarket.plan.target_qty)
        client_id = _premarket_protection_client_order_id(
            plan,
            qty=protected_qty,
        )
        existing = self.broker.get_order_by_client_id(client_id)
        if existing is not None:
            if existing.status.strip().lower() in {
                "new",
                "accepted",
                "pending_new",
                "partially_filled",
                "held",
            }:
                return False, None
            raise RuntimeError("premarket protective stop is no longer active")
        if not self.paper_authorized or not self.broker.writes_enabled:
            return True, None
        command = self.ledger.ensure_command(
            client_id,
            trade_date=plan.trade_date,
            kind="premarket_regular_protection",
            symbol=plan.symbol,
            at_utc=now_utc,
        )
        if command.completed:
            raise RuntimeError("premarket protective stop disappeared after submission")
        order = self.broker.submit_stop_order_idempotent(
            PaperStopRequest(
                client_order_id=client_id,
                symbol=plan.symbol,
                qty=protected_qty,
                stop_price=f"{plan.hard_stop:.2f}",
            )
        )
        self.ledger.complete_command(
            client_id,
            at_utc=now_utc,
            broker_order_id=order.id,
        )
        return False, order.id

    @staticmethod
    def _validate_identity(
        plan: AutonomousPaperPlan,
        snapshot: PaperSessionSnapshot,
    ) -> None:
        if snapshot.policy.trade_date != plan.trade_date:
            raise ValueError("Paper plan and policy trade dates do not match")

    def _cancel_and_flatten(
        self,
        *,
        plan: AutonomousPaperPlan,
        snapshot: PaperSessionSnapshot,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        trade_date = plan.trade_date
        now_utc = snapshot.policy.observed_at_utc
        cancelled: list[str] = []
        for order in self.broker.list_open_orders():
            command_id = f"cancel:{trade_date.isoformat()}:{order.id}"
            record = self.ledger.ensure_command(
                command_id,
                trade_date=trade_date,
                kind="cancel",
                symbol=order.symbol,
                at_utc=now_utc,
            )
            if record.completed:
                continue
            if self.broker.cancel_order(order.id):
                self.ledger.complete_command(command_id, at_utc=now_utc)
                cancelled.append(order.id)

        flattened: list[str] = []
        for position in self.broker.list_positions():
            if position.symbol != plan.symbol:
                continue
            qty = _whole_long_quantity(position)
            client_id = _flatten_client_order_id(trade_date, position.symbol)
            record = self.ledger.ensure_command(
                client_id,
                trade_date=trade_date,
                kind="hard_loss_flatten",
                symbol=position.symbol,
                at_utc=now_utc,
            )
            if record.completed:
                continue
            local_time = now_utc.astimezone(ZoneInfo("America/New_York")).time()
            if time(9, 30) <= local_time < time(16):
                broker_order = self.broker.submit_close_order_idempotent(
                    PaperCloseRequest(
                        client_order_id=client_id,
                        symbol=position.symbol,
                        qty=qty,
                    )
                )
            else:
                broker_order = self.broker.submit_extended_limit_idempotent(
                    PaperExtendedLimitRequest(
                        client_order_id=client_id,
                        symbol=position.symbol,
                        qty=qty,
                        side="sell",
                        limit_price=(f"{_marketable_exit_limit(snapshot.bid):.2f}"),
                    )
                )
            self.ledger.complete_command(
                client_id,
                at_utc=now_utc,
                broker_order_id=broker_order.id,
            )
            flattened.append(broker_order.id)
        return tuple(cancelled), tuple(flattened)

    def _cancel_symbol_and_close(
        self,
        *,
        plan: AutonomousPaperPlan,
        decision: PolicyDecision,
        snapshot: PaperSessionSnapshot,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        now_utc = snapshot.policy.observed_at_utc
        cancelled: list[str] = []
        for order in self.broker.list_open_orders():
            if order.symbol != plan.symbol:
                continue
            command_id = f"cancel:{plan.trade_date.isoformat()}:{order.id}"
            record = self.ledger.ensure_command(
                command_id,
                trade_date=plan.trade_date,
                kind="cancel",
                symbol=order.symbol,
                at_utc=now_utc,
            )
            if record.completed:
                continue
            if self.broker.cancel_order(order.id):
                self.ledger.complete_command(command_id, at_utc=now_utc)
                cancelled.append(order.id)

        matching = [
            position for position in self.broker.list_positions() if position.symbol == plan.symbol
        ]
        if len(matching) > 1:
            raise RuntimeError("Paper broker returned duplicate symbol positions")
        if not matching:
            return tuple(cancelled), ()
        qty = _whole_long_quantity(matching[0])
        client_id = _decision_close_client_order_id(plan, decision)
        record = self.ledger.ensure_command(
            client_id,
            trade_date=plan.trade_date,
            kind="policy_exit",
            symbol=plan.symbol,
            at_utc=now_utc,
        )
        if record.completed:
            return tuple(cancelled), ()
        local_time = now_utc.astimezone(ZoneInfo("America/New_York")).time()
        if time(9, 30) <= local_time < time(16):
            broker_order = self.broker.submit_close_order_idempotent(
                PaperCloseRequest(
                    client_order_id=client_id,
                    symbol=plan.symbol,
                    qty=qty,
                )
            )
        else:
            broker_order = self.broker.submit_extended_limit_idempotent(
                PaperExtendedLimitRequest(
                    client_order_id=client_id,
                    symbol=plan.symbol,
                    qty=qty,
                    side="sell",
                    limit_price=f"{_marketable_exit_limit(snapshot.bid):.2f}",
                )
            )
        self.ledger.complete_command(
            client_id,
            at_utc=now_utc,
            broker_order_id=broker_order.id,
        )
        return tuple(cancelled), (broker_order.id,)

    def _reduce_to_target(
        self,
        *,
        plan: AutonomousPaperPlan,
        snapshot: PaperSessionSnapshot,
        decision: PolicyDecision,
        account: PaperAccount,
    ) -> tuple[tuple[str, ...], str | None]:
        now_utc = snapshot.policy.observed_at_utc
        equity = _positive_decimal(account.equity, name="equity")
        target_qty = _target_quantity(
            plan,
            equity=equity,
            target_fraction=decision.target_position_fraction,
        )
        current_qty = _position_quantity(plan.symbol, self.broker.list_positions())
        reduction_qty = current_qty - target_qty
        if reduction_qty <= 0:
            return (), None
        cancelled: list[str] = []
        for order in self.broker.list_open_orders():
            if order.symbol != plan.symbol:
                continue
            if self._cancel_order_once(
                trade_date=plan.trade_date,
                order=order,
                now_utc=now_utc,
            ):
                cancelled.append(order.id)
        client_id = _reduction_client_order_id(
            plan,
            decision,
            target_qty=target_qty,
        )
        command = self.ledger.ensure_command(
            client_id,
            trade_date=plan.trade_date,
            kind=decision.action.value,
            symbol=plan.symbol,
            at_utc=now_utc,
        )
        if command.completed:
            return tuple(cancelled), command.broker_order_id
        local_time = now_utc.astimezone(ZoneInfo("America/New_York")).time()
        if time(9, 30) <= local_time < time(16):
            order = self.broker.submit_close_order_idempotent(
                PaperCloseRequest(
                    client_order_id=client_id,
                    symbol=plan.symbol,
                    qty=reduction_qty,
                )
            )
        else:
            order = self.broker.submit_extended_limit_idempotent(
                PaperExtendedLimitRequest(
                    client_order_id=client_id,
                    symbol=plan.symbol,
                    qty=reduction_qty,
                    side="sell",
                    limit_price=f"{_marketable_exit_limit(snapshot.bid):.2f}",
                )
            )
        self.ledger.complete_command(
            client_id,
            at_utc=now_utc,
            broker_order_id=order.id,
        )
        return tuple(cancelled), order.id

    def _submit_entry(
        self,
        *,
        plan: AutonomousPaperPlan,
        snapshot: PaperSessionSnapshot,
        decision: PolicyDecision,
        account: PaperAccount,
    ) -> tuple[str, ...]:
        now_utc = snapshot.policy.observed_at_utc
        quote_age = (now_utc - snapshot.quote_asof_utc).total_seconds()
        if not 0 <= quote_age <= 30:
            return ()
        local_time = now_utc.astimezone(ZoneInfo("America/New_York")).time()
        premarket = time(7) <= local_time < time(9, 25)
        regular = time(9, 30) <= local_time < time(16)
        if not premarket and not regular:
            return ()
        if (
            account.status.strip().upper() != "ACTIVE"
            or account.account_blocked
            or account.trading_blocked
        ):
            return ()
        equity = _positive_decimal(account.equity, name="equity")
        desired_qty = _target_quantity(
            plan,
            equity=equity,
            target_fraction=decision.target_position_fraction,
        )
        current_qty = _position_quantity(plan.symbol, self.broker.list_positions())
        delta = desired_qty - current_qty
        if delta <= 0:
            return ()
        if premarket:
            order_id = self._run_premarket_entry(
                plan=plan,
                snapshot=snapshot,
                target_qty=desired_qty,
                filled_qty=current_qty,
            )
            return (order_id,) if order_id is not None else ()

        full_qty = _target_quantity(
            plan,
            equity=equity,
            target_fraction=1.0,
        )
        tail_fraction = _tail_fraction(snapshot.policy)
        tail_target_qty = min(
            desired_qty,
            _target_quantity(
                plan,
                equity=equity,
                target_fraction=tail_fraction,
            ),
        )
        main_target_qty = desired_qty - tail_target_qty
        prior_fraction = 0.0 if decision.action is PolicyAction.ENTER_PROBE else 0.25
        prior_total_qty = _target_quantity(
            plan,
            equity=equity,
            target_fraction=prior_fraction,
        )
        prior_tail_qty = min(
            prior_total_qty,
            int(
                (Decimal(full_qty) * Decimal(str(tail_fraction))).to_integral_value(
                    rounding=ROUND_FLOOR
                )
            ),
        )
        prior_main_qty = prior_total_qty - prior_tail_qty
        component_quantities = (
            ("tail", tail_target_qty - prior_tail_qty),
            ("main", main_target_qty - prior_main_qty),
        )
        risk_per_share = snapshot.ask - plan.hard_stop
        first_target_r = Decimal(str(snapshot.policy.first_target_reward_r or 2.5))
        take_profit = (
            plan.take_profit_1
            if plan.take_profit_1 is not None
            else snapshot.ask + (risk_per_share * first_target_r)
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        submitted: list[str] = []
        for component, quantity in component_quantities:
            if quantity <= 0:
                continue
            client_id = _entry_client_order_id(
                plan,
                decision,
                phase="regular",
                component=component,
            )
            record = self.ledger.ensure_command(
                client_id,
                trade_date=plan.trade_date,
                kind=f"regular_{decision.action.value}_{component}",
                symbol=plan.symbol,
                at_utc=now_utc,
            )
            if record.completed:
                if record.broker_order_id is not None:
                    submitted.append(record.broker_order_id)
                continue
            order = self.broker.submit_order_idempotent(
                PaperOrderRequest(
                    client_order_id=client_id,
                    symbol=plan.symbol,
                    qty=quantity,
                    order_type="market",
                    take_profit_price=(f"{take_profit:.2f}" if component == "main" else None),
                    stop_loss_price=f"{plan.hard_stop:.2f}",
                )
            )
            self.ledger.complete_command(
                client_id,
                at_utc=now_utc,
                broker_order_id=order.id,
            )
            submitted.append(order.id)
        if sum(quantity for _, quantity in component_quantities) != delta:
            raise RuntimeError("regular entry component allocation is inconsistent")
        return tuple(submitted)

    def _run_extended_hours_stop(
        self,
        *,
        plan: AutonomousPaperPlan,
        snapshot: PaperSessionSnapshot,
        account: PaperAccount,
    ) -> SyntheticStopExecutionResult | None:
        now_utc = snapshot.policy.observed_at_utc
        local_time = now_utc.astimezone(ZoneInfo("America/New_York")).time()
        if time(9, 30) <= local_time < time(16):
            return None
        position_qty = _position_quantity(plan.symbol, self.broker.list_positions())
        if position_qty <= 0:
            return None
        premarket = self.ledger.get_premarket(f"{plan.plan_id}:premarket-probe")
        planned_qty = premarket.plan.target_qty if premarket is not None else position_qty
        result = self.synthetic_stop_controller.tick(
            SyntheticStopPlan(
                plan_id=f"{plan.plan_id}:extended-stop",
                symbol=plan.symbol,
                qty=planned_qty,
                stop_price=plan.hard_stop,
            ),
            SyntheticStopSnapshot(
                observed_at_utc=now_utc,
                quote_asof_utc=snapshot.quote_asof_utc,
                bid=snapshot.bid,
                ask=snapshot.ask,
                last_trade=snapshot.last_trade,
                last_trade_asof_utc=snapshot.last_trade_asof_utc,
                quote_provenance=snapshot.quote_provenance,
                trade_provenance=snapshot.trade_provenance,
                data_healthy=snapshot.policy.data_healthy,
                broker_healthy=(
                    account.status.strip().upper() == "ACTIVE"
                    and not account.account_blocked
                    and not account.trading_blocked
                ),
                verified_material_negative=snapshot.policy.material_negative,
                halt_risk=snapshot.halt_risk,
                filled=False,
            ),
        )
        if result.action in {
            StopAction.SUBMIT_EXIT_LIMIT,
            StopAction.CANCEL_REPLACE_EXIT,
        }:
            return result
        return None

    def _run_premarket_entry(
        self,
        *,
        plan: AutonomousPaperPlan,
        snapshot: PaperSessionSnapshot,
        target_qty: int,
        filled_qty: int,
    ) -> str | None:
        now_utc = snapshot.policy.observed_at_utc
        entry_plan_id = f"{plan.plan_id}:premarket-probe"
        record = self.ledger.get_premarket(entry_plan_id)
        if record is None:
            entry_plan = PremarketEntryPlan(
                plan_id=entry_plan_id,
                symbol=plan.symbol,
                target_qty=target_qty,
                reference_price=plan.reference_price,
            )
            record = self.ledger.ensure_premarket(entry_plan, at_utc=now_utc)
        else:
            entry_plan = record.plan
        active_order = (
            self.broker.get_order_by_client_id(record.active_client_order_id)
            if record.active_client_order_id is not None
            else None
        )
        order_working = active_order is not None and active_order.status.strip().lower() in {
            "new",
            "accepted",
            "pending_new",
            "partially_filled",
        }
        decision = self.premarket_entry_engine.evaluate(
            record.plan,
            record.runtime,
            PremarketEntrySnapshot(
                observed_at_utc=now_utc,
                quote_asof_utc=snapshot.quote_asof_utc,
                bid=snapshot.bid,
                ask=snapshot.ask,
                filled_qty=min(filled_qty, record.plan.target_qty),
                order_working=order_working,
                quote_provenance=snapshot.quote_provenance,
                data_healthy=snapshot.policy.data_healthy,
                broker_healthy=True,
            ),
        )
        if (
            decision.action
            in {
                PremarketEntryAction.CANCEL_REPLACE,
                PremarketEntryAction.CANCEL_REMAINDER,
                PremarketEntryAction.ABANDON,
            }
            and active_order is not None
        ):
            self._cancel_order_once(
                trade_date=plan.trade_date,
                order=active_order,
                now_utc=now_utc,
            )
        if decision.action in {
            PremarketEntryAction.SUBMIT_LIMIT,
            PremarketEntryAction.CANCEL_REPLACE,
        }:
            if decision.limit_price is None:
                raise RuntimeError("premarket entry submit lacks limit price")
            client_id = _premarket_client_order_id(
                entry_plan,
                attempt=decision.runtime.attempt,
            )
            command = self.ledger.ensure_command(
                client_id,
                trade_date=plan.trade_date,
                kind="premarket_entry",
                symbol=plan.symbol,
                at_utc=now_utc,
            )
            if command.completed:
                broker_order_id = command.broker_order_id
            else:
                order = self.broker.submit_extended_limit_idempotent(
                    PaperExtendedLimitRequest(
                        client_order_id=client_id,
                        symbol=plan.symbol,
                        qty=decision.remaining_qty,
                        side="buy",
                        limit_price=f"{decision.limit_price:.2f}",
                    )
                )
                broker_order_id = order.id
                self.ledger.complete_command(
                    client_id,
                    at_utc=now_utc,
                    broker_order_id=broker_order_id,
                )
            self.ledger.save_premarket(
                entry_plan.plan_id,
                runtime=decision.runtime,
                active_client_order_id=client_id,
                active_broker_order_id=broker_order_id,
                at_utc=now_utc,
            )
            return broker_order_id
        active_client = record.active_client_order_id
        active_broker = record.active_broker_order_id
        if decision.action in {
            PremarketEntryAction.CANCEL_REMAINDER,
            PremarketEntryAction.ABANDON,
            PremarketEntryAction.COMPLETE,
        }:
            active_client = None
            active_broker = None
        self.ledger.save_premarket(
            entry_plan.plan_id,
            runtime=decision.runtime,
            active_client_order_id=active_client,
            active_broker_order_id=active_broker,
            at_utc=now_utc,
        )
        return active_broker

    def _cancel_order_once(
        self,
        *,
        trade_date: date,
        order: BrokerOrder,
        now_utc: datetime,
    ) -> bool:
        command_id = f"cancel:{trade_date.isoformat()}:{order.id}"
        record = self.ledger.ensure_command(
            command_id,
            trade_date=trade_date,
            kind="cancel",
            symbol=order.symbol,
            at_utc=now_utc,
        )
        if record.completed:
            return False
        if self.broker.cancel_order(order.id):
            self.ledger.complete_command(command_id, at_utc=now_utc)
            return True
        return False


def _daily_return(account: PaperAccount) -> Decimal:
    equity = _positive_decimal(account.equity, name="equity")
    last_equity = _positive_decimal(account.last_equity, name="last_equity")
    return (equity / last_equity) - Decimal(1)


def _positive_decimal(value: str, *, name: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"Paper account {name} is invalid") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"Paper account {name} must be finite and positive")
    return parsed


def _reject_audit_secrets(value: object, *, key: str = "") -> None:
    """Keep process evidence complete without persisting credentials."""

    normalized_key = key.lower().replace("-", "_")
    sensitive_fragments = (
        "secret",
        "password",
        "token",
        "api_key",
        "apikey",
        "authorization",
        "credential",
    )
    if any(fragment in normalized_key for fragment in sensitive_fragments):
        raise ValueError("Paper audit payload must not contain secrets")
    if isinstance(value, Mapping):
        for child_key, child_value in value.items():
            if not isinstance(child_key, str):
                raise ValueError("Paper audit payload keys must be strings")
            _reject_audit_secrets(child_value, key=child_key)
    elif isinstance(value, (list, tuple)):
        for child_value in value:
            _reject_audit_secrets(child_value, key=key)


def _audit_json_safe(value: object) -> object:
    """Normalize exact execution evidence into a deterministic JSON value."""

    if value is None or isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Paper audit float must be finite")
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("Paper audit decimal must be finite")
        return str(value)
    if isinstance(value, datetime):
        _require_utc(value)
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return _audit_json_safe(value.value)
    if isinstance(value, Mapping):
        normalized: dict[str, object] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError("Paper audit payload keys must be strings")
            normalized[key] = _audit_json_safe(child)
        return normalized
    if isinstance(value, (list, tuple)):
        return [_audit_json_safe(child) for child in value]
    raise ValueError("Paper audit payload contains an unsupported value")


def _whole_long_quantity(position: PaperPosition) -> int:
    if position.side.strip().lower() != "long":
        raise RuntimeError("autonomous Paper execution is permanently long-only")
    try:
        qty = Decimal(position.qty)
    except InvalidOperation as exc:
        raise RuntimeError("Paper position quantity is invalid") from exc
    if qty <= 0 or qty != qty.to_integral_value():
        raise RuntimeError("Paper position quantity must be a positive whole number")
    return int(qty)


def _flatten_client_order_id(trade_date: date, symbol: str) -> str:
    digest = hashlib.sha256(f"{trade_date}:{symbol}:hard-loss".encode()).hexdigest()[:12]
    return f"tsv2-{trade_date:%Y%m%d}-{symbol}-hard-flat-{digest}"


def _decision_close_client_order_id(
    plan: AutonomousPaperPlan,
    decision: PolicyDecision,
) -> str:
    material = ":".join((plan.plan_id, *decision.reasons, "exit"))
    digest = hashlib.sha256(material.encode()).hexdigest()[:12]
    return f"tsv2-{plan.trade_date:%Y%m%d}-{plan.symbol}-exit-{digest}"


def _fail_closed_client_order_id(
    plan: AutonomousPaperPlan,
    *,
    reason: str,
) -> str:
    material = f"{plan.plan_id}:{reason}:runtime-fail-closed"
    digest = hashlib.sha256(material.encode()).hexdigest()[:12]
    return f"tsv2-{plan.trade_date:%Y%m%d}-{plan.symbol}-fault-{digest}"


def _fresh_exit_quote(
    *,
    observed_at_utc: datetime,
    bid: Decimal | None,
    quote_asof_utc: datetime | None,
    quote_provenance: str | None,
) -> bool:
    if (
        bid is None
        or not bid.is_finite()
        or bid <= 0
        or quote_asof_utc is None
        or not (quote_provenance or "").strip()
    ):
        return False
    _require_utc(quote_asof_utc)
    age = (observed_at_utc - quote_asof_utc).total_seconds()
    return 0 <= age <= 30


def _marketable_exit_limit(bid: Decimal) -> Decimal:
    return (bid * (Decimal(1) - Decimal("0.0025"))).quantize(
        Decimal("0.01"),
        rounding=ROUND_FLOOR,
    )


def _reduction_client_order_id(
    plan: AutonomousPaperPlan,
    decision: PolicyDecision,
    *,
    target_qty: int,
) -> str:
    material = ":".join(
        (
            plan.plan_id,
            decision.action.value,
            str(target_qty),
            *decision.reasons,
        )
    )
    digest = hashlib.sha256(material.encode()).hexdigest()[:12]
    return f"tsv2-{plan.trade_date:%Y%m%d}-{plan.symbol}-reduce-{digest}"


def _entry_client_order_id(
    plan: AutonomousPaperPlan,
    decision: PolicyDecision,
    *,
    phase: str,
    component: str,
) -> str:
    material = ":".join(
        (
            plan.plan_id,
            phase,
            component,
            decision.action.value,
            f"{decision.target_position_fraction:.4f}",
        )
    )
    digest = hashlib.sha256(material.encode()).hexdigest()[:12]
    return (
        f"tsv2-{plan.trade_date:%Y%m%d}-{plan.symbol}-"
        f"{phase}-{component}-{decision.action.value}-{digest}"
    )


def _premarket_client_order_id(
    plan: PremarketEntryPlan,
    *,
    attempt: int,
) -> str:
    digest = hashlib.sha256(plan.plan_id.encode()).hexdigest()[:12]
    return f"tsv2-{plan.symbol}-premarket-{digest}-{attempt}"


def _premarket_protection_client_order_id(
    plan: AutonomousPaperPlan,
    *,
    qty: int,
) -> str:
    material = f"{plan.plan_id}:premarket-regular-stop:{qty}:{plan.hard_stop}"
    digest = hashlib.sha256(material.encode()).hexdigest()[:12]
    return f"tsv2-{plan.trade_date:%Y%m%d}-{plan.symbol}-premarket-protect-{qty}-{digest}"


def _premarket_plan_json(plan: PremarketEntryPlan) -> str:
    return json.dumps(
        {
            "plan_id": plan.plan_id,
            "symbol": plan.symbol,
            "target_qty": plan.target_qty,
            "reference_price": str(plan.reference_price),
            "reprice_seconds": plan.reprice_seconds,
            "total_ttl_seconds": plan.total_ttl_seconds,
            "max_reprices": plan.max_reprices,
            "max_chase_fraction": str(plan.max_chase_fraction),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _premarket_runtime_json(runtime: PremarketEntryRuntime) -> str:
    return json.dumps(
        {
            "started_at_utc": _iso(runtime.started_at_utc),
            "last_command_at_utc": _iso(runtime.last_command_at_utc),
            "attempt": runtime.attempt,
            "completed_at_utc": _iso(runtime.completed_at_utc),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _premarket_record(row: sqlite3.Row | None) -> _PremarketRecord:
    if row is None:
        raise RuntimeError("premarket entry record disappeared")
    plan_payload = json.loads(str(row["plan_json"]))
    runtime_payload = json.loads(str(row["runtime_json"]))
    return _PremarketRecord(
        plan=PremarketEntryPlan(
            plan_id=str(plan_payload["plan_id"]),
            symbol=str(plan_payload["symbol"]),
            target_qty=int(plan_payload["target_qty"]),
            reference_price=Decimal(str(plan_payload["reference_price"])),
            reprice_seconds=float(plan_payload["reprice_seconds"]),
            total_ttl_seconds=float(plan_payload["total_ttl_seconds"]),
            max_reprices=int(plan_payload["max_reprices"]),
            max_chase_fraction=Decimal(str(plan_payload["max_chase_fraction"])),
        ),
        runtime=PremarketEntryRuntime(
            started_at_utc=_optional_datetime(runtime_payload["started_at_utc"]),
            last_command_at_utc=_optional_datetime(runtime_payload["last_command_at_utc"]),
            attempt=int(runtime_payload["attempt"]),
            completed_at_utc=_optional_datetime(runtime_payload["completed_at_utc"]),
        ),
        active_client_order_id=(
            str(row["active_client_order_id"])
            if row["active_client_order_id"] is not None
            else None
        ),
        active_broker_order_id=(
            str(row["active_broker_order_id"])
            if row["active_broker_order_id"] is not None
            else None
        ),
    )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _optional_datetime(value: object) -> datetime | None:
    return datetime.fromisoformat(str(value)) if value is not None else None


def _target_quantity(
    plan: AutonomousPaperPlan,
    *,
    equity: Decimal,
    target_fraction: float,
) -> int:
    entry_price = plan.reference_price
    risk_per_share = entry_price - plan.hard_stop
    if risk_per_share <= 0:
        return 0
    risk_budget = equity * plan.full_risk_fraction
    notional_budget = equity * plan.max_notional_fraction
    by_risk = int((risk_budget / risk_per_share).to_integral_value(rounding=ROUND_FLOOR))
    by_notional = int((notional_budget / entry_price).to_integral_value(rounding=ROUND_FLOOR))
    full_quantity = min(by_risk, by_notional)
    return int(
        (Decimal(full_quantity) * Decimal(str(target_fraction))).to_integral_value(
            rounding=ROUND_FLOOR
        )
    )


def _tail_fraction(snapshot: PolicySnapshot) -> float:
    return _tail_mode_and_fraction(snapshot)[1]


def _tail_mode_and_fraction(
    snapshot: PolicySnapshot,
) -> tuple[TailMode, float]:
    score = snapshot.right_tail.value
    if (
        score is not None
        and score >= 85.0
        and snapshot.a_plus_plus_approved
        and snapshot.catalyst.value is not None
        and snapshot.catalyst.value >= 95.0
        and snapshot.order_flow.value is not None
        and snapshot.order_flow.value >= 85.0
        and snapshot.execution.value is not None
        and snapshot.execution.value >= 85.0
    ):
        return TailMode.A_PLUS_PLUS, 0.30
    if score is not None and score >= 70.0:
        return TailMode.HIGH_RIGHT_TAIL, 0.25
    return TailMode.STANDARD, 0.20


def _position_quantity(
    symbol: str,
    positions: tuple[PaperPosition, ...],
) -> int:
    matching = [position for position in positions if position.symbol == symbol]
    if not matching:
        return 0
    if len(matching) > 1:
        raise RuntimeError("Paper broker returned duplicate symbol positions")
    return _whole_long_quantity(matching[0])


def _entry_order_filled(order: BrokerOrder) -> bool:
    try:
        filled_qty = Decimal(order.filled_qty)
    except InvalidOperation:
        return False
    return (
        order.status.strip().lower() == "filled"
        and filled_qty.is_finite()
        and filled_qty >= Decimal(order.qty)
    )


def _require_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("autonomous Paper timestamp must be UTC")
