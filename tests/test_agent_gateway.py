from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast

import polars as pl
import pytest
from mcp.client.stdio import stdio_client
from pydantic import ValidationError

from agent_gateway.contracts import (
    AgentRole,
    AuditReport,
    Availability,
    EvolutionProposal,
    Fact,
    Lesson,
    LessonCategory,
    QueryEntity,
    StoreQuery,
    Thesis,
    ThesisStage,
    ThesisStance,
)
from agent_gateway.policy import AuthorizationError
from agent_gateway.service import AgentGatewayService
from agent_gateway.store import SQLiteAgentFactStore
from data_plane.contracts import DataQualityCheck, QualitySeverity
from data_plane.storage import persist_snapshot
from mcp import ClientSession, StdioServerParameters
from research.monthly_evolution_agents import (
    MonthlyProposalReview,
    ProposalDraft,
    materialize_proposals,
)
from research.pdca_agents import LessonDraft, PostmarketLessonReview, materialize_lessons
from scripts.run_monthly_evolution import _build_package
from scripts.run_structured_pdca import _pdca_fact_package

FUTURE_SESSION = date(2030, 7, 1)


def _snapshot_frame() -> pl.DataFrame:
    asof = datetime(2030, 7, 1, 11, 30, tzinfo=UTC)
    return pl.DataFrame(
        {
            "symbol": ["ABCD"],
            "session_date": [FUTURE_SESSION],
            "catalyst_categories": [["contract_partnership"]],
            "pass_gate": [True],
            "reject_reason": [""],
            "selection_rank": [1],
            "rvol": [4.2],
            "rvol_provenance": ["synthetic.rvol"],
            "price": [20.0],
            "price_provenance": ["synthetic.price"],
            "adv_usd": [20_000_000.0],
            "adv_usd_provenance": ["synthetic.adv"],
            "beta": [1.8],
            "beta_provenance": ["synthetic.beta"],
            "atr_pct": [0.05],
            "atr_pct_provenance": ["synthetic.atr_pct"],
            "market_cap": [3_000_000_000.0],
            "market_cap_provenance": ["synthetic.market_cap"],
            "free_float": [50_000_000.0],
            "free_float_provenance": ["synthetic.free_float"],
            "tier": ["mid"],
            "model_score": [None],
            "model_provenance": [None],
            "gate_asof_utc": [asof],
        },
        schema_overrides={
            "session_date": pl.Date,
            "gate_asof_utc": pl.Datetime("us", "UTC"),
            "model_score": pl.Float64,
            "model_provenance": pl.String,
        },
    )


@pytest.fixture
def synthetic_project(tmp_path: Path) -> Path:
    source_root = Path(__file__).resolve().parents[1]
    shutil.copyfile(source_root / "config.yaml", tmp_path / "config.yaml")
    persist_snapshot(
        _snapshot_frame(),
        root=tmp_path / "data",
        source="kernel.universe.selection_gates",
        schema_version="selection_gates.synthetic.v1",
        checks=(
            DataQualityCheck(
                name="synthetic_fixture",
                severity=QualitySeverity.CRITICAL,
                passed=True,
                observed="valid",
                expected="valid",
                provenance="tests.test_agent_gateway",
            ),
        ),
    )
    return tmp_path


@pytest.fixture
def gateway(synthetic_project: Path) -> AgentGatewayService:
    store = SQLiteAgentFactStore(synthetic_project / "runs" / "agent-test.sqlite3")
    return AgentGatewayService(project_root=synthetic_project, store=store)


def _fact(name: str = "sample_metric") -> Fact:
    return Fact(
        name=name,
        value=1.0,
        availability=Availability.AVAILABLE,
        provenance="synthetic.fact",
    )


def _object_dict(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def test_fact_and_narrative_contracts_reject_unproven_numbers() -> None:
    with pytest.raises(ValidationError, match="N/A facts must use a null value"):
        Fact(
            name="bad",
            value=1.0,
            availability=Availability.UNAVAILABLE,
            provenance="synthetic.bad",
        )
    with pytest.raises(ValidationError, match="narrative decision numbers"):
        Thesis(
            agent=AgentRole.RISK,
            symbol="ABCD",
            trade_date=FUTURE_SESSION,
            stage=ThesisStage.PREMARKET,
            stance=ThesisStance.WATCH,
            facts=(_fact(),),
            inference="Observed return exceeded 5 percent in the sample.",
            falsification="Reject when the stated causal mechanism no longer holds.",
            source_snapshot_ids=("synthetic-selection",),
        )


def test_role_policy_and_unavailable_features_are_fail_closed(
    gateway: AgentGatewayService,
) -> None:
    result = gateway.features_order_flow(
        agent_name="order-flow", trade_date=FUTURE_SESSION, symbol="ABCD"
    )
    assert result["availability"] == "N/A"
    facts = _object_dict(result["data"])["facts"]
    assert isinstance(facts, list)
    assert all(_object_dict(fact)["availability"] == "N/A" for fact in facts)

    with pytest.raises(AuthorizationError):
        gateway.features_order_flow(
            agent_name="sentiment", trade_date=FUTURE_SESSION, symbol="ABCD"
        )
    audits = gateway.store.query(StoreQuery(entity=QueryEntity.TOOL_AUDIT, limit=10))
    assert len(audits) == 2
    assert {row["success"] for row in audits} == {0, 1}


def test_pdca_can_read_anonymized_intraday_selection_postmortem(
    gateway: AgentGatewayService,
    synthetic_project: Path,
) -> None:
    persist_snapshot(
        pl.DataFrame(
            {
                "session_date": [FUTURE_SESSION],
                "symbol": ["MISS"],
                "opportunity_rank": [1],
                "decision_outcome": ["missed_detectable_opportunity"],
                "root_cause": ["factor_gap"],
                "pattern_key": ["factor_gap:price_order_flow_or_sector"],
                "research_action": ["test_price_order_flow_or_sector_factor_in_sandbox"],
                "research_eligible": [True],
                "production_change_allowed": [False],
                "close_return": [0.08],
                "provenance": ["synthetic.postmortem"],
            }
        ),
        root=synthetic_project / "data",
        source="research.intraday_selection_postmortem",
        schema_version="intraday_selection_postmortem.v1",
        checks=(),
    )

    result = gateway.postgres_query(
        agent_name="pdca",
        query=StoreQuery(
            entity=QueryEntity.INTRADAY_SELECTION_POSTMORTEMS,
            trade_date=FUTURE_SESSION,
        ),
    )

    assert result["availability"] == "available"
    assert result["snapshot_ids"]
    rows = result["data"]
    assert isinstance(rows, list)
    assert len(rows) == 1
    row = _object_dict(rows[0])
    assert "symbol" not in row
    assert str(row["case_id"]).startswith("case-")
    assert row["root_cause"] == "factor_gap"
    facts = row["facts"]
    assert isinstance(facts, list)
    fact_by_name = {
        str(_object_dict(fact)["name"]): _object_dict(fact) for fact in facts
    }
    assert fact_by_name["production_change_allowed"]["value"] is False

    prior_session = date(2030, 6, 28)
    persist_snapshot(
        pl.DataFrame(
            {
                "session_date": [prior_session],
                "symbol": ["OTHER"],
                "opportunity_rank": [1],
                "decision_outcome": ["intentional_rejection"],
                "root_cause": ["intentional_gate"],
                "pattern_key": ["intentional_gate:rvol_below_or_equal_min"],
                "research_action": ["counterfactual_test_without_mutating_hard_guardrail"],
                "research_eligible": [True],
                "production_change_allowed": [False],
                "close_return": [0.06],
                "provenance": ["synthetic.postmortem"],
            }
        ),
        root=synthetic_project / "data",
        source="research.intraday_selection_postmortem",
        schema_version="intraday_selection_postmortem.v1",
        checks=(),
    )
    history = gateway.postgres_query(
        agent_name="pdca",
        query=StoreQuery(
            entity=QueryEntity.INTRADAY_SELECTION_POSTMORTEMS,
            limit=200,
        ),
    )
    history_rows = history["data"]
    history_snapshot_ids = cast(list[object], history["snapshot_ids"])
    assert isinstance(history_rows, list)
    assert len(history_rows) == 2
    assert len(history_snapshot_ids) == 2

    persist_snapshot(
        pl.DataFrame(
            {
                "session_date": [FUTURE_SESSION],
                "symbol": ["ABCD"],
                "signal_triggered": [True],
                "outcome_label": ["tp"],
                "gross_return": [0.03],
                "episode_provenance": ["synthetic.episode"],
            }
        ),
        root=synthetic_project / "data",
        source="research.trading_episodes",
        schema_version="trading_episode.v1",
        checks=(),
    )
    fact_package, metric_index, snapshot_ids = _pdca_fact_package(
        gateway,
        FUTURE_SESSION,
    )
    package = json.loads(fact_package)

    assert len(package["anonymous_cases"]) == 1
    assert len(package["missed_opportunities"]) == 1
    assert package["opportunity_availability"] == "available"
    assert any(key.startswith("opportunity:case-") for key in metric_index)
    assert len(snapshot_ids) == 2


def test_order_flow_agent_reads_materialized_shadow_snapshot(
    gateway: AgentGatewayService,
    synthetic_project: Path,
) -> None:
    persist_snapshot(
        pl.DataFrame(
            {
                "symbol": ["ABCD"],
                "session_date": [FUTURE_SESSION],
                "availability": ["available"],
                "data_cutoff_utc": [
                    datetime(2099, 1, 5, 14, 0, tzinfo=UTC)
                ],
                "order_imbalance": [0.4],
                "vpoc": [12.34],
                "buy_sell_pressure_ratio": [2.5],
                "quote_size_imbalance": [0.2],
                "microprice": [12.35],
                "spread_bps": [4.0],
                "order_flow_confirmation_score": [68.0],
                "order_flow_provenance": ["test.sip|tick_rule.v1"],
                "production_eligible": [False],
            }
        ).with_columns(
            pl.col("data_cutoff_utc").cast(pl.Datetime("ns", "UTC"))
        ),
        root=synthetic_project / "data",
        source="kernel.features.order_flow_shadow",
        schema_version="order_flow_shadow.v1",
        checks=(),
    )

    result = gateway.features_order_flow(
        agent_name="order-flow",
        trade_date=FUTURE_SESSION,
        symbol="ABCD",
    )

    assert result["availability"] == "available"
    assert result["snapshot_ids"]
    facts = _object_dict(result["data"])["facts"]
    assert isinstance(facts, list)
    by_name = {
        str(_object_dict(fact)["name"]): _object_dict(fact) for fact in facts
    }
    assert by_name["order_imbalance"]["value"] == 0.4
    assert by_name["order_flow_confirmation_score"]["value"] == 68.0
    assert all(fact["provenance"] == "test.sip|tick_rule.v1" for fact in by_name.values())


def test_server_process_identity_binding_is_enforced(
    gateway: AgentGatewayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QUANT_AGENT_NAME", "sentiment")
    with pytest.raises(AuthorizationError, match="identity does not match"):
        gateway.features_order_flow(
            agent_name="order-flow", trade_date=FUTURE_SESSION, symbol="ABCD"
        )


def test_lesson_is_idempotent_and_ticker_anonymous(gateway: AgentGatewayService) -> None:
    with pytest.raises(ValueError, match="ticker-anonymous"):
        gateway.lessons_write(
            agent_name="pdca",
            lesson=Lesson(
                agent=AgentRole.PDCA,
                category=LessonCategory.SELECTION_REVIEW,
                trade_date=FUTURE_SESSION,
                hypothesis="Catalyst quality should distinguish robust selections.",
                observation="ABCD depended on a narrow evidence chain during selection.",
                conclusion="Require broader evidence before accepting this pattern.",
                metrics=(_fact(),),
                source_record_ids=("synthetic-episode",),
            ),
        )

    lesson = Lesson(
        agent=AgentRole.PDCA,
        category=LessonCategory.SELECTION_REVIEW,
        trade_date=FUTURE_SESSION,
        hypothesis="Catalyst quality should distinguish robust selections.",
        observation="The selected cohort depended on a narrow evidence chain.",
        conclusion="Require broader evidence before accepting this pattern.",
        metrics=(_fact(),),
        source_record_ids=("synthetic-episode",),
    )
    first = gateway.lessons_write(agent_name="pdca", lesson=lesson)
    second = gateway.lessons_write(agent_name="pdca", lesson=lesson)
    assert _object_dict(first["data"])["record_id"] == _object_dict(second["data"])["record_id"]
    records = gateway.store.query(StoreQuery(entity=QueryEntity.LESSONS))
    assert len(records) == 1


def test_discipline_report_is_structured_and_idempotent(gateway: AgentGatewayService) -> None:
    report = AuditReport(
        agent=AgentRole.DISCIPLINE,
        trade_date=FUTURE_SESSION,
        status="incomplete_evidence",
    )
    first = gateway.audit_reports_write(agent_name="discipline", report=report)
    second = gateway.audit_reports_write(agent_name="discipline", report=report)
    assert _object_dict(first["data"])["record_id"] == _object_dict(second["data"])["record_id"]
    records = gateway.store.query(StoreQuery(entity=QueryEntity.AUDIT_REPORTS))
    assert len(records) == 1


def test_pdca_materialization_cannot_invent_metrics_or_execution_lessons() -> None:
    metric = _fact("case_metric")
    review = PostmarketLessonReview(
        lessons=(
            LessonDraft(
                category=LessonCategory.SELECTION_REVIEW,
                hypothesis="Catalyst breadth may distinguish robust selections.",
                observation="The anonymous cohort shared a narrow evidence pattern.",
                conclusion="Treat the pattern as fragile until independently replicated.",
                metric_refs=("case-alpha:case_metric",),
                factor_profile=("narrow_catalyst",),
            ),
        )
    )
    lessons = materialize_lessons(
        review,
        trade_date=FUTURE_SESSION,
        metric_index={"case-alpha:case_metric": metric},
        source_record_ids=("synthetic-episode",),
    )
    assert lessons[0].metrics[0].name == "case-alpha:case_metric"

    forbidden = review.model_copy(
        update={
            "lessons": (
                review.lessons[0].model_copy(update={"category": LessonCategory.EXECUTION_GAP}),
            )
        }
    )
    with pytest.raises(ValueError, match="may not infer execution"):
        materialize_lessons(
            forbidden,
            trade_date=FUTURE_SESSION,
            metric_index={"case-alpha:case_metric": metric},
            source_record_ids=("synthetic-episode",),
        )


def test_monthly_evolution_requires_evidence_cluster_and_stays_draft(
    gateway: AgentGatewayService,
) -> None:
    for index in range(10):
        lesson = Lesson(
            agent=AgentRole.PDCA,
            category=LessonCategory.SELECTION_REVIEW,
            trade_date=FUTURE_SESSION,
            hypothesis="Catalyst breadth may distinguish robust selections.",
            observation="The anonymous cohort shared a narrow evidence pattern.",
            conclusion="Treat the pattern as fragile until independently replicated.",
            metrics=(
                Fact(
                    name="cohort_outcome",
                    value=float(index),
                    availability=Availability.AVAILABLE,
                    provenance=f"synthetic.metric.{index}",
                ),
            ),
            source_record_ids=(f"synthetic-episode-{index}",),
            factor_profile=("narrow_catalyst", "high_elasticity"),
        )
        gateway.lessons_write(agent_name="pdca", lesson=lesson)

    package, clusters, metrics, attempted = _build_package(gateway)
    assert package
    assert len(clusters) == 1
    cluster_id = next(iter(clusters))
    count_ref = f"{cluster_id}:observation_count"
    lesson_id = next(iter(clusters[cluster_id]))
    review = MonthlyProposalReview(
        proposals=(
            ProposalDraft(
                hypothesis="A broader evidence gate may reduce fragile selections.",
                expected_effect="Fragile cohort frequency should decline after validation.",
                validation_plan=(
                    "Use point in time data, purged folds, an untouched holdout, "
                    "conservative costs, a placebo, regime checks, and falsification."
                ),
                cluster_ids=(cluster_id,),
                target_metric_refs=(count_ref,),
                evidence_lesson_ids=(lesson_id,),
            ),
        )
    )
    proposals = materialize_proposals(
        review,
        proposal_month=date(2030, 7, 1),
        eligible_clusters=clusters,
        metric_index=metrics,
        attempted_config_hashes=attempted,
    )
    assert proposals[0].status == "draft"
    assert proposals[0].production_eligible is False


def test_proposals_and_tradeplan_submissions_remain_non_executable(
    gateway: AgentGatewayService,
) -> None:
    proposal = EvolutionProposal(
        agent=AgentRole.PDCA,
        proposal_month=date(2030, 7, 1),
        hypothesis="A stronger evidence filter may reduce fragile selections.",
        expected_effect="Fragile selection frequency should decline after validation.",
        validation_plan="Run purged out-of-sample evaluation with frozen costs.",
        target_metrics=(_fact("fragile_selection_rate"),),
        evidence_lesson_ids=("lesson-synthetic",),
    )
    proposal_result = gateway.proposal_write(agent_name="pdca", proposal=proposal)
    proposal_data = _object_dict(proposal_result["data"])
    assert proposal_result["data"] == {
        "record_id": proposal_data["record_id"],
        "status": "draft",
        "production_eligible": False,
    }

    result = gateway.tradeplan_submit(
        agent_name="commander",
        trade_date=FUTURE_SESSION,
        symbol="ABCD",
        confidence=0.7,
        confidence_provenance="synthetic.thesis.consensus",
    )
    result_data = _object_dict(result["data"])
    assert result_data["status"] == "shadow_draft"
    assert result_data["execution_eligible"] is False
    assert result_data["broker_submission_count"] == 0
    drafts = gateway.store.query(StoreQuery(entity=QueryEntity.TRADEPLAN_DRAFTS))
    assert len(drafts) == 1
    assert _object_dict(drafts[0]["document"])["broker_submission_count"] == 0

    with pytest.raises(ValueError, match="must remain non-executable"):
        gateway.store.put_tradeplan_draft(
            actor=AgentRole.COMMANDER,
            document={
                "trade_date": FUTURE_SESSION.isoformat(),
                "status": "shadow_draft",
                "execution_eligible": True,
                "broker_submission_count": 1,
            },
        )

    with pytest.raises(ValidationError, match="never production eligible"):
        EvolutionProposal(
            agent=AgentRole.PDCA,
            proposal_month=date(2030, 7, 1),
            hypothesis="A stronger evidence filter may reduce fragile selections.",
            expected_effect="Fragile selection frequency should decline after validation.",
            validation_plan="Run purged out of sample evaluation with frozen costs.",
            target_metrics=(_fact("fragile_selection_rate"),),
            evidence_lesson_ids=("lesson-synthetic",),
            production_eligible=True,
        )


def test_official_mcp_stdio_lists_and_calls_tools(synthetic_project: Path) -> None:
    async def exercise() -> None:
        env = os.environ.copy()
        env.update(
            {
                "TRADING_SYSTEM_ROOT": str(synthetic_project),
                "QUANT_AGENT_STATE_DB": str(synthetic_project / "runs" / "mcp.sqlite3"),
                "QUANT_AGENT_NAME": "risk",
            }
        )
        project_root = Path(__file__).resolve().parents[1]
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(project_root / "plugins" / "quant-agent" / "server" / "launch.py")],
            env=env,
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = {tool.name for tool in tools.tools}
                assert "universe_query" in names
                assert "tradeplan_submit" in names
                result = await session.call_tool(
                    "universe_query",
                    {
                        "agent_name": "risk",
                        "trade_date": FUTURE_SESSION.isoformat(),
                        "pool": "all",
                        "limit": 1,
                    },
                )
                assert result.isError is False
                assert result.structuredContent is not None
                assert result.structuredContent["tool"] == "universe_query"

    asyncio.run(exercise())
