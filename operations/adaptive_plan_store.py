"""Restart-safe store and event ledger for adaptive trade plans."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from db.migrations.sqlite import SQLiteMigration, apply_sqlite_migrations
from kernel.adaptive_trade_plan import (
    AdaptiveTradePlanEngine,
    BaselineTradePlan,
    PlanAction,
    PlanDecision,
    PlanMode,
    PlanRuntime,
    PlanState,
    PositionFacts,
    RealtimePlanFacts,
)


def _canonical(value: dict[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _plan_payload(plan: BaselineTradePlan) -> dict[str, object]:
    return {
        "plan_id": plan.plan_id,
        "symbol": plan.symbol,
        "trade_date": plan.trade_date.isoformat(),
        "mode": plan.mode.value,
        "entry_window_end_utc": plan.entry_window_end_utc.isoformat(),
        "force_exit_utc": plan.force_exit_utc.isoformat(),
        "hard_stop": plan.hard_stop,
        "max_risk_dollars": plan.max_risk_dollars,
        "max_notional": plan.max_notional,
        "probe_fraction": plan.probe_fraction,
        "max_spread_ratio": plan.max_spread_ratio,
        "soft_cooldown_seconds": plan.soft_cooldown.total_seconds(),
        "max_soft_revisions": plan.max_soft_revisions,
    }


def _plan_from_payload(payload: dict[str, Any]) -> BaselineTradePlan:
    return BaselineTradePlan(
        plan_id=str(payload["plan_id"]),
        symbol=str(payload["symbol"]),
        trade_date=date.fromisoformat(str(payload["trade_date"])),
        mode=PlanMode(str(payload["mode"])),
        entry_window_end_utc=datetime.fromisoformat(
            str(payload["entry_window_end_utc"])
        ),
        force_exit_utc=datetime.fromisoformat(str(payload["force_exit_utc"])),
        hard_stop=float(payload["hard_stop"]),
        max_risk_dollars=float(payload["max_risk_dollars"]),
        max_notional=float(payload["max_notional"]),
        probe_fraction=float(payload["probe_fraction"]),
        max_spread_ratio=float(payload["max_spread_ratio"]),
        soft_cooldown=timedelta(seconds=float(payload["soft_cooldown_seconds"])),
        max_soft_revisions=int(payload["max_soft_revisions"]),
    )


def _runtime_payload(runtime: PlanRuntime) -> dict[str, object]:
    return {
        "plan_id": runtime.plan_id,
        "state": runtime.state.value,
        "consecutive_confirmations": runtime.consecutive_confirmations,
        "last_completed_one_minute_bar_utc": (
            None
            if runtime.last_completed_one_minute_bar_utc is None
            else runtime.last_completed_one_minute_bar_utc.isoformat()
        ),
        "last_material_revision_utc": (
            None
            if runtime.last_material_revision_utc is None
            else runtime.last_material_revision_utc.isoformat()
        ),
        "soft_revision_count": runtime.soft_revision_count,
        "protective_stop": runtime.protective_stop,
        "revision": runtime.revision,
        "last_add_signal_position_shares": runtime.last_add_signal_position_shares,
    }


def _optional_datetime(value: object) -> datetime | None:
    return None if value is None else datetime.fromisoformat(str(value))


def _runtime_from_payload(payload: dict[str, Any]) -> PlanRuntime:
    return PlanRuntime(
        plan_id=str(payload["plan_id"]),
        state=PlanState(str(payload["state"])),
        consecutive_confirmations=int(payload["consecutive_confirmations"]),
        last_completed_one_minute_bar_utc=_optional_datetime(
            payload.get("last_completed_one_minute_bar_utc")
        ),
        last_material_revision_utc=_optional_datetime(
            payload.get("last_material_revision_utc")
        ),
        soft_revision_count=int(payload["soft_revision_count"]),
        protective_stop=float(payload["protective_stop"]),
        revision=int(payload["revision"]),
        last_add_signal_position_shares=(
            None
            if payload.get("last_add_signal_position_shares") is None
            else int(payload["last_add_signal_position_shares"])
        ),
    )


def _facts_payload(facts: RealtimePlanFacts) -> dict[str, object]:
    return {
        "observed_at_utc": facts.observed_at_utc.isoformat(),
        "quote_ts_utc": facts.quote_ts_utc.isoformat(),
        "bid": facts.bid,
        "ask": facts.ask,
        "last_price": facts.last_price,
        "session_vwap": facts.session_vwap,
        "completed_one_minute_bar_utc": (
            None
            if facts.completed_one_minute_bar_utc is None
            else facts.completed_one_minute_bar_utc.isoformat()
        ),
        "one_minute_trigger": facts.one_minute_trigger,
        "five_minute_confirmed": facts.five_minute_confirmed,
        "fifteen_minute_confirmed": facts.fifteen_minute_confirmed,
        "green_volume_ratio": facts.green_volume_ratio,
        "relative_strength": facts.relative_strength,
        "benchmark_above_vwap": facts.benchmark_above_vwap,
        "sector_above_vwap": facts.sector_above_vwap,
        "market_risk_off": facts.market_risk_off,
        "order_flow_imbalance": facts.order_flow_imbalance,
        "order_flow_confirmation_score": facts.order_flow_confirmation_score,
        "order_flow_provenance": facts.order_flow_provenance,
        "quote_provenance": facts.quote_provenance,
        "catalyst_score": facts.catalyst_score,
        "data_complete": facts.data_complete,
        "proposed_structural_stop": facts.proposed_structural_stop,
        "first_target_filled": facts.first_target_filled,
    }


def _decision_payload(
    decision: PlanDecision,
    *,
    facts: RealtimePlanFacts,
) -> dict[str, object]:
    return {
        "plan_id": decision.plan_id,
        "symbol": decision.symbol,
        "observed_at_utc": decision.observed_at_utc.isoformat(),
        "action": decision.action.value,
        "prior_state": decision.prior_state.value,
        "next_state": decision.next_state.value,
        "material_revision": decision.material_revision,
        "reasons": list(decision.reasons),
        "blockers": list(decision.blockers),
        "runtime": _runtime_payload(decision.runtime),
        "suggested_shares": decision.suggested_shares,
        "facts": _facts_payload(facts),
        "order_authorized": False,
    }


@dataclass(frozen=True)
class StoredEvaluation:
    decision: PlanDecision
    sequence: int | None


def _create_adaptive_plan_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS adaptive_plans (
            plan_id TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            baseline_json TEXT NOT NULL,
            registered_at_utc TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS adaptive_runtime (
            plan_id TEXT PRIMARY KEY REFERENCES adaptive_plans(plan_id),
            runtime_json TEXT NOT NULL,
            latest_evaluation_json TEXT,
            latest_event_json TEXT,
            last_event_signature TEXT,
            updated_at_utc TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS adaptive_events (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_id TEXT NOT NULL REFERENCES adaptive_plans(plan_id),
            observed_at_utc TEXT NOT NULL,
            event_json TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_adaptive_events_plan_sequence
        ON adaptive_events(plan_id, sequence)
        """
    )


ADAPTIVE_PLAN_MIGRATIONS = (
    SQLiteMigration(
        version=1,
        name="adaptive_plan_schema",
        signature="adaptive_plan_schema.v1",
        apply=_create_adaptive_plan_schema,
    ),
)


class AdaptivePlanStore:
    """Own plan registration, atomic evaluation, recovery and client reads."""

    def __init__(
        self,
        path: str | Path,
        *,
        engine: AdaptiveTradePlanEngine | None = None,
    ):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = engine or AdaptiveTradePlanEngine()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            apply_sqlite_migrations(
                connection,
                owner="operations.adaptive_plan_store",
                migrations=ADAPTIVE_PLAN_MIGRATIONS,
            )

    def register(self, plan: BaselineTradePlan) -> None:
        payload = _plan_payload(plan)
        serialized = _canonical(payload)
        runtime = PlanRuntime.initial(plan)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT baseline_json FROM adaptive_plans WHERE plan_id=?",
                (plan.plan_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["baseline_json"]) != serialized:
                    connection.rollback()
                    raise ValueError(
                        "baseline risk envelope is immutable for an existing plan id"
                    )
                connection.commit()
                return
            registered = datetime.now(UTC).isoformat()
            connection.execute(
                """
                INSERT INTO adaptive_plans (
                    plan_id, symbol, trade_date, baseline_json, registered_at_utc
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    plan.plan_id,
                    plan.symbol,
                    plan.trade_date.isoformat(),
                    serialized,
                    registered,
                ),
            )
            connection.execute(
                """
                INSERT INTO adaptive_runtime (
                    plan_id, runtime_json, updated_at_utc
                ) VALUES (?, ?, ?)
                """,
                (plan.plan_id, _canonical(_runtime_payload(runtime)), registered),
            )
            connection.commit()

    def evaluate(
        self,
        plan_id: str,
        facts: RealtimePlanFacts,
        *,
        position: PositionFacts | None,
    ) -> StoredEvaluation:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT p.baseline_json, r.runtime_json, r.last_event_signature
                FROM adaptive_plans AS p
                JOIN adaptive_runtime AS r USING(plan_id)
                WHERE p.plan_id=?
                """,
                (plan_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise KeyError(f"unknown adaptive plan: {plan_id}")
            plan = _plan_from_payload(
                cast(dict[str, Any], json.loads(str(row["baseline_json"])))
            )
            runtime = _runtime_from_payload(
                cast(dict[str, Any], json.loads(str(row["runtime_json"])))
            )
            decision = self.engine.evaluate(plan, runtime, facts, position=position)
            decision_payload = _decision_payload(decision, facts=facts)
            signature = _canonical(
                {
                    "action": decision.action.value,
                    "next_state": decision.next_state.value,
                    "reasons": list(decision.reasons),
                    "blockers": list(decision.blockers),
                }
            )
            interesting = (
                decision.material_revision
                or decision.action is not PlanAction.NO_ACTION
                or bool(decision.blockers)
            )
            should_emit = interesting and signature != row["last_event_signature"]
            sequence: int | None = None
            latest_event_json: str | None = None
            if should_emit:
                event_json = _canonical(decision_payload)
                cursor = connection.execute(
                    """
                    INSERT INTO adaptive_events (
                        plan_id, observed_at_utc, event_json
                    ) VALUES (?, ?, ?)
                    """,
                    (
                        plan_id,
                        decision.observed_at_utc.isoformat(),
                        event_json,
                    ),
                )
                if cursor.lastrowid is None:
                    raise RuntimeError("adaptive plan event sequence was not allocated")
                sequence = cursor.lastrowid
                latest_event_json = event_json
            connection.execute(
                """
                UPDATE adaptive_runtime
                SET runtime_json=?,
                    latest_evaluation_json=?,
                    latest_event_json=COALESCE(?, latest_event_json),
                    last_event_signature=?,
                    updated_at_utc=?
                WHERE plan_id=?
                """,
                (
                    _canonical(_runtime_payload(decision.runtime)),
                    _canonical(decision_payload),
                    latest_event_json,
                    signature,
                    decision.observed_at_utc.isoformat(),
                    plan_id,
                ),
            )
            connection.commit()
        return StoredEvaluation(decision=decision, sequence=sequence)

    def runtime(self, plan_id: str) -> PlanRuntime:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT runtime_json FROM adaptive_runtime WHERE plan_id=?",
                (plan_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown adaptive plan: {plan_id}")
        return _runtime_from_payload(
            cast(dict[str, Any], json.loads(str(row["runtime_json"])))
        )

    def plan(self, plan_id: str) -> BaselineTradePlan:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT baseline_json FROM adaptive_plans WHERE plan_id=?",
                (plan_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown adaptive plan: {plan_id}")
        return _plan_from_payload(
            cast(dict[str, Any], json.loads(str(row["baseline_json"])))
        )

    def events_after(
        self,
        sequence: int,
        *,
        limit: int = 100,
    ) -> tuple[dict[str, object], ...]:
        if sequence < 0 or not 1 <= limit <= 1000:
            raise ValueError("invalid event cursor or limit")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT sequence, plan_id, observed_at_utc, event_json
                FROM adaptive_events
                WHERE sequence > ?
                ORDER BY sequence
                LIMIT ?
                """,
                (sequence, limit),
            ).fetchall()
        output: list[dict[str, object]] = []
        for row in rows:
            event = cast(dict[str, object], json.loads(str(row["event_json"])))
            output.append(
                {
                    "sequence": int(row["sequence"]),
                    "plan_id": str(row["plan_id"]),
                    "observed_at_utc": str(row["observed_at_utc"]),
                    "event": event,
                }
            )
        return tuple(output)

    def dashboard(self) -> dict[str, object]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT p.baseline_json, r.runtime_json, r.latest_event_json,
                       r.latest_evaluation_json, r.updated_at_utc
                FROM adaptive_plans AS p
                JOIN adaptive_runtime AS r USING(plan_id)
                WHERE p.trade_date = (
                    SELECT MAX(trade_date) FROM adaptive_plans
                )
                ORDER BY p.trade_date DESC, p.symbol
                """
            ).fetchall()
            sequence_row = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) AS latest FROM adaptive_events"
            ).fetchone()
        plans: list[dict[str, object]] = []
        for row in rows:
            baseline = cast(
                dict[str, object],
                json.loads(str(row["baseline_json"])),
            )
            plans.append(
                {
                    "plan_id": str(baseline["plan_id"]),
                    "symbol": str(baseline["symbol"]),
                    "baseline": baseline,
                    "runtime": cast(
                        dict[str, object],
                        json.loads(str(row["runtime_json"])),
                    ),
                    "latest_decision": (
                        None
                        if row["latest_event_json"] is None
                        else cast(
                            dict[str, object],
                            json.loads(str(row["latest_event_json"])),
                        )
                    ),
                    "latest_evaluation": (
                        None
                        if row["latest_evaluation_json"] is None
                        else cast(
                            dict[str, object],
                            json.loads(str(row["latest_evaluation_json"])),
                        )
                    ),
                    "updated_at_utc": str(row["updated_at_utc"]),
                }
            )
        return {
            "schema_version": "adaptive_trade_dashboard.v1",
            "plans": plans,
            "latest_sequence": int(sequence_row["latest"]) if sequence_row else 0,
            "orders_authorized": False,
        }
