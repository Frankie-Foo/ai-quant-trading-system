"""Desktop-only runtime safety refresh for the isolated IBKR Paper loop."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import polars as pl
from pydantic import SecretStr

from data_plane.providers.alpaca_direct import AlpacaNewsArticle
from data_plane.providers.catalyst_news import fetch_massive_news
from execution.alpaca_sip_stream import SipQuote
from execution.sip_store import SipEventStore
from operations.autonomous_paper_config import AutonomousPaperPlanBundle
from operations.livermore_push import LivermorePushClient
from operations.runtime_agent_cycle import (
    RuntimeAgentBrokerPort,
    RuntimeAgentCycleSummary,
    RuntimeAgentMarketPort,
    RuntimeAgentPushPort,
    run_runtime_agent_cycle,
)
from operations.runtime_agent_safety import RuntimeAgentRole
from research.catalyst_scoring import ModelScoreResponse
from research.providers.openrouter_runtime import OpenRouterRuntimeClient

_API_KEY = "OPENROUTER_RUNTIME_API_KEY"
_CATALYST_MODEL = "OPENROUTER_RUNTIME_CATALYST_MODEL"
_RED_TEAM_MODEL = "OPENROUTER_RUNTIME_RED_TEAM_MODEL"


class MassiveSipRuntimeMarket:
    """Use Desktop's existing Massive news and append-only SIP store."""

    def __init__(self, *, runs_root: Path) -> None:
        self._store = SipEventStore(runs_root / "sip-stream.sqlite3")

    def fetch_news(
        self,
        symbols: tuple[str, ...],
        *,
        start_utc: datetime,
        end_utc: datetime,
    ) -> tuple[AlpacaNewsArticle, ...]:
        articles: list[AlpacaNewsArticle] = []
        for symbol in symbols:
            frame = fetch_massive_news(
                start_utc,
                end_utc,
                ticker=symbol,
                pace_seconds=0,
            )
            articles.extend(_massive_articles(frame, symbol=symbol))
        return tuple(sorted(articles, key=lambda item: (item.article_id, item.symbols)))

    def fetch_quotes(
        self,
        symbols: tuple[str, ...],
        *,
        start_utc: datetime,
        end_utc: datetime,
    ) -> tuple[SipQuote, ...]:
        quotes: list[SipQuote] = []
        for symbol in symbols:
            quote = self._store.latest_quote(symbol)
            if quote is not None and start_utc <= quote.ts_utc < end_utc:
                quotes.append(quote)
        return tuple(quotes)


class _EnvironmentPushHealth:
    def __init__(self, environ: Mapping[str, str]) -> None:
        self._environ = environ

    def configured_channel_available(self) -> bool:
        app_id = str(self._environ.get("LIVERMORE_APP_ID", "")).strip()
        secret = str(self._environ.get("LIVERMORE_APP_SECRET", "")).strip()
        channel_id = str(self._environ.get("LIVERMORE_CHANNEL_ID", "")).strip()
        if not app_id or not secret or not channel_id:
            raise RuntimeError("paper_push_not_configured")
        client = LivermorePushClient(
            app_id=app_id,
            app_secret=SecretStr(secret),
            channel_id=channel_id,
        )
        try:
            return client.configured_channel_available()
        finally:
            client.close()


class DesktopPaperSafetyRefresher:
    """Refresh strict runtime evidence; failures are represented as unsafe evidence."""

    def __init__(
        self,
        *,
        runs_root: Path,
        environ: Mapping[str, str],
        market: RuntimeAgentMarketPort | None = None,
        push: RuntimeAgentPushPort | None = None,
        complete_json: Callable[[str, str], ModelScoreResponse] | None = None,
    ) -> None:
        self._runs_root = runs_root
        self._environ = environ
        self._market = market
        self._push = push
        self._complete_json = complete_json

    def refresh(
        self,
        *,
        bundles: tuple[AutonomousPaperPlanBundle, ...],
        broker: RuntimeAgentBrokerPort,
        observed_at_utc: datetime,
    ) -> dict[str, object]:
        catalyst_model = _required(self._environ, _CATALYST_MODEL)
        red_team_model = _required(self._environ, _RED_TEAM_MODEL)
        completion = self._completion(observed_at_utc)
        summary = run_runtime_agent_cycle(
            bundles=bundles,
            agent_root=self._runs_root / "runtime-agents",
            push_health_path=self._runs_root / "runtime-agents" / "push-health.json",
            observed_at_utc=observed_at_utc,
            market=self._market or MassiveSipRuntimeMarket(runs_root=self._runs_root),
            broker=broker,
            push=self._push or _EnvironmentPushHealth(self._environ),
            model_id={
                RuntimeAgentRole.CATALYST: catalyst_model,
                RuntimeAgentRole.RED_TEAM: red_team_model,
            },
            completions={
                RuntimeAgentRole.CATALYST: lambda prompt: completion(
                    RuntimeAgentRole.CATALYST,
                    catalyst_model,
                    prompt,
                ),
                RuntimeAgentRole.RED_TEAM: lambda prompt: completion(
                    RuntimeAgentRole.RED_TEAM,
                    red_team_model,
                    prompt,
                ),
            },
        )
        return _summary_payload(summary)

    def _completion(
        self,
        observed_at_utc: datetime,
    ) -> Callable[[RuntimeAgentRole, str, str], ModelScoreResponse]:
        if self._complete_json is not None:
            complete = self._complete_json
        else:
            api_key = _required(self._environ, _API_KEY)
            client = OpenRouterRuntimeClient(api_key=api_key)

            def complete(model_id: str, prompt: str) -> ModelScoreResponse:
                return client.complete_json(prompt, model_id=model_id)

        def tracked(
            role: RuntimeAgentRole,
            model_id: str,
            prompt: str,
        ) -> ModelScoreResponse:
            response = complete(model_id, prompt)
            _write_model_completion(
                root=self._runs_root,
                observed_at_utc=observed_at_utc,
                role=role,
                model_id=model_id,
                prompt=prompt,
                response=response,
            )
            return response

        return tracked


def _massive_articles(frame: pl.DataFrame, *, symbol: str) -> tuple[AlpacaNewsArticle, ...]:
    required = {
        "source_event_id",
        "published_utc",
        "updated_utc",
        "symbols",
        "headline",
        "summary",
        "publisher",
        "url",
    }
    if required - set(frame.columns):
        raise ValueError("Massive runtime news schema is incomplete")
    articles: list[AlpacaNewsArticle] = []
    for row in frame.to_dicts():
        raw_symbols = row["symbols"]
        symbol_values = raw_symbols if isinstance(raw_symbols, list) else []
        symbols = tuple(
            value.strip().upper()
            for value in symbol_values
            if isinstance(value, str) and value.strip()
        )
        published = row["published_utc"]
        updated = row["updated_utc"] or published
        event_id = str(row["source_event_id"] or "").strip()
        headline = str(row["headline"] or "").strip()
        if (
            symbol not in symbols
            or not event_id
            or not headline
            or not isinstance(published, datetime)
            or not isinstance(updated, datetime)
        ):
            continue
        articles.append(
            AlpacaNewsArticle(
                article_id=event_id,
                headline=headline,
                summary=str(row["summary"] or "").strip(),
                author=str(row["publisher"] or "Massive").strip() or "Massive",
                created_at_utc=published.astimezone(UTC),
                updated_at_utc=updated.astimezone(UTC),
                url=str(row["url"] or "https://massive.com"),
                symbols=symbols,
                source="massive",
            )
        )
    return tuple(articles)


def _required(environ: Mapping[str, str], name: str) -> str:
    value = str(environ.get(name, "")).strip()
    if not value:
        raise RuntimeError("paper_runtime_safety_not_configured")
    return value


def _summary_payload(summary: RuntimeAgentCycleSummary) -> dict[str, object]:
    return asdict(summary)


def _write_model_completion(
    *,
    root: Path,
    observed_at_utc: datetime,
    role: RuntimeAgentRole,
    model_id: str,
    prompt: str,
    response: ModelScoreResponse,
) -> None:
    prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    path = (
        root
        / "runtime-agents"
        / observed_at_utc.date().isoformat()
        / "model-completions"
        / f"{role.value}-{prompt_hash}.json"
    )
    payload = {
        "schema_version": "desktop_runtime_model_completion.v1",
        "observed_at_utc": observed_at_utc.isoformat(),
        "role": role.value,
        "model_id": model_id,
        "prompt_sha256": prompt_hash,
        "response_model": response.response_model,
        "provider_request_id": response.provider_request_id,
        "system_fingerprint": response.system_fingerprint,
        "prompt_tokens": response.prompt_tokens,
        "completion_tokens": response.completion_tokens,
        "response_json": response.content,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
