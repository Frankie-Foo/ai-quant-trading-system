from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl

from data_plane.contracts import DataQualityCheck, QualitySeverity
from data_plane.storage import persist_snapshot
from execution.session_ledger import PaperSessionLedger, PaperSessionStatus
from operations.evidence import refresh_maturity_evidence
from operations.readiness import Attestation, MaturityEvidence

NOW = datetime(2026, 7, 21, 20, 0, tzinfo=UTC)


def _check(source: str) -> tuple[DataQualityCheck, ...]:
    return (
        DataQualityCheck(
            name="test",
            passed=True,
            observed="ok",
            expected="ok",
            severity=QualitySeverity.CRITICAL,
            provenance=source,
        ),
    )


def test_refresh_derives_objective_metrics_and_preserves_attestations(
    tmp_path: Path,
) -> None:
    pit, _ = persist_snapshot(
        pl.DataFrame({"trade_date": [date(2026, 7, 20), date(2026, 7, 21)]}),
        root=tmp_path / "data",
        source="research.history.pit_selection_index",
        schema_version="test",
        checks=_check("pit"),
    )
    assert pit.usable
    census, _ = persist_snapshot(
        pl.DataFrame(
            {
                "symbol": ["A", "B", "C"],
                "status": ["labeled", "quote_unavailable", "labeled"],
            }
        ),
        root=tmp_path / "data",
        source="research.history.trade_replay_census",
        schema_version="test",
        checks=_check("census"),
    )
    persist_snapshot(
        pl.DataFrame({"symbol": ["A", "C"]}),
        root=tmp_path / "data",
        source="research.history.net_labels",
        schema_version="test",
        checks=_check("labels"),
        parent_snapshot_ids=(census.dataset_id,),
    )
    persist_snapshot(
        pl.DataFrame({"fold": [1, 2, 3, 4, 5]}),
        root=tmp_path / "data",
        source="research.validation.purged_oos_folds",
        schema_version="test",
        checks=_check("folds"),
    )
    order_db = tmp_path / "orders.sqlite3"
    sessions = PaperSessionLedger(order_db)
    sessions.start(
        trade_date=date(2026, 7, 21),
        started_at_utc=NOW,
        expected_close_utc=NOW,
        reconciliation_match_rate=1.0,
    )
    sessions.finish(
        trade_date=date(2026, 7, 21),
        ended_at_utc=NOW,
        status=PaperSessionStatus.COMPLETED,
        event_count=10,
        orders_submitted=1,
    )
    attested = Attestation(passed=True, evidence_refs=("receipt:sip",))
    existing = MaturityEvidence(
        asof_utc=NOW,
        full_market_realtime_data=attested,
    )

    result = refresh_maturity_evidence(
        data_root=tmp_path / "data",
        order_db=order_db,
        existing=existing,
        asof_utc=NOW,
    )

    assert result.point_in_time_history_sessions == 2
    assert result.net_labeled_trade_count == 2
    assert result.purged_oos_fold_count == 5
    assert result.quote_cost_coverage == 2 / 3
    assert result.paper_trading_sessions == 1
    assert result.reconciliation_match_rate == 1.0
    assert result.duplicate_order_count == 0
    assert result.full_market_realtime_data == attested

