"""Immutable investment events through the installed VPS Work CLI.

The legacy client type is retained for callers; no Feishu API is used. A local
write-ahead intent prevents replaying an ambiguous remote append after restart.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from db.migrations.sqlite import SQLiteMigration, apply_sqlite_migrations
from operations.feishu_base import (
    FeishuBaseError,
    FeishuBaseEventClient,
    FeishuBaseSettings,
    FeishuTableSettings,
    InvestmentTable,
    _json_safe_mapping,
)

SERVICE_URL = "https://vps-service.vertu.cn"
Runner = Callable[[tuple[str, ...], str | None], Mapping[str, object]]


def _create_intents(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE vps_event_intents (
        identity TEXT PRIMARY KEY, payload TEXT NOT NULL, record_id TEXT
    )""")


def _create_projection_queue(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE vps_projection_queue (
        identity TEXT PRIMARY KEY, doc_id TEXT NOT NULL, table_name TEXT NOT NULL,
        table_id TEXT NOT NULL, event_id TEXT NOT NULL, payload TEXT NOT NULL,
        record_id TEXT, last_attempt TEXT NOT NULL DEFAULT '', last_error TEXT
    )""")


INTENT_MIGRATIONS = (
    SQLiteMigration(
        version=1, name="vps_event_intents", signature="vps_event_intents.v1", apply=_create_intents
    ),
    SQLiteMigration(
        version=2, name="vps_projection_queue", signature="vps_projection_queue.v1",
        apply=_create_projection_queue,
    ),
)


@dataclass(frozen=True)
class VpsInvestmentSettings:
    doc_id: str
    tables: dict[str, dict[str, str]]
    state_db: Path

    @classmethod
    def from_environment(cls, values: Mapping[str, str]) -> VpsInvestmentSettings:
        names = (
            "VPS_INVESTMENT_BINDING_FILE",
            "VPS_INVESTMENT_BINDING_SHA256",
            "VPS_INVESTMENT_STATE_DB",
        )
        if any(not values.get(name, "").strip() for name in names):
            raise ValueError("VPS investment binding, fingerprint and state DB are required")
        raw = Path(values[names[0]]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != values[names[1]].strip().lower():
            raise ValueError("VPS investment binding fingerprint mismatch")
        data = json.loads(raw.decode("utf-8"))
        doc_id = str(UUID(data["doc_id"]))
        tables = data["tables"]
        expected = {table.value for table in InvestmentTable}
        if not isinstance(tables, dict) or set(tables) != expected:
            raise ValueError("VPS investment requires four explicit tables")
        fields = {"table_id", "event_id", "payload", "symbol", "summary"}
        for entry in tables.values():
            if not isinstance(entry, dict) or set(entry) != fields:
                raise ValueError("VPS investment field binding is incomplete")
            for value in entry.values():
                if not isinstance(value, str) or str(UUID(value)) != value:
                    raise ValueError("VPS investment IDs must be canonical UUIDs")
            if len(set(entry.values())) != len(fields):
                raise ValueError("VPS investment field IDs must be distinct")
        if len({entry["table_id"] for entry in tables.values()}) != 4:
            raise ValueError("VPS investment tables must be distinct")
        state_db = Path(values[names[2]])
        if not state_db.is_absolute():
            raise ValueError("VPS investment state DB must be absolute")
        return cls(doc_id=doc_id, tables=tables, state_db=state_db)


class VpsInvestmentClient(FeishuBaseEventClient):
    """Preserve the existing event port while selecting VPS as the only backend."""

    def __init__(
        self,
        settings: VpsInvestmentSettings,
        *,
        runner: Runner | None = None,
        sleep: Callable[[float], None] = time.sleep,
        command: tuple[str, ...] | None = None,
    ) -> None:
        # Reuse the proven inter-process lock, not the Feishu transport/field coercion.
        tables = {
            name: FeishuTableSettings(entry["table_id"], "运行ID")
            for name, entry in settings.tables.items()
        }
        super().__init__(
            FeishuBaseSettings(
                base_token=settings.doc_id,
                selection=tables["selection"],
                monitor=tables["monitor"],
                trade=tables["trade"],
                review=tables["review"],
                lock_db_path=settings.state_db.with_suffix(".lock.sqlite3"),
            )
        )
        self.vps = settings
        self._vps_runner = runner
        self._vps_command = command
        self._sleep = sleep

    def queue_events(
        self, events: Sequence[tuple[InvestmentTable, str, Mapping[str, object]]]
    ) -> None:
        """Freeze the whole batch before any network I/O, including not-yet-attempted rows."""
        self.vps.state_db.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.vps.state_db, timeout=30)) as connection:
            apply_sqlite_migrations(
                connection, owner="operations.vps_event_intents", migrations=INTENT_MIGRATIONS
            )
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                for table, event_id, fields in events:
                    event_id, payload = self._payload(table, event_id, fields)
                    table_id = self.vps.tables[table.value]["table_id"]
                    identity = f"{self.vps.doc_id}:{table_id}:{event_id}"
                    previous = connection.execute(
                        "SELECT payload FROM vps_projection_queue WHERE identity=?", (identity,)
                    ).fetchone()
                    if previous is not None and previous[0] != payload:
                        raise FeishuBaseError("VPS queued immutable payload conflict")
                    connection.execute(
                        "INSERT OR IGNORE INTO vps_projection_queue "
                        "(identity,doc_id,table_name,table_id,event_id,payload) "
                        "VALUES (?,?,?,?,?,?)",
                        (identity, self.vps.doc_id, table.value, table_id, event_id, payload),
                    )

    def flush_pending(self, *, limit: int = 10) -> dict[str, int]:
        self.queue_events([])
        with closing(sqlite3.connect(self.vps.state_db, timeout=30)) as connection:
            rows = connection.execute(
                "SELECT identity,table_name,table_id,event_id,payload FROM vps_projection_queue "
                "WHERE doc_id=? AND record_id IS NULL ORDER BY last_attempt,identity LIMIT ?",
                (self.vps.doc_id, limit),
            ).fetchall()
        result = {"delivered": 0, "failed": 0}
        for identity, table_name, table_id, event_id, payload in rows:
            error = None
            try:
                if self.vps.tables[table_name]["table_id"] != table_id:
                    raise FeishuBaseError("VPS queued destination changed")
                self.record_event(InvestmentTable(table_name), event_id, json.loads(payload))
                result["delivered"] += 1
            except (FeishuBaseError, ValueError, OSError) as exc:
                error = type(exc).__name__  # no credentials or raw CLI output in audit
                result["failed"] += 1
            with closing(sqlite3.connect(self.vps.state_db, timeout=30)) as connection:
                with connection:
                    connection.execute(
                        "UPDATE vps_projection_queue SET last_attempt=?,last_error=? "
                        "WHERE identity=?", (datetime.now(UTC).isoformat(), error, identity),
                    )
        return result

    def _call(self, arguments: tuple[str, ...], stdin: str | None = None) -> dict[str, Any]:
        args = ("docs", *arguments, "--doc-id", self.vps.doc_id, "--base-url", SERVICE_URL)
        try:
            if self._vps_runner is not None:
                result = self._vps_runner(args, stdin)
            else:
                command = self._vps_command
                if command is None:
                    executable = shutil.which("vps-work.cmd" if os.name == "nt" else "vps-work")
                    if executable is None:
                        raise FeishuBaseError("vps-work is not installed")
                    if os.name == "nt":
                        node = shutil.which("node")
                        script = Path(executable).parent / "node_modules/vps-work/dist/vps-work.cjs"
                        if node is None or not script.is_file():
                            raise FeishuBaseError("vps-work Node entrypoint is unavailable")
                        command = (node, str(script))
                    else:
                        command = (executable,)
                completed = subprocess.run(
                    [*command, *args],
                    input=stdin,
                    capture_output=True,
                    shell=False,
                    text=True,
                    encoding="utf-8",
                    errors="strict",
                    timeout=45,
                    check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    if os.name == "nt"
                    else 0,
                )
                if completed.returncode != 0:
                    raise FeishuBaseError(f"vps-work failed: exit={completed.returncode}")
                if len(completed.stdout) > 2_000_000:
                    raise FeishuBaseError("vps-work response exceeds size limit")
                result = json.loads(completed.stdout)
            if not isinstance(result, Mapping) or result.get("error") or result.get("ok") is False:
                raise FeishuBaseError("vps-work returned an invalid response")
            return dict(result)
        except subprocess.TimeoutExpired:
            raise FeishuBaseError("vps-work failed: timeout") from None
        except (OSError, subprocess.SubprocessError, ValueError, RecursionError):
            raise FeishuBaseError(
                "vps-work failed: invalid response or process unavailable"
            ) from None

    def check_access(self) -> dict[str, str]:
        base = self._call(("+base-schema",)).get("base")
        if not isinstance(base, dict) or base.get("docId") != self.vps.doc_id:
            raise FeishuBaseError("VPS schema document mismatch")
        tables = base.get("tables")
        if not isinstance(tables, list):
            raise FeishuBaseError("VPS schema tables missing")
        checked = {}
        for table in InvestmentTable:
            entry = self.vps.tables[table.value]
            matches = [
                item
                for item in tables
                if isinstance(item, dict) and item.get("id") == entry["table_id"]
            ]
            if len(matches) != 1 or not isinstance(matches[0].get("fields"), list):
                raise FeishuBaseError("VPS schema table missing or duplicated")
            columns = {
                item.get("id"): item.get("type")
                for item in matches[0]["fields"]
                if isinstance(item, dict)
            }
            for key in ("event_id", "payload", "symbol", "summary"):
                expected = "longText" if key in {"payload", "summary"} else "singleLineText"
                if columns.get(entry[key]) != expected:
                    raise FeishuBaseError("VPS schema field missing or wrong type")
            self._find(table, "__ai_quant_readonly_healthcheck__", None)
            checked[table.value] = entry["table_id"]
        return checked

    def _find(
        self,
        table: InvestmentTable,
        event_id: str,
        expected_fields: Mapping[str, str] | None,
    ) -> str | None:
        entry = self.vps.tables[table.value]
        response = self._call(
            ("+base-records", "--table", entry["table_id"], "--query", event_id, "--limit", "200")
        )
        rows, total = response.get("records"), response.get("total")
        if not isinstance(rows, list) or type(total) is not int or total < 0:
            raise FeishuBaseError("VPS records contract is invalid")
        if total != len(rows) or len(rows) >= 200 or response.get("hasMore"):
            raise FeishuBaseError("VPS records query truncated")
        found = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("fields"), dict):
                raise FeishuBaseError("VPS record fields missing")
            fields = row["fields"]
            if fields.get(entry["event_id"]) != event_id:
                continue
            if not isinstance(row.get("id"), str) or not row["id"].strip():
                raise FeishuBaseError("VPS record ID missing")
            if expected_fields is not None:
                for field_id, expected in expected_fields.items():
                    if fields.get(field_id) != expected:
                        raise FeishuBaseError("VPS immutable record mismatch")
            found.append(row["id"])
        if len(found) > 1:
            raise FeishuBaseError("VPS duplicate immutable event")
        return found[0] if found else None

    @staticmethod
    def _payload(
        table: InvestmentTable,
        event_id: str,
        fields: Mapping[str, object],
    ) -> tuple[str, str]:
        if not isinstance(table, InvestmentTable) or not event_id.strip() or len(event_id) > 1000:
            raise ValueError("VPS investment event identity is invalid")
        event_id = event_id.strip()
        normalized = _json_safe_mapping(fields)
        if normalized.get("运行ID") not in (None, event_id):
            raise ValueError("VPS event identity conflicts with fields")
        normalized["运行ID"] = event_id
        payload = json.dumps(normalized, ensure_ascii=False, sort_keys=True, allow_nan=False)
        if (
            "\ufffd" in payload
            or "??" in payload
            or payload.encode("utf-8").decode("utf-8") != payload
        ):
            raise ValueError("VPS investment text encoding is invalid")
        return event_id, payload

    def record_event(
        self, table: InvestmentTable, event_id: str, fields: Mapping[str, object]
    ) -> str:
        event_id, payload = self._payload(table, event_id, fields)
        self.queue_events([(table, event_id, fields)])
        normalized = json.loads(payload)
        entry = self.vps.tables[table.value]
        remote_fields = {
            entry["event_id"]: event_id,
            entry["payload"]: payload,
            entry["symbol"]: str(normalized.get("股票代码", "")),
            entry["summary"]: str(
                normalized.get("执行摘要")
                or normalized.get("触发理由")
                or normalized.get("复盘结论")
                or event_id
            ),
        }
        identity = f"{self.vps.doc_id}:{entry['table_id']}:{event_id}"
        with self._write_lock():
            connection = sqlite3.connect(self.vps.state_db, timeout=30)
            try:
                apply_sqlite_migrations(
                    connection, owner="operations.vps_event_intents", migrations=INTENT_MIGRATIONS
                )
                intent = connection.execute(
                    "SELECT payload, record_id FROM vps_event_intents WHERE identity=?",
                    (identity,),
                ).fetchone()
                if intent is not None and intent[0] != payload:
                    raise FeishuBaseError("VPS local immutable payload conflict")
                record_id = self._find(table, event_id, remote_fields)
                if intent is not None and intent[1] and record_id != intent[1]:
                    raise FeishuBaseError("VPS confirmed record changed or disappeared")
                if record_id is None:
                    if intent is not None:
                        raise FeishuBaseError("VPS uncertain write: reconciliation required")
                    connection.execute(
                        "INSERT INTO vps_event_intents(identity,payload) VALUES (?,?)",
                        (identity, payload),
                    )
                    connection.commit()  # durable intent BEFORE the append leaves this process
                    record = {"fields": remote_fields}
                    try:
                        self._call(
                            (
                                "+base-create-records",
                                "--table",
                                entry["table_id"],
                                "--records-file",
                                "-",
                            ),
                            json.dumps([record], ensure_ascii=False),
                        )
                    except FeishuBaseError:
                        pass  # reconcile even if the write timed out after server acceptance
                    for delay in (0.0, 0.25, 0.75, 1.5):
                        if delay:
                            self._sleep(delay)
                        record_id = self._find(table, event_id, remote_fields)
                        if record_id is not None:
                            break
                    if record_id is None:
                        raise FeishuBaseError("VPS uncertain write: no verified readback")
                connection.execute(
                    "INSERT INTO vps_event_intents(identity,payload,record_id) VALUES (?,?,?) "
                    "ON CONFLICT(identity) DO UPDATE SET record_id=excluded.record_id",
                    (identity, payload, record_id),
                )
                connection.execute(
                    "UPDATE vps_projection_queue SET record_id=?,last_error=NULL WHERE identity=?",
                    (record_id, identity),
                )
                connection.commit()
                return f"vps-work:{self.vps.doc_id}:{entry['table_id']}:{record_id}"
            finally:
                connection.close()
