"""Generate idempotent draft-only evolution proposals from governed lessons."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import cast

from dotenv import load_dotenv

from agent_gateway.contracts import AgentRole, Availability, Fact, QueryEntity, StoreQuery
from agent_gateway.service import AgentGatewayService
from research.monthly_evolution_agents import (
    materialize_proposals,
    monthly_proposal_prompt,
    parse_monthly_review,
)
from research.providers.deepseek import DeepSeekClient

ROOT = Path(__file__).resolve().parents[1]
MIN_CLUSTER_OBSERVATIONS = 10
MIN_CLUSTER_SESSIONS = 10
MIN_CLUSTER_PROVENANCE = "quant-agent-plugin-v1.2|pdca.minimum_cluster_observations"


class LlmMode(StrEnum):
    OFF = "off"
    OPTIONAL = "optional"
    REQUIRED = "required"


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


def _rows(envelope: dict[str, object]) -> list[dict[str, object]]:
    value = envelope.get("data")
    if not isinstance(value, list):
        raise TypeError("query envelope data must be a list")
    return [cast(dict[str, object], row) for row in value if isinstance(row, dict)]


def _build_package(
    service: AgentGatewayService,
) -> tuple[
    str,
    dict[str, frozenset[str]],
    dict[str, Fact],
    tuple[str, ...],
]:
    lesson_envelope = service.postgres_query(
        agent_name="pdca",
        query=StoreQuery(entity=QueryEntity.LESSONS, limit=200),
    )
    factor_envelope = service.postgres_query(
        agent_name="pdca",
        query=StoreQuery(entity=QueryEntity.FACTOR_SNAPSHOTS, limit=200),
    )
    proposal_envelope = service.postgres_query(
        agent_name="pdca",
        query=StoreQuery(entity=QueryEntity.PROPOSALS, limit=200),
    )
    groups: dict[tuple[str, ...], list[dict[str, object]]] = defaultdict(list)
    attempted: set[str] = set()
    for row in _rows(proposal_envelope):
        document = row.get("document")
        if isinstance(document, dict):
            values = document.get("attempted_config_hashes", [])
            if isinstance(values, list):
                attempted.update(str(value) for value in values)
        digest = row.get("content_sha256")
        if isinstance(digest, str):
            attempted.add(digest)

    for row in _rows(lesson_envelope):
        document = row.get("document")
        if not isinstance(document, dict):
            continue
        profile = document.get("factor_profile")
        if not isinstance(profile, list) or not profile:
            continue
        groups[tuple(sorted(str(value) for value in profile))].append(row)

    metric_index: dict[str, Fact] = {}
    eligible: dict[str, frozenset[str]] = {}
    clusters: list[dict[str, object]] = []
    for profile, rows in sorted(groups.items()):
        sessions = {
            str(row.get("trade_date"))
            for row in rows
            if row.get("trade_date")
        }
        if len(rows) < MIN_CLUSTER_OBSERVATIONS or len(sessions) < MIN_CLUSTER_SESSIONS:
            continue
        cluster_id = f"cluster-{hashlib.sha256('|'.join(profile).encode()).hexdigest()[:16]}"
        lesson_ids = frozenset(str(row["record_id"]) for row in rows)
        eligible[cluster_id] = lesson_ids
        count_ref = f"{cluster_id}:observation_count"
        count_fact = Fact(
            name=count_ref,
            value=len(rows),
            availability=Availability.AVAILABLE,
            provenance=(
                "agent_gateway.store.lessons|exact_factor_profile_cluster.v1|"
                f"eligibility:{MIN_CLUSTER_PROVENANCE}"
            ),
        )
        metric_index[count_ref] = count_fact
        session_count_ref = f"{cluster_id}:independent_session_count"
        metric_index[session_count_ref] = Fact(
            name=session_count_ref,
            value=len(sessions),
            availability=Availability.AVAILABLE,
            provenance=(
                "agent_gateway.store.lessons|independent_session_cluster.v1|"
                f"eligibility:{MIN_CLUSTER_PROVENANCE}"
            ),
        )
        lesson_payload: list[dict[str, object]] = []
        for row in rows:
            lesson_id = str(row["record_id"])
            document = cast(dict[str, object], row["document"])
            metrics = document.get("metrics", [])
            metric_refs: list[str] = []
            if isinstance(metrics, list):
                for value in metrics:
                    fact = Fact.model_validate(value)
                    reference = f"{lesson_id}:{fact.name}"
                    metric_index[reference] = fact
                    metric_refs.append(reference)
            lesson_payload.append(
                {
                    "lesson_id": lesson_id,
                    "category": document.get("category"),
                    "hypothesis": document.get("hypothesis"),
                    "observation": document.get("observation"),
                    "conclusion": document.get("conclusion"),
                    "metric_refs": metric_refs,
                }
            )
        clusters.append(
            {
                "cluster_id": cluster_id,
                "factor_profile": profile,
                "observation_count_ref": count_ref,
                "independent_session_count_ref": session_count_ref,
                "lessons": lesson_payload,
            }
        )
    factor_rows = _rows(factor_envelope)
    for index, row in enumerate(factor_rows):
        facts = row.get("facts")
        if not isinstance(facts, list):
            continue
        case_id = str(row.get("case_id", f"factor-row-{index}"))
        for value in facts:
            fact = Fact.model_validate(value)
            reference = f"{case_id}:{fact.name}"
            metric_index[reference] = fact
            if isinstance(value, dict):
                value["fact_ref"] = reference
    threshold_ref = "monthly_gate:minimum_cluster_observations"
    metric_index[threshold_ref] = Fact(
        name=threshold_ref,
        value=MIN_CLUSTER_OBSERVATIONS,
        availability=Availability.AVAILABLE,
        provenance=MIN_CLUSTER_PROVENANCE,
    )
    package = {
        "eligible_clusters": clusters,
        "factor_health": factor_rows,
        "factor_health_availability": factor_envelope.get("availability"),
        "minimum_cluster_observations_ref": threshold_ref,
        "attempted_config_hashes": sorted(attempted),
    }
    return (
        json.dumps(package, ensure_ascii=False, sort_keys=True, default=str),
        eligible,
        metric_index,
        tuple(sorted(attempted)),
    )


def run(argv: list[str] | None = None) -> int:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser()
    parser.add_argument("--asof-date", type=_parse_date, required=True)
    parser.add_argument("--data-root", type=Path, default=ROOT / "data")
    parser.add_argument(
        "--llm-mode", type=LlmMode, choices=tuple(LlmMode), default=LlmMode.OPTIONAL
    )
    args = parser.parse_args(argv)
    service = AgentGatewayService(project_root=ROOT, data_root=args.data_root)
    package, clusters, metric_index, attempted_hashes = _build_package(service)
    result: dict[str, object] = {
        "asof_date": args.asof_date.isoformat(),
        "proposal_ids": [],
        "eligible_cluster_count": len(clusters),
        "orders_submitted": 0,
        "production_changes": 0,
    }
    parsed_package = json.loads(package)
    factor_health = parsed_package.get("factor_health", [])
    if not clusters and not factor_health:
        result["status"] = "insufficient_evidence"
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.llm_mode is LlmMode.OFF:
        result["status"] = "llm_off"
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    prompt, prompt_hash = monthly_proposal_prompt(package)
    request_audit = {
        "prompt_sha256": prompt_hash,
        "fact_package_sha256": hashlib.sha256(package.encode()).hexdigest(),
        "eligible_cluster_ids": tuple(sorted(clusters)),
    }
    try:
        response = DeepSeekClient.from_env().complete_json(prompt, max_tokens=4096)
        service.store.record_audit(
            actor=AgentRole.PDCA,
            tool="deepseek_monthly_evolution",
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
        review = parse_monthly_review(response.content)
        proposals = materialize_proposals(
            review,
            proposal_month=date(args.asof_date.year, args.asof_date.month, 1),
            eligible_clusters=clusters,
            metric_index=metric_index,
            attempted_config_hashes=attempted_hashes,
        )
        proposal_ids: list[str] = []
        for proposal in proposals:
            stored = service.proposal_write(agent_name="pdca", proposal=proposal)
            proposal_ids.append(str(cast(dict[str, object], stored["data"])["record_id"]))
        result["status"] = "complete"
        result["proposal_ids"] = proposal_ids
    except Exception as exc:
        service.store.record_audit(
            actor=AgentRole.PDCA,
            tool="deepseek_monthly_evolution",
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
