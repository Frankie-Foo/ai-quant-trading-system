"""Run fail-closed discipline and optional structured PDCA after program review."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import cast

import polars as pl
from dotenv import load_dotenv

from agent_gateway.contracts import (
    AgentRole,
    AuditReport,
    Fact,
    QueryEntity,
    StoreQuery,
)
from agent_gateway.service import AgentGatewayService
from data_plane.contracts import DatasetSnapshot
from research.pdca_agents import (
    lesson_review_prompt,
    materialize_lessons,
    parse_lesson_review,
)
from research.program_review import ProgramReview
from research.providers.deepseek import DeepSeekClient

ROOT = Path(__file__).resolve().parents[1]


class LlmMode(StrEnum):
    OFF = "off"
    OPTIONAL = "optional"
    REQUIRED = "required"


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


def _latest_program_review(
    data_root: Path, trade_date: date
) -> tuple[ProgramReview, DatasetSnapshot]:
    matches: list[tuple[DatasetSnapshot, Path]] = []
    for path in (data_root / "accepted").glob("research.postmarket.program_review-*/data.parquet"):
        frame = pl.read_parquet(path, columns=["session_date"])
        if frame.get_column("session_date").unique().to_list() != [trade_date]:
            continue
        manifest = DatasetSnapshot.model_validate_json(
            (path.parent / "manifest.json").read_text(encoding="utf-8")
        )
        manifest.assert_usable()
        matches.append((manifest, path))
    if not matches:
        raise FileNotFoundError("accepted program review is unavailable")
    manifest, path = max(matches, key=lambda item: item[0].asof_utc)
    frame = pl.read_parquet(path)
    if frame.height != 1:
        raise ValueError("program review snapshot must contain exactly one row")
    raw = frame.row(0, named=True).get("program_review_json")
    if not isinstance(raw, str):
        raise ValueError("program review JSON is unavailable")
    return ProgramReview.model_validate_json(raw), manifest


def _data(envelope: dict[str, object]) -> list[dict[str, object]]:
    value = envelope.get("data")
    if not isinstance(value, list):
        raise TypeError("query envelope data must be a list")
    return [cast(dict[str, object], row) for row in value if isinstance(row, dict)]


def _run_discipline(service: AgentGatewayService, trade_date: date) -> tuple[str, tuple[str, ...]]:
    entities = (
        QueryEntity.TRADE_PLANS,
        QueryEntity.EXECUTIONS,
        QueryEntity.BARRIER_EVENTS,
    )
    envelopes = [
        service.postgres_query(
            agent_name="discipline",
            query=StoreQuery(entity=entity, trade_date=trade_date, limit=200),
        )
        for entity in entities
    ]
    complete = all(envelope.get("availability") == "available" for envelope in envelopes)
    source_ids = tuple(
        str(value)
        for envelope in envelopes
        for value in cast(list[object], envelope.get("snapshot_ids", []))
    )
    report = AuditReport(
        agent=AgentRole.DISCIPLINE,
        trade_date=trade_date,
        status="complete" if complete and source_ids else "incomplete_evidence",
        findings=(),
        source_record_ids=source_ids,
    )
    result = service.audit_reports_write(agent_name="discipline", report=report)
    data = cast(dict[str, object], result["data"])
    return str(data["record_id"]), tuple(
        entity.value
        for entity, envelope in zip(entities, envelopes, strict=True)
        if envelope.get("availability") != "available"
    )


def _pdca_fact_package(
    service: AgentGatewayService, trade_date: date
) -> tuple[str, dict[str, Fact], tuple[str, ...]]:
    episode = service.postgres_query(
        agent_name="pdca",
        query=StoreQuery(
            entity=QueryEntity.TRADING_EPISODES,
            trade_date=trade_date,
            limit=200,
        ),
    )
    if episode.get("availability") != "available":
        raise ValueError("accepted trading episode is unavailable")
    rows = _data(episode)
    metric_index: dict[str, Fact] = {}
    for row in rows:
        case_id = row.get("case_id")
        facts = row.get("facts")
        if not isinstance(case_id, str) or not isinstance(facts, list):
            raise ValueError("anonymous episode row is malformed")
        for raw_fact in facts:
            fact = Fact.model_validate(raw_fact)
            reference = f"{case_id}:{fact.name}"
            metric_index[reference] = fact
            if isinstance(raw_fact, dict):
                raw_fact["fact_ref"] = reference
    snapshot_ids = tuple(str(value) for value in cast(list[object], episode["snapshot_ids"]))
    package = {
        "trade_date": trade_date.isoformat(),
        "snapshot_ids": snapshot_ids,
        "anonymous_cases": rows,
    }
    return (
        json.dumps(package, ensure_ascii=False, sort_keys=True, default=str),
        metric_index,
        snapshot_ids,
    )


def run(argv: list[str] | None = None) -> int:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser()
    parser.add_argument("--trade-date", type=_parse_date, required=True)
    parser.add_argument("--data-root", type=Path, default=ROOT / "data")
    parser.add_argument(
        "--llm-mode", type=LlmMode, choices=tuple(LlmMode), default=LlmMode.OPTIONAL
    )
    args = parser.parse_args(argv)
    service = AgentGatewayService(project_root=ROOT, data_root=args.data_root)
    report_id, missing_ledgers = _run_discipline(service, args.trade_date)
    program_review, program_snapshot = _latest_program_review(args.data_root, args.trade_date)
    result: dict[str, object] = {
        "trade_date": args.trade_date.isoformat(),
        "audit_report_id": report_id,
        "missing_ledgers": missing_ledgers,
        "lesson_ids": [],
        "orders_submitted": 0,
        "production_changes": 0,
    }
    if args.llm_mode is LlmMode.OFF:
        result["status"] = "llm_off"
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if not program_review.llm_research_allowed:
        result["status"] = "skipped_by_program_gate"
        result["program_status"] = program_review.status.value
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    fact_package, metric_index, episode_ids = _pdca_fact_package(service, args.trade_date)
    prompt, prompt_hash = lesson_review_prompt(fact_package)
    request_audit = {
        "prompt_sha256": prompt_hash,
        "fact_package_sha256": hashlib.sha256(fact_package.encode()).hexdigest(),
        "source_snapshot_ids": (*episode_ids, program_snapshot.dataset_id),
    }
    try:
        response = DeepSeekClient.from_env().complete_json(prompt, max_tokens=4096)
        service.store.record_audit(
            actor=AgentRole.PDCA,
            tool="deepseek_pdca_review",
            request=request_audit,
            response={
                "content": response.content,
                "provider_request_id": response.provider_request_id,
                "response_model": response.response_model,
                "system_fingerprint": response.system_fingerprint,
                "prompt_tokens": response.prompt_tokens,
                "completion_tokens": response.completion_tokens,
            },
            success=True,
            error_code=None,
        )
        review = parse_lesson_review(response.content)
        lessons = materialize_lessons(
            review,
            trade_date=args.trade_date,
            metric_index=metric_index,
            source_record_ids=(*episode_ids, program_snapshot.dataset_id),
        )
        lesson_ids = []
        for lesson in lessons:
            stored = service.lessons_write(agent_name="pdca", lesson=lesson)
            lesson_ids.append(str(cast(dict[str, object], stored["data"])["record_id"]))
        result["status"] = "complete"
        result["lesson_ids"] = lesson_ids
    except Exception as exc:
        service.store.record_audit(
            actor=AgentRole.PDCA,
            tool="deepseek_pdca_review",
            request=request_audit,
            response=None,
            success=False,
            error_code=type(exc).__name__,
        )
        if args.llm_mode is LlmMode.REQUIRED:
            raise
        result["status"] = "failed_optional"
        result["error_code"] = type(exc).__name__
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
