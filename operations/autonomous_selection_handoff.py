"""Freeze a selection, publish its Paper plan, and make it ready for monitoring."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Protocol

from execution.locked_selection import LockedSelection, load_locked_selection
from operations.autonomous_notifications import AutonomousNotificationLedger
from operations.autonomous_paper_config import load_autonomous_paper_config
from operations.autonomous_plan_compiler import compile_autonomous_paper_plans
from operations.feishu_investment_events import InvestmentEventPort, record_locked_selection
from operations.paper_runtime_policy import ExecutionAuthorization

OPEN_CONFIRMATION_SCHEMA = "open_confirmation.v1"


class PushPort(Protocol):
    def push(self, body: str) -> str: ...


@dataclass(frozen=True)
class AutonomousSelectionHandoff:
    config_path: Path
    symbols: tuple[str, ...]
    selection_snapshot_id: str
    message_id: str | None
    authorization: ExecutionAuthorization | None


@dataclass(frozen=True)
class OpenConfirmation:
    authorization: ExecutionAuthorization
    config_path: Path
    generated_at_utc: datetime


def prepare_autonomous_selection_handoff(
    *,
    data_root: Path,
    trade_date: date,
    output_path: Path,
    confirmation_path: Path | None = None,
    notification_db: Path,
    push: PushPort,
    audit: InvestmentEventPort | None = None,
    observed_at_utc: datetime | None = None,
    max_plans: int = 5,
    strategy_version: str = "modern-h15.v1",
) -> AutonomousSelectionHandoff:
    """Publish exactly one selection-plan message before autonomous monitoring."""

    observed_at = observed_at_utc or datetime.now(UTC)
    if observed_at.tzinfo is None or observed_at.utcoffset() != UTC.utcoffset(observed_at):
        raise ValueError("observed_at_utc must be UTC")
    prepared = compile_autonomous_paper_plans(
        data_root=data_root,
        trade_date=trade_date,
        output_path=output_path,
        max_plans=max_plans,
    )
    selection = load_locked_selection(data_root, trade_date, min_rvol=3.0)
    config = load_autonomous_paper_config(output_path)
    symbols = tuple(bundle.plan.symbol for bundle in config.plans)
    if tuple(item.symbol for item in prepared) != symbols:
        raise RuntimeError("selection-plan handoff symbols diverged")
    audit_record_ids: tuple[str, ...] = ()
    if audit is not None:
        audit_record_ids = record_locked_selection(
            audit,
            selection,
            observed_at_utc=observed_at,
        )
    key = f"selection-plan:{trade_date.isoformat()}:{selection.snapshot.dataset_id}"
    ledger = AutonomousNotificationLedger(notification_db)
    claim = ledger.claim(key, claimed_at_utc=observed_at)
    if claim == "sent":
        authorization = _existing_or_new_authorization(
            confirmation_path=confirmation_path,
            config_path=output_path,
            trade_date=trade_date,
            selection_snapshot_id=selection.snapshot.dataset_id,
            candidate_pool=symbols,
            feishu_record_ids=audit_record_ids,
            livermore_message_id=_sent_message_id(ledger, key),
            strategy_version=strategy_version,
            generated_at_utc=observed_at,
        )
        return AutonomousSelectionHandoff(
            config_path=output_path,
            symbols=symbols,
            selection_snapshot_id=selection.snapshot.dataset_id,
            message_id=None,
            authorization=authorization,
        )
    if claim != "claimed":
        raise RuntimeError("selection-plan notification is already in flight")
    message = format_selection_plan_message(
        selection,
        config_path=output_path,
        symbols=symbols,
    )
    try:
        message_id = push.push(message)
    except Exception:
        ledger.release_claim(key)
        raise
    ledger.record(
        key,
        message_id=message_id,
        sent_at_utc=observed_at,
        message_body=message,
        payload={
            "trade_date": trade_date.isoformat(),
            "symbols": list(symbols),
            "selection_snapshot_id": selection.snapshot.dataset_id,
            "config_path": str(output_path),
        },
    )
    authorization = _existing_or_new_authorization(
        confirmation_path=confirmation_path,
        config_path=output_path,
        trade_date=trade_date,
        selection_snapshot_id=selection.snapshot.dataset_id,
        candidate_pool=symbols,
        feishu_record_ids=audit_record_ids,
        livermore_message_id=message_id,
        strategy_version=strategy_version,
        generated_at_utc=observed_at,
    )
    return AutonomousSelectionHandoff(
        config_path=output_path,
        symbols=symbols,
        selection_snapshot_id=selection.snapshot.dataset_id,
        message_id=message_id,
        authorization=authorization,
    )


def format_selection_plan_message(
    selection: LockedSelection,
    *,
    config_path: Path,
    symbols: tuple[str, ...] | None = None,
) -> str:
    """Compact initial notification; later messages are actual Paper actions only."""

    selected = set(symbols or selection.symbols)
    rows = [
        (
            f"{candidate.selection_rank}. {candidate.symbol} | RVOL {candidate.rvol:.2f} | "
            f"盘前 {_percent_or_na(candidate.premarket_return)} | "
            f"参考价 ${candidate.premarket_close or candidate.price:.2f}"
        )
        for candidate in selection.candidates
        if candidate.symbol in selected
    ]
    return "\n".join(
        (
            "【AI量化｜今日选股与模拟盘预案】",
            f"交易日：{selection.trade_date.isoformat()}",
            *rows,
            "执行：以上标的进入自动盯盘；仅当实时条件和风控同时通过时提交 Alpaca Paper 订单。",
            "通知：此后仅推送实际模拟买入或卖出；每秒策略评估不推送。",
            f"证据：{selection.snapshot.dataset_id}",
            f"计划：{config_path.name}",
        )
    )


def _percent_or_na(value: float | None) -> str:
    return "N/A" if value is None else f"{value:+.2%}"


def _sent_message_id(ledger: AutonomousNotificationLedger, key: str) -> str:
    for record in ledger.list_records():
        if record["notification_key"] == key:
            return str(record["message_id"])
    return ""


def _existing_or_new_authorization(
    *,
    confirmation_path: Path | None,
    config_path: Path,
    trade_date: date,
    selection_snapshot_id: str,
    candidate_pool: tuple[str, ...],
    feishu_record_ids: tuple[str, ...],
    livermore_message_id: str,
    strategy_version: str,
    generated_at_utc: datetime,
) -> ExecutionAuthorization | None:
    if confirmation_path is None or not feishu_record_ids or not livermore_message_id.strip():
        return None
    if confirmation_path.exists():
        existing = load_open_confirmation(confirmation_path).authorization
        if (
            existing.trade_date != trade_date
            or existing.selection_snapshot_id != selection_snapshot_id
            or existing.candidate_pool != candidate_pool
            or existing.strategy_version != strategy_version
        ):
            raise RuntimeError("open confirmation identity does not match this handoff")
        return existing
    config_sha256 = hashlib.sha256(config_path.read_bytes()).hexdigest()
    material: dict[str, object] = {
        "trade_date": trade_date.isoformat(),
        "selection_snapshot_id": selection_snapshot_id,
        "candidate_pool": list(candidate_pool),
        "feishu_record_ids": list(feishu_record_ids),
        "livermore_message_id": livermore_message_id,
        "strategy_version": strategy_version,
        "config_sha256": config_sha256,
    }
    confirmation_id = hashlib.sha256(_canonical_json(material)).hexdigest()
    authorization = ExecutionAuthorization(
        trade_date=trade_date,
        selection_snapshot_id=selection_snapshot_id,
        open_confirmation_id=confirmation_id,
        feishu_record_id=",".join(feishu_record_ids),
        livermore_message_id=livermore_message_id,
        strategy_version=strategy_version,
        candidate_pool=candidate_pool,
        config_sha256=config_sha256,
    )
    if not authorization.is_complete():
        return None
    payload: dict[str, object] = {
        "schema_version": OPEN_CONFIRMATION_SCHEMA,
        "generated_at_utc": generated_at_utc.isoformat(),
        "config_path": str(config_path),
        "authorization": {
            "trade_date": trade_date.isoformat(),
            "selection_snapshot_id": selection_snapshot_id,
            "open_confirmation_id": confirmation_id,
            "feishu_record_id": authorization.feishu_record_id,
            "livermore_message_id": livermore_message_id,
            "strategy_version": strategy_version,
            "candidate_pool": list(candidate_pool),
            "config_sha256": config_sha256,
        },
    }
    _write_immutable_json(confirmation_path, payload)
    return authorization


def load_open_confirmation(path: Path) -> OpenConfirmation:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("open confirmation is unreadable") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != OPEN_CONFIRMATION_SCHEMA:
        raise ValueError("unsupported open confirmation")
    raw = payload.get("authorization")
    if not isinstance(raw, dict):
        raise ValueError("open confirmation authorization is missing")
    try:
        candidate_pool = tuple(str(item) for item in raw["candidate_pool"])
        authorization = ExecutionAuthorization(
            trade_date=date.fromisoformat(str(raw["trade_date"])),
            selection_snapshot_id=str(raw["selection_snapshot_id"]),
            open_confirmation_id=str(raw["open_confirmation_id"]),
            feishu_record_id=str(raw["feishu_record_id"]),
            livermore_message_id=str(raw["livermore_message_id"]),
            strategy_version=str(raw["strategy_version"]),
            candidate_pool=candidate_pool,
            config_sha256=str(raw["config_sha256"]),
        )
        generated_at_utc = datetime.fromisoformat(str(payload["generated_at_utc"]))
        config_path = Path(str(payload["config_path"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("open confirmation fields are invalid") from exc
    if not authorization.is_complete():
        raise ValueError("open confirmation is incomplete")
    material: dict[str, object] = {
        "trade_date": authorization.trade_date.isoformat(),
        "selection_snapshot_id": authorization.selection_snapshot_id,
        "candidate_pool": list(authorization.candidate_pool),
        "feishu_record_ids": authorization.feishu_record_id.split(","),
        "livermore_message_id": authorization.livermore_message_id,
        "strategy_version": authorization.strategy_version,
        "config_sha256": authorization.config_sha256,
    }
    expected_id = hashlib.sha256(_canonical_json(material)).hexdigest()
    if expected_id != authorization.open_confirmation_id:
        raise ValueError("open confirmation hash does not match its contents")
    config_hash = (
        hashlib.sha256(config_path.read_bytes()).hexdigest()
        if config_path.is_file()
        else ""
    )
    if config_hash != authorization.config_sha256:
        raise ValueError("open confirmation config hash does not match")
    return OpenConfirmation(
        authorization=authorization,
        config_path=config_path,
        generated_at_utc=generated_at_utc,
    )


def _canonical_json(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _write_immutable_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") != encoded:
            raise RuntimeError("open confirmation already exists with different contents")
        return
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
