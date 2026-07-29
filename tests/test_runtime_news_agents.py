from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from pydantic import ValidationError

from data_plane.providers.alpaca_direct import AlpacaNewsArticle
from operations.runtime_agent_safety import (
    RuntimeAgentRole,
    RuntimeAgentVerdict,
)
from operations.runtime_news_agents import (
    NewsAgentOutput,
    RuntimeNewsFactPackage,
    build_news_agent_prompt,
    build_supervisor_assessment,
    materialize_news_assessment,
    refresh_news_assessment,
)
from research.catalyst_scoring import ModelScoreResponse

TRADE_DATE = date(2026, 7, 29)
NOW = datetime(2026, 7, 29, 14, 0, tzinfo=UTC)


def _article() -> AlpacaNewsArticle:
    return AlpacaNewsArticle(
        article_id="101",
        headline="XYZ announces a product launch",
        summary="The company announced a new product.",
        author="Desk",
        created_at_utc=NOW - timedelta(minutes=2),
        updated_at_utc=NOW - timedelta(minutes=1),
        url="https://example.com/101",
        symbols=("XYZ",),
        source="benzinga",
    )


def test_runtime_news_prompt_is_bounded_distinct_and_content_addressed() -> None:
    package = RuntimeNewsFactPackage.build(
        symbol="XYZ",
        observed_at_utc=NOW,
        articles=(_article(),),
        query_snapshot_id="alpaca-news-query-1",
    )

    catalyst_prompt, catalyst_hash = build_news_agent_prompt(
        RuntimeAgentRole.CATALYST,
        package,
    )
    red_prompt, red_hash = build_news_agent_prompt(
        RuntimeAgentRole.RED_TEAM,
        package,
    )

    assert "untrusted quoted data" in catalyst_prompt
    assert catalyst_hash != red_hash
    assert "alpaca.news.benzinga:101" in catalyst_prompt
    assert package.source_snapshot_ids == (
        "alpaca-news-query-1",
        "alpaca.news.benzinga:101",
    )


def test_news_agent_output_cannot_claim_clear_or_block_inconsistently() -> None:
    with pytest.raises(ValidationError):
        NewsAgentOutput(
            verdict=RuntimeAgentVerdict.CLEAR,
            negative_news_clear=False,
            material_negative=False,
            rationale="The conclusion is deliberately inconsistent.",
            cited_source_ids=(),
        )
    with pytest.raises(ValidationError):
        NewsAgentOutput(
            verdict=RuntimeAgentVerdict.BLOCK,
            negative_news_clear=False,
            material_negative=True,
            rationale="The block has no cited source and must fail.",
            cited_source_ids=(),
        )


def test_materialized_news_assessment_rejects_unknown_citations() -> None:
    package = RuntimeNewsFactPackage.build(
        symbol="XYZ",
        observed_at_utc=NOW,
        articles=(_article(),),
        query_snapshot_id="alpaca-news-query-1",
    )
    _, prompt_hash = build_news_agent_prompt(
        RuntimeAgentRole.RED_TEAM,
        package,
    )
    output = NewsAgentOutput(
        verdict=RuntimeAgentVerdict.BLOCK,
        negative_news_clear=False,
        material_negative=True,
        rationale="A material negative item is present in the supplied evidence.",
        cited_source_ids=("unknown-source",),
    )

    with pytest.raises(ValueError, match="outside"):
        materialize_news_assessment(
            trade_date=TRADE_DATE,
            role=RuntimeAgentRole.RED_TEAM,
            package=package,
            output=output,
            model_id="deepseek-v4-pro",
            prompt_sha256=prompt_hash,
            generated_at_utc=NOW,
        )


def test_deterministic_supervisor_fails_closed_on_any_health_failure() -> None:
    clear = build_supervisor_assessment(
        trade_date=TRADE_DATE,
        symbol="XYZ",
        generated_at_utc=NOW,
        checks={
            "broker_read": True,
            "config_current": True,
            "market_data_current": True,
        },
        source_snapshot_ids=("broker-account-1", "sip-quote-1"),
    )
    blocked = build_supervisor_assessment(
        trade_date=TRADE_DATE,
        symbol="XYZ",
        generated_at_utc=NOW,
        checks={
            "broker_read": True,
            "config_current": True,
            "market_data_current": False,
        },
        source_snapshot_ids=("broker-account-1", "sip-quote-1"),
    )

    assert clear.role is RuntimeAgentRole.SUPERVISOR
    assert clear.healthy is True
    assert clear.verdict is RuntimeAgentVerdict.CLEAR
    assert blocked.healthy is False
    assert blocked.verdict is RuntimeAgentVerdict.INSUFFICIENT


def test_unchanged_news_reuses_valid_classification_without_another_llm_call() -> None:
    package = RuntimeNewsFactPackage.build(
        symbol="XYZ",
        observed_at_utc=NOW,
        articles=(_article(),),
        query_snapshot_id="alpaca-news-query-1",
    )
    calls = 0

    def complete(prompt: str) -> ModelScoreResponse:
        nonlocal calls
        calls += 1
        return ModelScoreResponse(
            content=NewsAgentOutput(
                verdict=RuntimeAgentVerdict.CLEAR,
                negative_news_clear=True,
                material_negative=False,
                rationale="No material negative fact appears in supplied evidence.",
                cited_source_ids=("alpaca.news.benzinga:101",),
            ).model_dump_json(),
            provider_request_id="request-1",
            response_model="deepseek-v4-pro",
            system_fingerprint="fingerprint-1",
        )

    first = refresh_news_assessment(
        trade_date=TRADE_DATE,
        role=RuntimeAgentRole.CATALYST,
        package=package,
        generated_at_utc=NOW,
        model_id="deepseek-v4-pro",
        complete_json=complete,
        previous=None,
    )
    renewed_package = RuntimeNewsFactPackage.build(
        symbol="XYZ",
        observed_at_utc=NOW + timedelta(seconds=15),
        articles=(_article(),),
        query_snapshot_id="alpaca-news-query-2",
    )
    second = refresh_news_assessment(
        trade_date=TRADE_DATE,
        role=RuntimeAgentRole.CATALYST,
        package=renewed_package,
        generated_at_utc=NOW + timedelta(seconds=15),
        model_id="deepseek-v4-pro",
        complete_json=complete,
        previous=first,
    )

    assert calls == 1
    assert second.generated_at_utc == NOW + timedelta(seconds=15)
    assert second.source_snapshot_ids[0] == "alpaca-news-query-2"
    assert second.verdict is RuntimeAgentVerdict.CLEAR
    assert "cached_classification=true" in second.provenance
