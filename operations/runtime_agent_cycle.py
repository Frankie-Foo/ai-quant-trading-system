"""One fail-closed runtime cycle for news agents, supervisor, and push health."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from data_plane.providers.alpaca_direct import AlpacaNewsArticle
from execution.alpaca_paper import PaperAccount
from execution.alpaca_sip_stream import SipQuote
from operations.autonomous_paper_config import AutonomousPaperPlanBundle
from operations.runtime_agent_safety import (
    PushHealthEvidence,
    RuntimeAgentAssessment,
    RuntimeAgentRole,
    load_push_health_evidence,
    load_runtime_agent_assessment,
    write_push_health_evidence,
    write_runtime_agent_assessment,
)
from operations.runtime_news_agents import (
    RuntimeNewsFactPackage,
    build_news_agent_prompt,
    build_supervisor_assessment,
    refresh_news_assessment,
    unhealthy_news_assessment,
)
from operations.runtime_safety_refresh import (
    refresh_runtime_safety_envelopes,
)
from research.catalyst_scoring import ModelScoreResponse


class RuntimeAgentMarketPort(Protocol):
    def fetch_news(
        self,
        symbols: tuple[str, ...],
        *,
        start_utc: datetime,
        end_utc: datetime,
    ) -> tuple[AlpacaNewsArticle, ...]: ...

    def fetch_quotes(
        self,
        symbols: tuple[str, ...],
        *,
        start_utc: datetime,
        end_utc: datetime,
    ) -> tuple[SipQuote, ...]: ...


class RuntimeAgentBrokerPort(Protocol):
    def get_account(self) -> PaperAccount: ...


class RuntimeAgentPushPort(Protocol):
    def configured_channel_available(self) -> bool: ...


@dataclass(frozen=True)
class RuntimeAgentCycleSummary:
    plans: int
    healthy_envelopes: int
    input_errors: int
    push_healthy: bool


def run_runtime_agent_cycle(
    *,
    bundles: tuple[AutonomousPaperPlanBundle, ...],
    agent_root: Path,
    push_health_path: Path,
    observed_at_utc: datetime,
    market: RuntimeAgentMarketPort,
    broker: RuntimeAgentBrokerPort,
    push: RuntimeAgentPushPort,
    model_id: str | Mapping[RuntimeAgentRole, str],
    completions: Mapping[
        RuntimeAgentRole,
        Callable[[str], ModelScoreResponse],
    ],
) -> RuntimeAgentCycleSummary:
    _require_utc(observed_at_utc)
    if not bundles:
        raise ValueError("runtime agent cycle requires plans")
    required_roles = {
        RuntimeAgentRole.CATALYST,
        RuntimeAgentRole.RED_TEAM,
    }
    if set(completions) != required_roles:
        raise ValueError("runtime agent cycle requires catalyst and red-team completions")
    model_ids = _news_model_ids(model_id)
    symbols = tuple(sorted(bundle.plan.symbol for bundle in bundles))
    news_start = observed_at_utc - timedelta(hours=24)
    error_count = 0
    try:
        articles = market.fetch_news(
            symbols,
            start_utc=news_start,
            end_utc=observed_at_utc,
        )
        news_healthy = True
        news_error_code = ""
    except Exception as exc:
        articles = ()
        news_healthy = False
        news_error_code = type(exc).__name__
        error_count += 1
    query_id = _snapshot_id(
        "alpaca-news-query",
        *(sorted(article.provenance for article in articles)),
        "healthy" if news_healthy else f"failed:{news_error_code}",
    )

    try:
        quotes = market.fetch_quotes(
            symbols,
            start_utc=observed_at_utc - timedelta(minutes=2),
            end_utc=observed_at_utc + timedelta(microseconds=1),
        )
        quote_error = False
    except Exception:
        quotes = ()
        quote_error = True
        error_count += 1
    latest_quotes = _latest_quotes(quotes)

    try:
        account = broker.get_account()
        broker_healthy = _account_healthy(account)
    except Exception:
        broker_healthy = False
        error_count += 1

    delivery_failure = _current_delivery_failure(
        push_health_path,
        observed_at_utc=observed_at_utc,
    )
    if delivery_failure is not None:
        push_healthy = False
    else:
        try:
            push_healthy = push.configured_channel_available()
        except Exception:
            push_healthy = False
            error_count += 1
        push_id = _snapshot_id(
            "livermore-channel-health",
            observed_at_utc,
            str(push_healthy).lower(),
        )
        write_push_health_evidence(
            push_health_path,
            PushHealthEvidence(
                generated_at_utc=observed_at_utc,
                expires_at_utc=observed_at_utc + timedelta(seconds=45),
                healthy=push_healthy,
                source_snapshot_id=push_id,
                provenance=(
                    "operations.runtime_agent_cycle.livermore_channel.v1"
                ),
            ),
        )

    for bundle in bundles:
        plan = bundle.plan
        day_root = agent_root / plan.trade_date.isoformat() / plan.symbol
        if news_healthy:
            package = RuntimeNewsFactPackage.build(
                symbol=plan.symbol,
                observed_at_utc=observed_at_utc,
                articles=articles,
                query_snapshot_id=query_id,
            )
            for role in sorted(required_roles, key=lambda item: item.value):
                output_path = day_root / f"{role.value}.json"
                previous = _previous_assessment(output_path)
                try:
                    assessment = refresh_news_assessment(
                        trade_date=plan.trade_date,
                        role=role,
                        package=package,
                        generated_at_utc=observed_at_utc,
                        model_id=model_ids[role],
                        complete_json=completions[role],
                        previous=previous,
                    )
                except Exception as exc:
                    _, prompt_hash = build_news_agent_prompt(role, package)
                    assessment = unhealthy_news_assessment(
                        trade_date=plan.trade_date,
                        symbol=plan.symbol,
                        role=role,
                        generated_at_utc=observed_at_utc,
                        model_id=model_ids[role],
                        prompt_sha256=prompt_hash,
                        source_snapshot_ids=package.source_snapshot_ids,
                        error_code=type(exc).__name__,
                    )
                    error_count += 1
                write_runtime_agent_assessment(output_path, assessment)
        else:
            failure_hash = hashlib.sha256(
                f"{plan.symbol}:{news_error_code}".encode()
            ).hexdigest()
            for role in required_roles:
                write_runtime_agent_assessment(
                    day_root / f"{role.value}.json",
                    unhealthy_news_assessment(
                        trade_date=plan.trade_date,
                        symbol=plan.symbol,
                        role=role,
                        generated_at_utc=observed_at_utc,
                        model_id=model_ids[role],
                        prompt_sha256=failure_hash,
                        source_snapshot_ids=(query_id,),
                        error_code=news_error_code,
                    ),
                )

        quote = latest_quotes.get(plan.symbol)
        quote_current = bool(
            not quote_error
            and quote is not None
            and 0
            <= (observed_at_utc - quote.ts_utc).total_seconds()
            <= 30
        )
        supervisor_sources = [query_id, "alpaca-paper-account-read"]
        if quote is not None:
            supervisor_sources.append(quote.provenance)
        supervisor = build_supervisor_assessment(
            trade_date=plan.trade_date,
            symbol=plan.symbol,
            generated_at_utc=observed_at_utc,
            checks={
                "broker_read": broker_healthy,
                "config_current": (
                    plan.trade_date
                    == observed_at_utc.astimezone(
                        ZoneInfo("America/New_York")
                    ).date()
                ),
                "market_data_current": quote_current,
                "news_retrieval": news_healthy,
            },
            source_snapshot_ids=tuple(supervisor_sources),
        )
        write_runtime_agent_assessment(
            day_root / f"{RuntimeAgentRole.SUPERVISOR.value}.json",
            supervisor,
        )

    summaries = refresh_runtime_safety_envelopes(
        bundles=bundles,
        agent_root=agent_root,
        push_health_path=push_health_path,
        observed_at_utc=observed_at_utc,
    )
    return RuntimeAgentCycleSummary(
        plans=len(summaries),
        healthy_envelopes=sum(item.agents_healthy for item in summaries),
        input_errors=error_count + sum(item.input_errors for item in summaries),
        push_healthy=push_healthy,
    )


def _previous_assessment(path: Path) -> RuntimeAgentAssessment | None:
    try:
        return load_runtime_agent_assessment(path)
    except ValueError:
        return None


def _current_delivery_failure(
    path: Path,
    *,
    observed_at_utc: datetime,
) -> PushHealthEvidence | None:
    try:
        evidence = load_push_health_evidence(path)
    except (OSError, ValueError):
        return None
    if (
        not evidence.healthy
        and evidence.provenance
        == "operations.autonomous_notifications.delivery.v1"
        and evidence.is_current(observed_at_utc)
    ):
        return evidence
    return None


def _latest_quotes(quotes: tuple[SipQuote, ...]) -> dict[str, SipQuote]:
    latest: dict[str, SipQuote] = {}
    for quote in quotes:
        current = latest.get(quote.symbol)
        if current is None or quote.ts_utc > current.ts_utc:
            latest[quote.symbol] = quote
    return latest


def _account_healthy(account: PaperAccount) -> bool:
    return bool(
        account.status.strip().upper() == "ACTIVE"
        and not account.account_blocked
        and not account.trading_blocked
    )


def _snapshot_id(prefix: str, *parts: object) -> str:
    material = "|".join(str(part) for part in parts)
    digest = hashlib.sha256(material.encode()).hexdigest()[:24]
    return f"{prefix}-{digest}"


def _news_model_ids(
    value: str | Mapping[RuntimeAgentRole, str],
) -> dict[RuntimeAgentRole, str]:
    roles = (RuntimeAgentRole.CATALYST, RuntimeAgentRole.RED_TEAM)
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            raise ValueError("runtime agent model ID is required")
        return {role: normalized for role in roles}
    result = {role: str(value.get(role, "")).strip() for role in roles}
    if not all(result.values()) or set(value) != set(roles):
        raise ValueError("runtime agent model IDs are incomplete")
    return result


def _require_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("runtime agent cycle timestamp must be UTC")
