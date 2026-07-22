"""Durable agent fact store with auditable, parameterized access only."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Protocol
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from agent_gateway.contracts import (
    AgentRole,
    AuditReport,
    EvolutionProposal,
    Lesson,
    QueryEntity,
    StoreQuery,
    Thesis,
    now_utc,
)

ENTITY_TABLES: dict[QueryEntity, str] = {
    QueryEntity.THESES: "agent_theses",
    QueryEntity.AUDIT_REPORTS: "audit_reports",
    QueryEntity.LESSONS: "lessons",
    QueryEntity.PROPOSALS: "evolution_proposals",
    QueryEntity.TRADEPLAN_DRAFTS: "agent_tradeplan_drafts",
    QueryEntity.TOOL_AUDIT: "tool_audit",
}


def _json(value: object) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _content_id(prefix: str, document: object) -> tuple[str, str]:
    encoded = _json(document).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    return f"{prefix}-{digest[:24]}", digest


class AgentFactStore(Protocol):
    def initialize(self) -> None: ...

    def record_audit(
        self,
        *,
        actor: AgentRole | None,
        tool: str,
        request: object,
        response: object | None,
        success: bool,
        error_code: str | None,
    ) -> str: ...

    def put_thesis(self, thesis: Thesis) -> str: ...

    def put_audit_report(self, report: AuditReport) -> str: ...

    def put_lesson(self, lesson: Lesson) -> str: ...

    def put_proposal(self, proposal: EvolutionProposal) -> str: ...

    def put_tradeplan_draft(self, *, actor: AgentRole, document: Mapping[str, object]) -> str: ...

    def query(self, query: StoreQuery) -> list[dict[str, object]]: ...


class SQLiteAgentFactStore:
    """Local durable store used in development and as a fail-closed fallback."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def initialize(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS tool_audit (
            audit_id TEXT PRIMARY KEY,
            actor TEXT,
            tool TEXT NOT NULL,
            request_json TEXT NOT NULL,
            response_json TEXT,
            success INTEGER NOT NULL CHECK(success IN (0, 1)),
            error_code TEXT,
            created_at_utc TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS agent_theses (
            record_id TEXT PRIMARY KEY,
            actor TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            category TEXT,
            status TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            document_json TEXT NOT NULL,
            created_at_utc TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS lessons (
            record_id TEXT PRIMARY KEY,
            actor TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            category TEXT NOT NULL CHECK(category IN (
                'selection_review','signal_decay','execution_gap','cost_drift'
            )),
            status TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            document_json TEXT NOT NULL,
            created_at_utc TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS audit_reports (
            record_id TEXT PRIMARY KEY,
            actor TEXT NOT NULL CHECK(actor = 'discipline'),
            trade_date TEXT NOT NULL,
            category TEXT,
            status TEXT NOT NULL CHECK(status IN ('complete', 'incomplete_evidence')),
            content_sha256 TEXT NOT NULL,
            document_json TEXT NOT NULL,
            created_at_utc TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS evolution_proposals (
            record_id TEXT PRIMARY KEY,
            actor TEXT NOT NULL CHECK(actor = 'pdca'),
            trade_date TEXT NOT NULL,
            category TEXT,
            status TEXT NOT NULL CHECK(status = 'draft'),
            content_sha256 TEXT NOT NULL,
            document_json TEXT NOT NULL CHECK(
                json_extract(document_json, '$.status') = 'draft'
                AND json_extract(document_json, '$.production_eligible') = 0
            ),
            created_at_utc TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS agent_tradeplan_drafts (
            record_id TEXT PRIMARY KEY,
            actor TEXT NOT NULL CHECK(actor = 'commander'),
            trade_date TEXT NOT NULL,
            category TEXT,
            status TEXT NOT NULL CHECK(status = 'shadow_draft'),
            content_sha256 TEXT NOT NULL,
            document_json TEXT NOT NULL CHECK(
                json_extract(document_json, '$.status') = 'shadow_draft'
                AND json_extract(document_json, '$.execution_eligible') = 0
                AND json_extract(document_json, '$.broker_submission_count') = 0
            ),
            created_at_utc TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_theses_date_actor ON agent_theses(trade_date, actor);
        CREATE INDEX IF NOT EXISTS idx_lessons_date_category ON lessons(trade_date, category);
        CREATE INDEX IF NOT EXISTS idx_audit_reports_date ON audit_reports(trade_date);
        CREATE INDEX IF NOT EXISTS idx_proposals_date ON evolution_proposals(trade_date);
        CREATE INDEX IF NOT EXISTS idx_audit_created ON tool_audit(created_at_utc);
        """
        with self._connect() as connection:
            connection.executescript(schema)

    def record_audit(
        self,
        *,
        actor: AgentRole | None,
        tool: str,
        request: object,
        response: object | None,
        success: bool,
        error_code: str | None,
    ) -> str:
        audit_id = f"audit-{uuid4().hex}"
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO tool_audit VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    audit_id,
                    actor.value if actor else None,
                    tool,
                    _json(request),
                    _json(response) if response is not None else None,
                    int(success),
                    error_code,
                    now_utc().isoformat(),
                ),
            )
        return audit_id

    def _put_document(
        self,
        *,
        table: str,
        prefix: str,
        actor: AgentRole,
        trade_date: date,
        category: str | None,
        status: str,
        document: object,
    ) -> str:
        if table not in set(ENTITY_TABLES.values()) - {"tool_audit"}:
            raise ValueError("document table is not allowlisted")
        record_id, digest = _content_id(prefix, document)
        with self._connect() as connection:
            connection.execute(
                f"INSERT OR IGNORE INTO {table} "
                "(record_id, actor, trade_date, category, status, content_sha256, "
                "document_json, created_at_utc) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record_id,
                    actor.value,
                    trade_date.isoformat(),
                    category,
                    status,
                    digest,
                    _json(document),
                    now_utc().isoformat(),
                ),
            )
        return record_id

    def put_thesis(self, thesis: Thesis) -> str:
        return self._put_document(
            table="agent_theses",
            prefix="thesis",
            actor=thesis.agent,
            trade_date=thesis.trade_date,
            category=thesis.stage.value,
            status="shadow",
            document=thesis,
        )

    def put_lesson(self, lesson: Lesson) -> str:
        return self._put_document(
            table="lessons",
            prefix="lesson",
            actor=lesson.agent,
            trade_date=lesson.trade_date,
            category=lesson.category.value,
            status="accepted_fact",
            document=lesson,
        )

    def put_audit_report(self, report: AuditReport) -> str:
        return self._put_document(
            table="audit_reports",
            prefix="audit-report",
            actor=report.agent,
            trade_date=report.trade_date,
            category=None,
            status=report.status,
            document=report,
        )

    def put_proposal(self, proposal: EvolutionProposal) -> str:
        return self._put_document(
            table="evolution_proposals",
            prefix="proposal",
            actor=proposal.agent,
            trade_date=proposal.proposal_month,
            category=None,
            status="draft",
            document=proposal,
        )

    def put_tradeplan_draft(self, *, actor: AgentRole, document: Mapping[str, object]) -> str:
        if actor is not AgentRole.COMMANDER:
            raise PermissionError("only commander may store a TradePlan draft")
        if (
            document.get("status") != "shadow_draft"
            or document.get("execution_eligible") is not False
            or document.get("broker_submission_count") != 0
        ):
            raise ValueError("TradePlan draft must remain non-executable with zero submissions")
        raw_date = document.get("trade_date")
        trade_date = date.fromisoformat(str(raw_date))
        return self._put_document(
            table="agent_tradeplan_drafts",
            prefix="tradeplan-draft",
            actor=actor,
            trade_date=trade_date,
            category=None,
            status="shadow_draft",
            document=document,
        )

    def query(self, query: StoreQuery) -> list[dict[str, object]]:
        table = ENTITY_TABLES[query.entity]
        clauses: list[str] = []
        params: list[object] = []
        if query.actor is not None:
            clauses.append("actor = ?")
            params.append(query.actor.value)
        if query.trade_date is not None:
            if table == "tool_audit":
                clauses.append("substr(created_at_utc, 1, 10) = ?")
            else:
                clauses.append("trade_date = ?")
            params.append(query.trade_date.isoformat())
        if query.category is not None:
            if table != "lessons":
                raise ValueError("category filtering is only valid for lessons")
            clauses.append("category = ?")
            params.append(query.category.value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        order = "created_at_utc DESC"
        params.append(query.limit)
        columns = (
            "audit_id, actor, tool, request_json, response_json, success, "
            "error_code, created_at_utc"
            if table == "tool_audit"
            else "record_id, actor, trade_date, category, status, content_sha256, "
            "document_json, created_at_utc"
        )
        sql = f"SELECT {columns} FROM {table}{where} ORDER BY {order} LIMIT ?"
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        output: list[dict[str, object]] = []
        for row in rows:
            item = dict(row)
            for key in ("request_json", "response_json", "document_json"):
                if key in item and item[key] is not None:
                    item[key.removesuffix("_json")] = json.loads(str(item.pop(key)))
            output.append(item)
        return output


class PostgresAgentFactStore:
    """Production store using the same constrained document interface as SQLite."""

    def __init__(self, dsn: str, *, migration_path: str | Path):
        if not dsn.strip():
            raise ValueError("Postgres DSN cannot be empty")
        self.dsn = dsn
        self.migration_path = Path(migration_path)

    def initialize(self) -> None:
        if not self.migration_path.exists():
            raise FileNotFoundError("agent fact-store migration file is unavailable")
        required = {
            "tool_audit",
            "agent_theses",
            "audit_reports",
            "lessons",
            "evolution_proposals",
            "agent_tradeplan_drafts",
        }
        with psycopg.connect(self.dsn) as connection:
            rows = connection.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'quant_agent'
                """
            ).fetchall()
        present = {str(row[0]) for row in rows}
        if not required.issubset(present):
            raise RuntimeError(
                "quant_agent schema is not migrated; apply deploy/postgres/001_agent_facts.sql"
            )

    def record_audit(
        self,
        *,
        actor: AgentRole | None,
        tool: str,
        request: object,
        response: object | None,
        success: bool,
        error_code: str | None,
    ) -> str:
        audit_id = f"audit-{uuid4().hex}"
        with psycopg.connect(self.dsn) as connection:
            connection.execute(
                """
                INSERT INTO quant_agent.tool_audit
                    (audit_id, actor, tool, request_json, response_json, success,
                     error_code, created_at_utc)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    audit_id,
                    actor.value if actor else None,
                    tool,
                    Jsonb(json.loads(_json(request))),
                    Jsonb(json.loads(_json(response))) if response is not None else None,
                    success,
                    error_code,
                    now_utc(),
                ),
            )
        return audit_id

    def _put_document(
        self,
        *,
        table: str,
        prefix: str,
        actor: AgentRole,
        trade_date: date,
        category: str | None,
        status: str,
        document: object,
    ) -> str:
        allowed = set(ENTITY_TABLES.values()) - {"tool_audit"}
        if table not in allowed:
            raise ValueError("document table is not allowlisted")
        record_id, digest = _content_id(prefix, document)
        sql = (
            f"INSERT INTO quant_agent.{table} "
            "(record_id, actor, trade_date, category, status, content_sha256, "
            "document_json, created_at_utc) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (record_id) DO NOTHING"
        )
        with psycopg.connect(self.dsn) as connection:
            connection.execute(
                sql,
                (
                    record_id,
                    actor.value,
                    trade_date,
                    category,
                    status,
                    digest,
                    Jsonb(json.loads(_json(document))),
                    now_utc(),
                ),
            )
        return record_id

    def put_thesis(self, thesis: Thesis) -> str:
        return self._put_document(
            table="agent_theses",
            prefix="thesis",
            actor=thesis.agent,
            trade_date=thesis.trade_date,
            category=thesis.stage.value,
            status="shadow",
            document=thesis,
        )

    def put_lesson(self, lesson: Lesson) -> str:
        return self._put_document(
            table="lessons",
            prefix="lesson",
            actor=lesson.agent,
            trade_date=lesson.trade_date,
            category=lesson.category.value,
            status="accepted_fact",
            document=lesson,
        )

    def put_audit_report(self, report: AuditReport) -> str:
        return self._put_document(
            table="audit_reports",
            prefix="audit-report",
            actor=report.agent,
            trade_date=report.trade_date,
            category=None,
            status=report.status,
            document=report,
        )

    def put_proposal(self, proposal: EvolutionProposal) -> str:
        return self._put_document(
            table="evolution_proposals",
            prefix="proposal",
            actor=proposal.agent,
            trade_date=proposal.proposal_month,
            category=None,
            status="draft",
            document=proposal,
        )

    def put_tradeplan_draft(self, *, actor: AgentRole, document: Mapping[str, object]) -> str:
        if actor is not AgentRole.COMMANDER:
            raise PermissionError("only commander may store a TradePlan draft")
        if (
            document.get("status") != "shadow_draft"
            or document.get("execution_eligible") is not False
            or document.get("broker_submission_count") != 0
        ):
            raise ValueError("TradePlan draft must remain non-executable with zero submissions")
        raw_date = document.get("trade_date")
        return self._put_document(
            table="agent_tradeplan_drafts",
            prefix="tradeplan-draft",
            actor=actor,
            trade_date=date.fromisoformat(str(raw_date)),
            category=None,
            status="shadow_draft",
            document=document,
        )

    def query(self, query: StoreQuery) -> list[dict[str, object]]:
        table = ENTITY_TABLES[query.entity]
        clauses: list[str] = []
        params: list[object] = []
        if query.actor is not None:
            clauses.append("actor = %s")
            params.append(query.actor.value)
        if query.trade_date is not None:
            if table == "tool_audit":
                clauses.append("created_at_utc::date = %s")
            else:
                clauses.append("trade_date = %s")
            params.append(query.trade_date)
        if query.category is not None:
            if table != "lessons":
                raise ValueError("category filtering is only valid for lessons")
            clauses.append("category = %s")
            params.append(query.category.value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        columns = (
            "audit_id, actor, tool, request_json, response_json, success, "
            "error_code, created_at_utc"
            if table == "tool_audit"
            else "record_id, actor, trade_date, category, status, content_sha256, "
            "document_json, created_at_utc"
        )
        params.append(query.limit)
        sql = (
            f"SELECT {columns} FROM quant_agent.{table}{where} "
            "ORDER BY created_at_utc DESC LIMIT %s"
        )
        with psycopg.connect(self.dsn, row_factory=dict_row) as connection:
            rows = connection.execute(sql, params).fetchall()
        output: list[dict[str, object]] = []
        for row in rows:
            item = dict(row)
            for key in ("request_json", "response_json", "document_json"):
                if key in item and item[key] is not None:
                    item[key.removesuffix("_json")] = item.pop(key)
            for key, value in tuple(item.items()):
                if isinstance(value, date):
                    item[key] = value.isoformat()
            output.append(item)
        return output


def build_agent_fact_store(project_root: str | Path) -> AgentFactStore:
    root = Path(project_root)
    dsn = os.getenv("QUANT_AGENT_POSTGRES_DSN", "").strip()
    if dsn:
        return PostgresAgentFactStore(
            dsn,
            migration_path=root / "deploy" / "postgres" / "001_agent_facts.sql",
        )
    state_path = os.getenv("QUANT_AGENT_STATE_DB", "").strip()
    return SQLiteAgentFactStore(
        Path(state_path) if state_path else root / "runs" / "agent-facts.sqlite3"
    )
