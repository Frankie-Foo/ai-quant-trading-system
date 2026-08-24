"""Safe, idempotent writes to the investment Feishu Base.

Only state transitions are accepted.  Per-poll market data never enters this
client.  The client uses four explicitly configured tables and a local SQLite
write lock so a retry cannot race another process into creating duplicates.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

# lark-cli returns Base datetime cells without an offset; the write contract
# stores UTC, so the readback normalizer treats that presentation as UTC.
FEISHU_DISPLAY_TIMEZONE = UTC


class FeishuBaseError(RuntimeError):
    """Sanitized Feishu Base integration failure."""


class FeishuBaseDuplicateError(FeishuBaseError):
    """More than one row matched an immutable event identity."""


class InvestmentTable(StrEnum):
    """The only Base tables this process is allowed to write."""

    SELECTION = "selection"
    MONITOR = "monitor"
    TRADE = "trade"
    REVIEW = "review"


@dataclass(frozen=True)
class FeishuTableSettings:
    table_id: str
    event_id_field: str


@dataclass(frozen=True)
class FeishuBaseSettings:
    """Dedicated coordinates for the user's investment Base.

    The legacy single-table environment variables are intentionally rejected.
    A SHA-256 fingerprint is optional for local tests but recommended for a
    deployed process; when supplied, it prevents a token mix-up at startup.
    """

    base_token: str
    selection: FeishuTableSettings
    monitor: FeishuTableSettings
    trade: FeishuTableSettings
    review: FeishuTableSettings
    base_token_sha256: str | None = None
    lock_db_path: Path = Path("runs/feishu-investment-events.sqlite3")

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> FeishuBaseSettings | None:
        values = os.environ if environment is None else environment
        legacy_names = (
            "FEISHU_BASE_TOKEN",
            "FEISHU_INVESTMENT_EVENTS_TABLE_ID",
        )
        if any(values.get(name, "").strip() for name in legacy_names):
            raise RuntimeError(
                "legacy Feishu configuration is forbidden; use the dedicated "
                "FEISHU_INVESTMENT_* four-table variables"
            )

        names = {
            "base_token": "FEISHU_INVESTMENT_BASE_TOKEN",
            "selection": "FEISHU_INVESTMENT_SELECTION_TABLE_ID",
            "monitor": "FEISHU_INVESTMENT_MONITOR_TABLE_ID",
            "trade": "FEISHU_INVESTMENT_TRADE_TABLE_ID",
            "review": "FEISHU_INVESTMENT_REVIEW_TABLE_ID",
        }
        configured = any(values.get(name, "").strip() for name in names.values())
        if not configured:
            if _truthy(values.get("FEISHU_INVESTMENT_AUDIT_REQUIRED")):
                raise RuntimeError("Feishu investment audit is required but not configured")
            return None

        raw = {key: values.get(name, "").strip() for key, name in names.items()}
        missing = tuple(key for key, value in raw.items() if not value)
        if missing:
            raise RuntimeError("incomplete dedicated Feishu configuration: " + ", ".join(missing))
        table_ids = tuple(raw[key] for key in ("selection", "monitor", "trade", "review"))
        if any(not table_id.startswith("tbl") for table_id in table_ids):
            raise RuntimeError("investment Feishu table IDs must start with tbl")
        if len(set(table_ids)) != len(table_ids):
            raise RuntimeError("investment Feishu table IDs must be distinct")

        event_id_field = values.get("FEISHU_INVESTMENT_EVENT_ID_FIELD", "运行ID").strip()
        if not event_id_field:
            raise RuntimeError("FEISHU_INVESTMENT_EVENT_ID_FIELD must be non-empty")

        fingerprint = values.get("FEISHU_INVESTMENT_BASE_TOKEN_SHA256", "").strip().lower()
        if _truthy(values.get("FEISHU_INVESTMENT_AUDIT_REQUIRED")) and not fingerprint:
            raise RuntimeError(
                "FEISHU_INVESTMENT_BASE_TOKEN_SHA256 is required when investment audit is required"
            )
        if fingerprint:
            if len(fingerprint) != 64 or any(
                char not in "0123456789abcdef" for char in fingerprint
            ):
                raise RuntimeError("FEISHU_INVESTMENT_BASE_TOKEN_SHA256 must be SHA-256 hex")
            actual = hashlib.sha256(raw["base_token"].encode()).hexdigest()
            if actual != fingerprint:
                raise RuntimeError("Feishu investment Base token fingerprint mismatch")

        lock_db = Path(
            values.get(
                "FEISHU_INVESTMENT_LOCK_DB",
                "runs/feishu-investment-events.sqlite3",
            ).strip()
        )
        table_settings = {
            key: FeishuTableSettings(table_id=raw[key], event_id_field=event_id_field)
            for key in ("selection", "monitor", "trade", "review")
        }
        return cls(
            base_token=raw["base_token"],
            selection=table_settings["selection"],
            monitor=table_settings["monitor"],
            trade=table_settings["trade"],
            review=table_settings["review"],
            base_token_sha256=fingerprint or None,
            lock_db_path=lock_db,
        )

    def table(self, table: InvestmentTable) -> FeishuTableSettings:
        if table is InvestmentTable.SELECTION:
            return self.selection
        if table is InvestmentTable.MONITOR:
            return self.monitor
        if table is InvestmentTable.TRADE:
            return self.trade
        if table is InvestmentTable.REVIEW:
            return self.review
        raise ValueError(f"unsupported investment table: {table!r}")


class FeishuBaseEventClient:
    """Write immutable state transitions to one of the four allowlisted tables."""

    def __init__(
        self,
        settings: FeishuBaseSettings,
        *,
        runner: Callable[[Sequence[str]], Mapping[str, object]] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        command: str | None = None,
    ) -> None:
        self.settings = settings
        self._runner = runner
        self._sleep = sleep
        self._command = command or ("lark-cli.cmd" if os.name == "nt" else "lark-cli")

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
        **kwargs: Any,
    ) -> FeishuBaseEventClient | None:
        settings = FeishuBaseSettings.from_environment(environment)
        return None if settings is None else cls(settings, **kwargs)

    def record_event(
        self,
        table: InvestmentTable,
        event_id: str,
        fields: Mapping[str, object],
    ) -> str:
        """Create or replay one immutable event under a cross-process lock."""

        if not isinstance(table, InvestmentTable):
            raise ValueError("Feishu table must be an InvestmentTable value")
        table_settings = self.settings.table(table)
        normalized_id = event_id.strip()
        if not normalized_id:
            raise ValueError("Feishu event ID is required")
        normalized_fields = _json_safe_mapping(fields)
        supplied_id = normalized_fields.get(table_settings.event_id_field)
        if supplied_id not in (None, normalized_id):
            raise ValueError("Feishu event ID field does not match event_id")
        normalized_fields[table_settings.event_id_field] = normalized_id
        projected_fields = tuple(normalized_fields)

        with self._write_lock():
            existing = self._exact_records(
                table,
                normalized_id,
                projected_fields,
            )
            if len(existing) > 1:
                raise FeishuBaseDuplicateError(f"duplicate Feishu event identity: {normalized_id}")
            if existing:
                self._verify_fields(normalized_id, existing[0], normalized_fields)
                return str(existing[0]["record_id"])

            self._run(
                (
                    "base",
                    "+record-upsert",
                    "--base-token",
                    self.settings.base_token,
                    "--table-id",
                    table_settings.table_id,
                    "--json",
                    json.dumps(
                        normalized_fields,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                )
            )
            readback = self._readback(
                table,
                normalized_id,
                projected_fields,
                label=f"{table.value}:{normalized_id}",
            )
            self._verify_fields(normalized_id, readback, normalized_fields)
            return str(readback["record_id"])

    def check_access(self) -> dict[str, str]:
        """Read every allowlisted table without creating or updating a record."""

        checked: dict[str, str] = {}
        for table in InvestmentTable:
            settings = self.settings.table(table)
            self._exact_records(
                table,
                "__ai_quant_readonly_healthcheck__",
                (settings.event_id_field,),
            )
            checked[table.value] = settings.table_id
        return checked

    @contextmanager
    def _write_lock(self) -> Iterator[None]:
        path = self.settings.lock_db_path
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=30)
        try:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS feishu_write_lock "
                "(lock_id INTEGER PRIMARY KEY CHECK (lock_id = 1))"
            )
            connection.commit()
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("INSERT OR IGNORE INTO feishu_write_lock(lock_id) VALUES (1)")
            yield
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _readback(
        self,
        table: InvestmentTable,
        event_id: str,
        fields: Sequence[str],
        *,
        label: str,
    ) -> dict[str, Any]:
        for delay in (0.0, 0.25, 0.75, 1.5):
            if delay:
                self._sleep(delay)
            rows = self._exact_records(table, event_id, fields)
            if len(rows) > 1:
                raise FeishuBaseDuplicateError(
                    f"duplicate Feishu event identity after write: {event_id}"
                )
            if rows:
                return rows[0]
        raise FeishuBaseError(f"{label} did not survive bounded readback")

    def _exact_records(
        self,
        table: InvestmentTable,
        event_id: str,
        fields: Sequence[str],
    ) -> list[dict[str, Any]]:
        table_settings = self.settings.table(table)
        filter_json = json.dumps(
            {
                "logic": "and",
                "conditions": [[table_settings.event_id_field, "==", event_id]],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        arguments: list[str] = [
            "base",
            "+record-list",
            "--base-token",
            self.settings.base_token,
            "--table-id",
            table_settings.table_id,
            "--filter-json",
            filter_json,
            "--limit",
            "200",
        ]
        for field in fields:
            arguments.extend(("--field-id", field))
        payload = self._run(tuple(arguments))
        data = _object(payload.get("data"), "Feishu record-list data")
        names = _string_list(
            data.get("fields") or data.get("field_names"),
            "Feishu record-list fields",
        )
        rows = data.get("data") or data.get("items") or []
        if not isinstance(rows, list):
            raise FeishuBaseError("Feishu record-list rows contract is invalid")
        if data.get("has_more") is True or data.get("hasMore") is True:
            raise FeishuBaseDuplicateError(
                f"Feishu event identity query returned more than 200 rows: {event_id}"
            )
        record_ids = data.get("record_id_list") or data.get("record_ids") or []
        if not isinstance(record_ids, list):
            raise FeishuBaseError("Feishu record-list IDs contract is invalid")
        output: list[dict[str, Any]] = []
        for index, row in enumerate(rows):
            if isinstance(row, dict):
                row_fields = _json_safe_mapping(row)
                record_id = row.get("record_id") or row.get("recordId")
            elif isinstance(row, list) and index < len(record_ids):
                row_fields = dict(zip(names, row, strict=False))
                record_id = record_ids[index]
            else:
                raise FeishuBaseError("Feishu record-list row contract is invalid")
            if not isinstance(record_id, str) or not record_id.strip():
                raise FeishuBaseError("Feishu record-list record ID is missing")
            output.append(
                {
                    "record_id": record_id,
                    "fields": _json_safe_mapping(row_fields),
                }
            )
        return output

    def _run(self, arguments: Sequence[str]) -> dict[str, Any]:
        command = (*arguments, "--format", "json")
        try:
            raw = (
                self._runner(command) if self._runner is not None else self._run_subprocess(command)
            )
        except FeishuBaseError:
            raise
        except (OSError, subprocess.SubprocessError) as exc:
            raise FeishuBaseError(f"lark-cli failed: {type(exc).__name__}") from exc
        payload = _json_safe_mapping(raw)
        if payload.get("ok") is not True:
            error = payload.get("error")
            error_type = (
                str(error.get("type"))
                if isinstance(error, dict) and error.get("type")
                else "unknown"
            )
            raise FeishuBaseError(f"lark-cli returned {error_type} error")
        return payload

    def _run_subprocess(self, arguments: Sequence[str]) -> Mapping[str, object]:
        command_arguments = list(arguments)
        temporary_json: Path | None = None
        if "--json" in command_arguments:
            json_index = command_arguments.index("--json") + 1
            if json_index >= len(command_arguments):
                raise FeishuBaseError("lark-cli JSON payload is missing")
            descriptor, temporary_name = tempfile.mkstemp(
                dir=Path.cwd(),
                prefix=".lark-json-",
                suffix=".json",
                text=True,
            )
            temporary_json = Path(temporary_name)
            try:
                with os.fdopen(
                    descriptor,
                    "w",
                    encoding="utf-8",
                    newline="\n",
                ) as handle:
                    handle.write(command_arguments[json_index])
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                command_arguments[json_index] = f"@./{temporary_json.name}"
            except Exception:
                temporary_json.unlink(missing_ok=True)
                raise
        completed = subprocess.run(
            [self._command, *command_arguments],
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=45,
        )
        try:
            try:
                payload = json.loads(completed.stdout or "{}")
            except json.JSONDecodeError as exc:
                raise FeishuBaseError("lark-cli returned invalid JSON") from exc
            if not isinstance(payload, dict):
                raise FeishuBaseError("lark-cli response contract is invalid")
            if completed.returncode != 0:
                raise FeishuBaseError("lark-cli command returned a non-zero exit code")
            return cast(dict[str, object], payload)
        finally:
            if temporary_json is not None:
                temporary_json.unlink(missing_ok=True)

    def _verify_fields(
        self,
        event_id: str,
        row: Mapping[str, object],
        expected: Mapping[str, object],
    ) -> None:
        actual = row.get("fields")
        if not isinstance(actual, Mapping):
            raise FeishuBaseError(f"Feishu readback fields missing: {event_id}")
        for field, expected_value in expected.items():
            actual_value = actual.get(field)
            # Feishu may return legacy date cells using the display timezone;
            # event identity makes the timestamp immutable and replay-safe.
            if (
                isinstance(expected_value, (int, float))
                and not isinstance(expected_value, bool)
                and abs(float(expected_value)) >= 100_000_000_000
                and isinstance(actual_value, str)
                and "T" in actual_value
            ):
                continue
            if _cell_text(actual_value) != _cell_text(expected_value):
                raise FeishuBaseError(f"Feishu readback mismatch for event {event_id}: {field}")


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _json_safe_mapping(value: Mapping[object, object] | object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise FeishuBaseError("Feishu JSON object contract is invalid")
    return {str(key): _json_safe(item) for key, item in value.items()}


def _json_safe(value: object) -> Any:
    if isinstance(value, datetime):
        aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return round(aware.timestamp() * 1000)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return _json_safe_mapping(value)
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise FeishuBaseError(f"unsupported Feishu field value: {type(value).__name__}")


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise FeishuBaseError(f"{name} contract is invalid")
    return cast(dict[str, Any], value)


def _string_list(value: object, name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise FeishuBaseError(f"{name} contract is invalid")
    return list(value)


def _cell_text(value: object) -> str:
    if isinstance(value, list) and len(value) == 1:
        return _cell_text(value[0])
    if isinstance(value, Mapping):
        if "text" in value:
            return str(value["text"])
        if "value" in value:
            return _cell_text(value["value"])
    if isinstance(value, (dict, list)):
        return json.dumps(_json_safe(value), ensure_ascii=False, sort_keys=True)
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        numeric = float(value)
        if 100_000_000_000 <= abs(numeric) <= 100_000_000_000_000:
            try:
                return _datetime_text(datetime.fromtimestamp(numeric / 1000, UTC))
            except (OverflowError, OSError, ValueError):
                pass
        try:
            normalized = Decimal(str(value)).normalize()
        except (ArithmeticError, ValueError):
            return str(value)
        return format(normalized, "f")
    if value is None:
        return ""
    if isinstance(value, datetime):
        return _datetime_text(value)
    if isinstance(value, str):
        return _datetime_text_or_text(value)
    return str(value)


def _datetime_text(value: datetime) -> str:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).replace(microsecond=0).isoformat()


def _datetime_text_or_text(value: str) -> str:
    candidate = value.strip()
    if "T" not in candidate and " " not in candidate:
        return value
    try:
        parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError:
        return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=FEISHU_DISPLAY_TIMEZONE)
    return _datetime_text(parsed)
