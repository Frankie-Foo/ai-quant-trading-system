from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from data_plane.providers.alpaca_direct import AlpacaNewsArticle
from execution.alpaca_paper import PaperAccount
from execution.alpaca_sip_stream import SipQuote
from execution.autonomous_paper_session import AutonomousPaperPlan
from kernel.intraday_policy import DecisionMetric, EntryRoute
from operations.autonomous_paper_config import AutonomousPaperPlanBundle
from operations.autonomous_policy_adapter import (
    AutonomousPolicyEvidence,
    load_runtime_safety_envelope,
)
from operations.desktop_paper_safety import DesktopPaperSafetyRefresher
from operations.runtime_agent_safety import (
    RuntimeAgentRole,
    RuntimeAgentVerdict,
    load_runtime_agent_assessment,
)
from operations.runtime_news_agents import NewsAgentOutput
from research.catalyst_scoring import ModelScoreResponse

NOW = datetime(2026, 8, 3, 14, 0, tzinfo=UTC)


def _bundle(tmp_path: Path) -> AutonomousPaperPlanBundle:
    metric = DecisionMetric(
        value=80.0,
        asof_utc=NOW - timedelta(minutes=2),
        provenance="test.selection.v1",
    )
    return AutonomousPaperPlanBundle(
        plan=AutonomousPaperPlan(
            plan_id="auto-20260803-XYZ",
            symbol="XYZ",
            trade_date=date(2026, 8, 3),
            reference_price=Decimal("100"),
            hard_stop=Decimal("98"),
            max_notional_fraction=Decimal("0.10"),
            full_risk_fraction=Decimal("0.0035"),
            source_snapshot_ids=("selection-1",),
            provenance="test.selection.v1",
        ),
        evidence=AutonomousPolicyEvidence(
            route=EntryRoute.CATALYST,
            catalyst=metric,
            factor=metric,
            right_tail=metric,
            first_target_reward_r=2.5,
            weighted_expected_reward_r=3.0,
            reward_risk_provenance="test.reward-risk.v1",
            a_plus_plus_approved=False,
        ),
        safety_envelope_path=tmp_path / "safety" / "XYZ.json",
        benchmark_symbol="SPY",
        sector_symbol="SPY",
        market_context_provenance="test.market.v1",
    )


class _Market:
    def fetch_news(
        self,
        symbols: tuple[str, ...],
        *,
        start_utc: datetime,
        end_utc: datetime,
    ) -> tuple[AlpacaNewsArticle, ...]:
        del symbols, start_utc, end_utc
        return (
            AlpacaNewsArticle(
                article_id="101",
                headline="XYZ reports strong results",
                summary="No material negative development is supplied.",
                author="Massive",
                created_at_utc=NOW - timedelta(minutes=1),
                updated_at_utc=NOW - timedelta(minutes=1),
                url="https://example.com/101",
                symbols=("XYZ",),
                source="massive",
            ),
        )

    def fetch_quotes(
        self,
        symbols: tuple[str, ...],
        *,
        start_utc: datetime,
        end_utc: datetime,
    ) -> tuple[SipQuote, ...]:
        del symbols, start_utc, end_utc
        return (
            SipQuote(
                symbol="XYZ",
                ts_utc=NOW - timedelta(seconds=1),
                bid_price=100.0,
                bid_size=100,
                ask_price=100.01,
                ask_size=100,
                provenance="alpaca.sip.websocket@test",
            ),
        )


class _Broker:
    def get_account(self) -> PaperAccount:
        return PaperAccount(
            status="ACTIVE",
            account_blocked=False,
            trading_blocked=False,
            equity="100000",
            last_equity="100000",
            buying_power="100000",
        )


class _Push:
    def configured_channel_available(self) -> bool:
        return True


def test_desktop_safety_uses_separate_models_and_writes_current_evidence(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, str]] = []

    def complete(model: str, prompt: str) -> ModelScoreResponse:
        calls.append((model, prompt))
        return ModelScoreResponse(
            content=NewsAgentOutput(
                verdict=RuntimeAgentVerdict.CLEAR,
                negative_news_clear=True,
                material_negative=False,
                rationale="Supplied facts contain no material negative development.",
                cited_source_ids=("alpaca.news.massive:101",),
            ).model_dump_json(),
            response_model=model,
        )

    bundle = _bundle(tmp_path)
    summary = DesktopPaperSafetyRefresher(
        runs_root=tmp_path,
        environ={
            "OPENROUTER_RUNTIME_CATALYST_MODEL": "openai/gpt-5.6",
            "OPENROUTER_RUNTIME_RED_TEAM_MODEL": "anthropic/claude-sonnet-5",
        },
        market=_Market(),
        push=_Push(),
        complete_json=complete,
    ).refresh(
        bundles=(bundle,),
        broker=_Broker(),
        observed_at_utc=NOW,
    )

    agent_root = tmp_path / "runtime-agents" / "2026-08-03" / "XYZ"
    assert summary == {
        "plans": 1,
        "healthy_envelopes": 1,
        "input_errors": 0,
        "push_healthy": True,
    }
    assert {model for model, _ in calls} == {
        "openai/gpt-5.6",
        "anthropic/claude-sonnet-5",
    }
    assert load_runtime_agent_assessment(
        agent_root / f"{RuntimeAgentRole.CATALYST.value}.json"
    ).model_id == "openai/gpt-5.6"
    assert load_runtime_agent_assessment(
        agent_root / f"{RuntimeAgentRole.RED_TEAM.value}.json"
    ).model_id == "anthropic/claude-sonnet-5"
    assert load_runtime_safety_envelope(bundle.safety_envelope_path).agents_healthy
    completions = list((agent_root.parent / "model-completions").glob("*.json"))
    assert len(completions) == 2
