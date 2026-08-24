from __future__ import annotations

import ast
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from data_plane.contracts import (
    DataQualityCheck,
    DatasetRejectedError,
    DatasetSnapshot,
    QualitySeverity,
)
from execution.order_state import OrderLifecycle, OrderState, apply_transition
from research.registry import (
    ExperimentEvidence,
    ExperimentStage,
    PerformanceEvidence,
    ResearchRun,
    ResearchSplit,
    ScientificHypothesis,
    evaluate_experiment,
)

NOW = datetime(2026, 7, 16, 10, 0, tzinfo=UTC)
HASH = "a" * 64
ROOT = Path(__file__).parents[1]


def test_buffett_monitor_is_structurally_read_only() -> None:
    path = ROOT / "scripts" / "monitor_trade_plan.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_roots = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert "execution" not in imported_roots
    assert "submit_order" not in source
    assert "AlpacaPaperBroker" not in source


def test_production_supervisor_has_no_legacy_paper_lane() -> None:
    source = (ROOT / "schedule" / "supervisor.py").read_text(encoding="utf-8")
    assert '"schedule.modern_funnel"' in source
    assert '"schedule.paper"' not in source
    assert '"schedule.premarket"' not in source


def test_legacy_paper_entrypoints_are_hard_retired() -> None:
    for path in (
        ROOT / "scripts" / "run_paper_session.py",
        ROOT / "scripts" / "run_autonomous_paper_session.py",
        ROOT / "operations" / "alpaca_paper_autopilot.py",
    ):
        assert "reject_retired_paper_runtime" in path.read_text(encoding="utf-8")
    assert not (ROOT / "scripts" / "start_autonomous_paper_day.ps1").exists()
    compose = (ROOT / "compose.autonomous-paper.yaml").read_text(encoding="utf-8")
    assert "paper-executor:" not in compose
    assert "scripts.run_autonomous_paper_session" not in compose


def test_obsolete_single_symbol_monitors_are_removed() -> None:
    obsolete = (
        "monitor_alab.py",
        "monitor_dis.py",
        "monitor_lly.py",
        "monitor_mrvl.py",
        "monitor_nvda.py",
        "monitor_on.py",
        "monitor_direct_readonly.py",
        "monitor_h30_plan.py",
        "monitor_intraday_attack.py",
    )
    assert not any((ROOT / "scripts" / name).exists() for name in obsolete)


def test_enterprise_operating_documents_and_single_runtime_adr_exist() -> None:
    required = (
        "AGENTS.md",
        "README.md",
        "PROGRESS.md",
        "CHANGELOG.md",
        "docs/ARCHITECTURE.md",
        "docs/DEPLOYMENT.md",
        "docs/RUNBOOK.md",
        "docs/ADR/0001-single-paper-runtime.md",
    )
    assert all((ROOT / path).is_file() for path in required)
    runbook = (ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")
    assert "15:45" in runbook
    assert "15:50" in runbook
    assert "Alpaca Paper" in runbook


def test_dataset_snapshot_requires_timezone_aware_asof() -> None:
    with pytest.raises(ValidationError):
        DatasetSnapshot(
            dataset_id="bars-1m-2026-07-15",
            source="massive.aggs",
            asof_utc=datetime(2026, 7, 16, 10, 0),
            content_sha256=HASH,
            schema_version="bars_1m.v1",
            row_count=390,
        )


def test_critical_data_quality_failure_quarantines_snapshot() -> None:
    failed_check = DataQualityCheck(
        name="ohlc_logic",
        severity=QualitySeverity.CRITICAL,
        passed=False,
        observed="high < close",
        expected="high >= max(open, close)",
        provenance="data.quality.ohlc@2026-07-16T10:00:00Z",
    )
    snapshot = DatasetSnapshot(
        dataset_id="bars-1m-2026-07-15",
        source="massive.aggs",
        asof_utc=NOW,
        content_sha256=HASH,
        schema_version="bars_1m.v1",
        row_count=390,
        checks=(failed_check,),
    )
    assert snapshot.usable is False
    with pytest.raises(DatasetRejectedError):
        snapshot.assert_usable()


def test_research_split_enforces_true_out_of_sample_order() -> None:
    with pytest.raises(ValidationError):
        ResearchSplit(
            train_start=date(2023, 1, 1),
            train_end=date(2024, 12, 31),
            validation_start=date(2024, 6, 1),
            validation_end=date(2025, 12, 31),
            test_start=date(2026, 1, 1),
            test_end=date(2026, 6, 30),
        )


def test_research_run_manifest_is_stable_and_auditable() -> None:
    split = ResearchSplit(
        train_start=date(2023, 1, 1),
        train_end=date(2024, 12, 31),
        validation_start=date(2025, 1, 1),
        validation_end=date(2025, 12, 31),
        test_start=date(2026, 1, 1),
        test_end=date(2026, 6, 30),
    )
    run = ResearchRun(
        run_id=UUID("00000000-0000-0000-0000-000000000001"),
        created_at_utc=NOW,
        data_snapshot_ids=("bars-1m-2026-07-15",),
        feature_set_sha256="b" * 64,
        config_sha256="c" * 64,
        code_sha256="d" * 64,
        random_seed=7,
        attempted_configurations=1,
        split=split,
    )
    assert run.manifest_sha256() == run.manifest_sha256()
    assert len(run.manifest_sha256()) == 64


def _performance(*, expectancy: float, profit_factor: float) -> PerformanceEvidence:
    return PerformanceEvidence(
        trades=100,
        win_rate=0.40,
        average_win_loss=1.80,
        profit_factor=profit_factor,
        expectancy=expectancy,
        expectancy_ci95=(expectancy - 10, expectancy + 10),
    )


def test_ai4s_gate_rejects_negative_out_of_sample_evidence() -> None:
    hypothesis = ScientificHypothesis(
        hypothesis_id="modern-h15-pullback-only.v1",
        statement="Waiting for pullback acceptance should improve net expectancy.",
        mechanism="Accepted higher prices should reduce failed breakout entries.",
        falsification="Reject when blind net expectancy is not positive after full costs.",
        changed_variable="entry_mode",
        control="first_breakout_entry",
        validation_plan="Use chronological train, validation, blind, then Paper evidence.",
        evidence_ids=("current_modern_h15_3y",),
    )
    evidence = ExperimentEvidence(
        hypothesis=hypothesis,
        full=_performance(expectancy=-92.08, profit_factor=0.67),
        blind=_performance(expectancy=-34.26, profit_factor=0.87),
        attempted_configurations=1,
        blind_evaluations=1,
        point_in_time=True,
        quote_aware_costs=True,
        critical_quality_passed=True,
    )

    decision = evaluate_experiment(evidence)

    assert decision.stage is ExperimentStage.REJECTED
    assert decision.production_eligible is False


def test_ai4s_gate_requires_paper_forward_before_human_review() -> None:
    hypothesis = ScientificHypothesis(
        hypothesis_id="modern-h15-pullback-only.v1",
        statement="Waiting for pullback acceptance should improve net expectancy.",
        mechanism="Accepted higher prices should reduce failed breakout entries.",
        falsification="Reject when blind net expectancy is not positive after full costs.",
        changed_variable="entry_mode",
        control="first_breakout_entry",
        validation_plan="Use chronological train, validation, blind, then Paper evidence.",
        evidence_ids=("future_backtest",),
    )
    evidence = ExperimentEvidence(
        hypothesis=hypothesis,
        full=_performance(expectancy=30, profit_factor=1.20),
        blind=_performance(expectancy=20, profit_factor=1.10),
        attempted_configurations=1,
        blind_evaluations=1,
        point_in_time=True,
        quote_aware_costs=True,
        critical_quality_passed=True,
    )

    assert evaluate_experiment(evidence).stage is ExperimentStage.ELIGIBLE_FOR_PAPER
    reviewed = evaluate_experiment(evidence.model_copy(update={"paper_trading_days": 30}))
    assert reviewed.stage is ExperimentStage.ELIGIBLE_FOR_HUMAN_REVIEW
    assert reviewed.production_eligible is False


def test_oms_state_machine_rejects_illegal_transition() -> None:
    order = OrderLifecycle(
        client_order_id="plan-1-entry-1",
        plan_id="plan-1",
        symbol="NVDA",
        requested_shares=100,
    )
    with pytest.raises(ValueError, match="illegal order transition"):
        apply_transition(
            order,
            OrderState.FILLED,
            at_utc=NOW,
            provenance="execution.test@2026-07-16T10:00:00Z",
            filled_shares=100,
        )


def test_oms_state_machine_records_partial_fill_path() -> None:
    order = OrderLifecycle(
        client_order_id="plan-1-entry-1",
        plan_id="plan-1",
        symbol="NVDA",
        requested_shares=100,
    )
    for state in (OrderState.PENDING_RISK, OrderState.APPROVED, OrderState.SUBMITTED):
        order = apply_transition(
            order,
            state,
            at_utc=NOW,
            provenance="execution.test@2026-07-16T10:00:00Z",
        )
    order = apply_transition(
        order,
        OrderState.PARTIALLY_FILLED,
        at_utc=NOW,
        provenance="broker.fill@2026-07-16T10:00:00Z",
        filled_shares=40,
    )
    order = apply_transition(
        order,
        OrderState.FILLED,
        at_utc=NOW,
        provenance="broker.fill@2026-07-16T10:00:00Z",
        filled_shares=100,
    )
    assert order.state is OrderState.FILLED
    assert order.filled_shares == 100
    assert [event.sequence for event in order.events] == [1, 2, 3, 4, 5]
