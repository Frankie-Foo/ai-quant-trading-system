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
from operations.runtime_agent_cycle import run_runtime_agent_cycle
from operations.runtime_agent_safety import (
    PushHealthEvidence,
    RuntimeAgentRole,
    RuntimeAgentVerdict,
    load_runtime_agent_assessment,
    write_push_health_evidence,
)
from operations.runtime_news_agents import NewsAgentOutput
from research.catalyst_scoring import ModelScoreResponse

TRADE_DATE = date(2026, 7, 29)
NOW = datetime(2026, 7, 29, 14, 0, tzinfo=UTC)


def _bundle(tmp_path: Path) -> AutonomousPaperPlanBundle:
    metric = DecisionMetric(
        value=80.0,
        asof_utc=NOW - timedelta(minutes=10),
        provenance="accepted.metric.v1",
    )
    return AutonomousPaperPlanBundle(
        plan=AutonomousPaperPlan(
            plan_id="auto-20260729-XYZ",
            symbol="XYZ",
            trade_date=TRADE_DATE,
            reference_price=Decimal("100"),
            hard_stop=Decimal("98"),
            max_notional_fraction=Decimal("0.20"),
            full_risk_fraction=Decimal("0.0035"),
            source_snapshot_ids=("selection-1",),
            provenance="accepted.selection.v1",
        ),
        evidence=AutonomousPolicyEvidence(
            route=EntryRoute.CATALYST,
            catalyst=metric,
            factor=metric,
            right_tail=metric,
            first_target_reward_r=2.5,
            weighted_expected_reward_r=3.0,
            reward_risk_provenance="accepted.reward-risk.v1",
            a_plus_plus_approved=False,
        ),
        safety_envelope_path=tmp_path / "safety" / "XYZ.json",
        benchmark_symbol="SPY",
        sector_symbol="XLK",
        market_context_provenance="accepted.market-context.v1",
    )


class FakeMarket:
    def fetch_news(
        self,
        symbols: tuple[str, ...],
        *,
        start_utc: datetime,
        end_utc: datetime,
    ) -> tuple[AlpacaNewsArticle, ...]:
        return (
            AlpacaNewsArticle(
                article_id="101",
                headline="XYZ launches a product",
                summary="The company announced a product.",
                author="Desk",
                created_at_utc=NOW - timedelta(minutes=2),
                updated_at_utc=NOW - timedelta(minutes=1),
                url="https://example.com/101",
                symbols=("XYZ",),
                source="benzinga",
            ),
        )

    def fetch_quotes(
        self,
        symbols: tuple[str, ...],
        *,
        start_utc: datetime,
        end_utc: datetime,
    ) -> tuple[SipQuote, ...]:
        return (
            SipQuote(
                symbol="XYZ",
                ts_utc=NOW - timedelta(seconds=1),
                bid_price=100.0,
                bid_size=100,
                ask_price=100.02,
                ask_size=100,
                provenance="alpaca.sip.rest.quotes@test",
            ),
        )


class FakeBroker:
    def get_account(self) -> PaperAccount:
        return PaperAccount(
            status="ACTIVE",
            account_blocked=False,
            trading_blocked=False,
            equity="100000",
            last_equity="100000",
            buying_power="400000",
        )


class FakePush:
    def configured_channel_available(self) -> bool:
        return True


def _complete(prompt: str) -> ModelScoreResponse:
    return ModelScoreResponse(
        content=NewsAgentOutput(
            verdict=RuntimeAgentVerdict.CLEAR,
            negative_news_clear=True,
            material_negative=False,
            rationale="No material negative fact appears in supplied evidence.",
            cited_source_ids=("alpaca.news.benzinga:101",),
        ).model_dump_json(),
        response_model="deepseek-v4-pro",
    )


def test_runtime_agent_cycle_writes_three_agents_push_and_tradeable_envelope(
    tmp_path: Path,
) -> None:
    agent_root = tmp_path / "agents"

    summary = run_runtime_agent_cycle(
        bundles=(_bundle(tmp_path),),
        agent_root=agent_root,
        push_health_path=agent_root / "push-health.json",
        observed_at_utc=NOW,
        market=FakeMarket(),
        broker=FakeBroker(),
        push=FakePush(),
        model_id="deepseek-v4-pro",
        completions={
            RuntimeAgentRole.CATALYST: _complete,
            RuntimeAgentRole.RED_TEAM: _complete,
        },
    )

    day_root = agent_root / TRADE_DATE.isoformat() / "XYZ"
    assert summary.healthy_envelopes == 1
    assert load_runtime_agent_assessment(
        day_root / "supervisor.json"
    ).healthy is True
    envelope = load_runtime_safety_envelope(
        tmp_path / "safety" / "XYZ.json"
    )
    assert envelope.agents_healthy is True
    assert envelope.negative_news_clear is True
    assert envelope.push_healthy is True


def test_runtime_agent_cycle_preserves_current_delivery_failure_latch(
    tmp_path: Path,
) -> None:
    agent_root = tmp_path / "agents"
    push_health_path = agent_root / "push-health.json"
    write_push_health_evidence(
        push_health_path,
        PushHealthEvidence(
            generated_at_utc=NOW - timedelta(seconds=5),
            expires_at_utc=NOW + timedelta(minutes=4),
            healthy=False,
            source_snapshot_id="livermore-delivery-failure-test",
            provenance="operations.autonomous_notifications.delivery.v1",
        ),
    )

    summary = run_runtime_agent_cycle(
        bundles=(_bundle(tmp_path),),
        agent_root=agent_root,
        push_health_path=push_health_path,
        observed_at_utc=NOW,
        market=FakeMarket(),
        broker=FakeBroker(),
        push=FakePush(),
        model_id="deepseek-v4-pro",
        completions={
            RuntimeAgentRole.CATALYST: _complete,
            RuntimeAgentRole.RED_TEAM: _complete,
        },
    )

    envelope = load_runtime_safety_envelope(
        tmp_path / "safety" / "XYZ.json"
    )
    assert summary.push_healthy is False
    assert envelope.push_healthy is False
