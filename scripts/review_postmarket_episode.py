"""Persist deterministic postmarket diagnostics with optional LLM enrichment."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import polars as pl
from dotenv import load_dotenv

from data_plane.contracts import DataQualityCheck, DatasetSnapshot, QualitySeverity
from data_plane.storage import persist_snapshot
from research.evolution import evaluate_proposal
from research.postmortem_agents import (
    critic_prompt,
    history_fact_package,
    parse_critic_review,
    parse_research_review,
    research_prompt,
    validate_evidence_symbols,
)
from research.program_review import (
    PROGRAM_REVIEW_VERSION,
    ProgramReview,
    ProgramReviewStatus,
    ReviewPolicy,
    build_program_review,
)
from research.providers.deepseek import DEEPSEEK_MODEL, DeepSeekClient

ROOT = Path(__file__).resolve().parents[1]
REVIEW_SCHEMA_VERSION = "postmarket_program_review.v1"
DEFAULT_POLICY = ReviewPolicy(
    minimum_episodes=20,
    minimum_labeled_trades=20,
    minimum_net_labeled_trades=20,
)


class LlmMode(StrEnum):
    OFF = "off"
    OPTIONAL = "optional"
    REQUIRED = "required"


@dataclass(frozen=True)
class LlmEnrichment:
    status: str
    research_review_json: str | None = None
    critic_review_json: str | None = None
    research_prompt_sha256: str | None = None
    critic_prompt_sha256: str | None = None
    research_request_id: str | None = None
    critic_request_id: str | None = None
    research_response_model: str | None = None
    critic_response_model: str | None = None
    research_system_fingerprint: str | None = None
    critic_system_fingerprint: str | None = None
    research_prompt_tokens: int | None = None
    research_completion_tokens: int | None = None
    critic_prompt_tokens: int | None = None
    critic_completion_tokens: int | None = None
    critic_verdict: str | None = None
    evolution_status: str | None = None
    error_code: str | None = None


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


def _latest_episode_paths(
    data_root: Path,
) -> dict[date, tuple[DatasetSnapshot, Path]]:
    latest: dict[date, tuple[DatasetSnapshot, Path]] = {}
    for path in (data_root / "accepted").glob(
        "research.trading_episodes-*/data.parquet"
    ):
        values = (
            pl.read_parquet(path, columns=["session_date"])
            .get_column("session_date")
            .unique()
            .to_list()
        )
        if len(values) != 1 or not isinstance(values[0], date):
            continue
        snapshot = DatasetSnapshot.model_validate(_manifest(path.parent / "manifest.json"))
        current = latest.get(values[0])
        if current is None or snapshot.asof_utc > current[0].asof_utc:
            latest[values[0]] = (snapshot, path)
    return latest


def _load_episodes(
    data_root: Path, trade_date: date
) -> tuple[pl.DataFrame, DatasetSnapshot, pl.DataFrame, tuple[str, ...]]:
    latest = _latest_episode_paths(data_root)
    target = latest.get(trade_date)
    if target is None:
        raise FileNotFoundError(f"no accepted trading episode for {trade_date}")
    target_snapshot, target_path = target
    ordered = sorted(
        (session_date, value)
        for session_date, value in latest.items()
        if session_date <= trade_date
    )
    frames = [pl.read_parquet(path) for _, (_, path) in ordered]
    history = pl.concat(frames, how="diagonal_relaxed")
    history_ids = tuple(snapshot.dataset_id for _, (snapshot, _) in ordered)
    return pl.read_parquet(target_path), target_snapshot, history, history_ids


def _llm_enrichment(
    *,
    mode: LlmMode,
    program_review: ProgramReview,
    history: pl.DataFrame,
    history_ids: tuple[str, ...],
) -> LlmEnrichment:
    if mode is LlmMode.OFF:
        return LlmEnrichment(status="off")
    if not program_review.llm_research_allowed:
        return LlmEnrichment(status="skipped_by_program_gate")
    try:
        facts = history_fact_package(
            history,
            dataset_ids=history_ids,
            program_review_json=program_review.model_dump_json(),
        )
        research_text, research_hash = research_prompt(facts)
        client = DeepSeekClient.from_env()
        research_response = client.complete_json(research_text, max_tokens=2048)
        review = parse_research_review(research_response.content)
        fact_payload = json.loads(facts)
        fact_symbols = {
            str(row["symbol"])
            for row in fact_payload["rows"]
            if isinstance(row, dict) and "symbol" in row
        }
        validate_evidence_symbols(review, fact_symbols)
        critic_text, critic_hash = critic_prompt(facts, review)
        critic_response = client.complete_json(critic_text, max_tokens=2048)
        critique = parse_critic_review(critic_response.content)
        decision = evaluate_proposal(
            review,
            critique,
            episode_count=program_review.metrics.episode_count,
            labeled_trade_count=program_review.metrics.labeled_trade_count,
            min_episodes=program_review.policy.minimum_episodes,
            min_labeled_trades=program_review.policy.minimum_labeled_trades,
        )
        return LlmEnrichment(
            status="complete_two_agent_research",
            research_review_json=review.model_dump_json(),
            critic_review_json=critique.model_dump_json(),
            research_prompt_sha256=research_hash,
            critic_prompt_sha256=critic_hash,
            research_request_id=research_response.provider_request_id,
            critic_request_id=critic_response.provider_request_id,
            research_response_model=research_response.response_model,
            critic_response_model=critic_response.response_model,
            research_system_fingerprint=research_response.system_fingerprint,
            critic_system_fingerprint=critic_response.system_fingerprint,
            research_prompt_tokens=research_response.prompt_tokens,
            research_completion_tokens=research_response.completion_tokens,
            critic_prompt_tokens=critic_response.prompt_tokens,
            critic_completion_tokens=critic_response.completion_tokens,
            critic_verdict=critique.verdict.value,
            evolution_status=decision.status.value,
        )
    except Exception as exc:
        if mode is LlmMode.REQUIRED:
            raise
        return LlmEnrichment(status="failed_optional", error_code=type(exc).__name__)


def _check(
    name: str, passed: bool, observed: object, expected: str
) -> DataQualityCheck:
    return DataQualityCheck(
        name=name,
        severity=QualitySeverity.CRITICAL,
        passed=passed,
        observed=str(observed),
        expected=expected,
        provenance=f"research.program_review:{PROGRAM_REVIEW_VERSION}",
    )


def main() -> None:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser()
    parser.add_argument("--trade-date", type=_parse_date, required=True)
    parser.add_argument("--data-root", type=Path, default=ROOT / "data")
    parser.add_argument(
        "--llm-mode",
        type=LlmMode,
        choices=tuple(LlmMode),
        default=LlmMode.OPTIONAL,
        help="optional is the production default; agents run only after program gates",
    )
    args = parser.parse_args()

    episode, episode_snapshot, history, history_ids = _load_episodes(
        args.data_root, args.trade_date
    )
    program_review = build_program_review(
        current_episode=episode,
        episode_history=history,
        policy=DEFAULT_POLICY,
    )
    enrichment = _llm_enrichment(
        mode=args.llm_mode,
        program_review=program_review,
        history=history,
        history_ids=history_ids,
    )
    metrics = program_review.metrics
    row = {
        "session_date": args.trade_date,
        "episode_dataset_id": episode_snapshot.dataset_id,
        "history_episode_dataset_ids": list(history_ids),
        "review_engine_version": PROGRAM_REVIEW_VERSION,
        "program_review_sha256": program_review.content_sha256(),
        "program_review_json": program_review.model_dump_json(),
        "program_status": program_review.status.value,
        "episode_count": metrics.episode_count,
        "labeled_trade_count": metrics.labeled_trade_count,
        "net_labeled_trade_count": metrics.net_labeled_trade_count,
        "censored_trigger_count": metrics.censored_trigger_count,
        "finding_count": len(program_review.findings),
        "sandbox_experiment_count": len(program_review.sandbox_experiments),
        "llm_mode": args.llm_mode.value,
        "llm_status": enrichment.status,
        "llm_model_id": (
            DEEPSEEK_MODEL if enrichment.research_review_json is not None else None
        ),
        "research_review_json": enrichment.research_review_json,
        "critic_review_json": enrichment.critic_review_json,
        "research_prompt_sha256": enrichment.research_prompt_sha256,
        "critic_prompt_sha256": enrichment.critic_prompt_sha256,
        "research_request_id": enrichment.research_request_id,
        "critic_request_id": enrichment.critic_request_id,
        "research_response_model": enrichment.research_response_model,
        "critic_response_model": enrichment.critic_response_model,
        "research_system_fingerprint": enrichment.research_system_fingerprint,
        "critic_system_fingerprint": enrichment.critic_system_fingerprint,
        "research_prompt_tokens": enrichment.research_prompt_tokens,
        "research_completion_tokens": enrichment.research_completion_tokens,
        "critic_prompt_tokens": enrichment.critic_prompt_tokens,
        "critic_completion_tokens": enrichment.critic_completion_tokens,
        "critic_verdict": enrichment.critic_verdict,
        "agent_evolution_status": enrichment.evolution_status,
        "llm_error_code": enrichment.error_code,
        "approved_for_production": False,
        "review_asof_utc": datetime.now(UTC),
        "provenance": f"research.program_review:{PROGRAM_REVIEW_VERSION}",
    }
    frame = pl.DataFrame([row])
    eligible = (
        program_review.status is ProgramReviewStatus.ELIGIBLE_FOR_SANDBOX_EXPERIMENT
    )
    agent_complete = enrichment.status == "complete_two_agent_research"
    checks = (
        _check(
            "program_cannot_promote_production",
            program_review.approved_for_production is False,
            program_review.approved_for_production,
            "approved_for_production is always false",
        ),
        _check(
            "llm_called_only_after_program_gate",
            enrichment.research_review_json is None
            or program_review.llm_research_allowed,
            enrichment.status,
            "LLM enrichment is off/skipped until deterministic gates pass",
        ),
        _check(
            "net_costs_required_for_sandbox",
            not eligible
            or metrics.net_labeled_trade_count
            >= DEFAULT_POLICY.minimum_net_labeled_trades,
            metrics.net_labeled_trade_count,
            f">={DEFAULT_POLICY.minimum_net_labeled_trades} net labels",
        ),
        _check(
            "complete_episode_lineage",
            len(history_ids) == metrics.episode_count,
            len(history_ids),
            f"{metrics.episode_count} latest Episode parent IDs",
        ),
        _check(
            "frozen_agent_response_models",
            not agent_complete
            or {
                enrichment.research_response_model,
                enrichment.critic_response_model,
            }
            == {DEEPSEEK_MODEL},
            (
                enrichment.research_response_model,
                enrichment.critic_response_model,
            ),
            f"both agent responses use {DEEPSEEK_MODEL} when called",
        ),
        _check(
            "agent_fingerprints_recorded",
            not agent_complete
            or (
                enrichment.research_system_fingerprint is not None
                and enrichment.critic_system_fingerprint is not None
            ),
            agent_complete,
            "both response fingerprints are recorded when agents run",
        ),
        _check(
            "independent_agent_prompts",
            not agent_complete
            or enrichment.research_prompt_sha256
            != enrichment.critic_prompt_sha256,
            enrichment.research_prompt_sha256
            == enrichment.critic_prompt_sha256,
            "Research and Critic use distinct prompt hashes",
        ),
    )
    snapshot, path = persist_snapshot(
        frame,
        root=args.data_root,
        source="research.postmarket.program_review",
        schema_version=REVIEW_SCHEMA_VERSION,
        checks=checks,
        parent_snapshot_ids=history_ids,
    )
    snapshot.assert_usable()
    print(
        json.dumps(
            {
                "trade_date": args.trade_date.isoformat(),
                "status": program_review.status.value,
                "findings": len(program_review.findings),
                "sandbox_experiments": len(program_review.sandbox_experiments),
                "llm_status": enrichment.status,
                "approved_for_production": False,
                "dataset_id": snapshot.dataset_id,
                "path": str(path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
