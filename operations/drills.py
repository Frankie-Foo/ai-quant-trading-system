"""Repeatable local safety drills using production code paths and no network writes."""

from __future__ import annotations

import gc
import json
import shutil
import sqlite3
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import NoReturn

from execution.alpaca_paper import PaperOrderRequest
from execution.engine import PaperExecutionEngine
from execution.ledger import OrderLedger
from execution.order_state import OrderState
from kernel.config import load_config
from kernel.guardrails import GuardrailContext, RiskCode
from kernel.tradeplan import TradePlan
from operations.backup import create_backup, restore_and_verify


@dataclass(frozen=True)
class LocalDrillReceipt:
    receipt_id: str
    created_at_utc: str
    kill_switch_passed: bool
    broker_calls_during_kill_switch: int
    backup_restore_passed: bool
    restored_file_count: int
    backup_archive: str
    representative_snapshot: str
    provenance: str = "operations.drills.run_local_safety_drills.v1"


class _TripwireBroker:
    writes_enabled = True

    def __init__(self) -> None:
        self.calls = 0

    def submit_order_idempotent(self, request: PaperOrderRequest) -> NoReturn:
        del request
        self.calls += 1
        raise AssertionError("kill switch allowed a broker write")


def _run_kill_switch(config_path: Path, working_dir: Path) -> tuple[bool, int]:
    now = datetime.now(UTC)
    plan = TradePlan(
        plan_id="local-kill-switch-drill",
        trace_id="local-kill-switch-drill",
        strategy_version="orb5.v1",
        symbol="AAPL",
        trade_date=now.date(),
        decision_asof_utc=now - timedelta(seconds=1),
        created_at_utc=now,
        quantity=1,
        reference_price=Decimal("100"),
        take_profit_price=Decimal("102"),
        stop_loss_price=Decimal("99"),
        time_stop_utc=now + timedelta(minutes=1),
        source_snapshot_ids=("drill-selection",),
        provenance="operations.drills.kill_switch.v1",
    )
    context = GuardrailContext(
        evaluated_at_utc=now,
        market_data_asof_utc=now - timedelta(seconds=1),
        market_data_feed="sip",
        paper_endpoint=True,
        kill_switch_active=True,
        market_open=True,
        account_active=True,
        account_blocked=False,
        trading_blocked=False,
        equity=Decimal("100000"),
        daily_pnl=Decimal("0"),
        gross_exposure=Decimal("0"),
        buying_power=Decimal("100000"),
        sizing_notional_cap=Decimal("100"),
        selected_symbols=("AAPL",),
        selection_snapshot_ids=("drill-selection",),
    )
    broker = _TripwireBroker()
    result = PaperExecutionEngine(
        broker=broker,
        ledger=OrderLedger(working_dir / "kill-switch.sqlite3"),
        config=load_config(config_path),
        paper_authorized=True,
    ).execute(plan, context)
    passed = (
        result.lifecycle.state is OrderState.REJECTED
        and result.verdict.failure_code is RiskCode.P0_KILL_SWITCH
        and broker.calls == 0
    )
    return passed, broker.calls


def _representative_snapshot(data_root: Path) -> Path:
    candidates = tuple((data_root / "accepted").glob("*/data.parquet"))
    if not candidates:
        raise FileNotFoundError("no accepted snapshot is available for restore drill")
    return min(candidates, key=lambda path: path.stat().st_size)


def run_local_safety_drills(
    *,
    root: Path,
    data_root: Path,
    state_root: Path,
    backup_dir: Path,
    receipt_dir: Path,
) -> tuple[LocalDrillReceipt, Path]:
    created = datetime.now(UTC)
    receipt_id = f"local-safety-{created:%Y%m%dT%H%M%S%fZ}"
    with tempfile.TemporaryDirectory(prefix="trading-safety-drill-") as temporary:
        work = Path(temporary)
        kill_passed, broker_calls = _run_kill_switch(root / "config.yaml", work)
        # sqlite3 context managers commit but do not close; force finalizers before
        # TemporaryDirectory cleanup on Windows, where open databases cannot unlink.
        gc.collect()
        representative = _representative_snapshot(data_root)
        sample_root = work / "sample-data"
        sample_destination = sample_root / "accepted" / representative.parent.name
        shutil.copytree(representative.parent, sample_destination)
        sqlite_paths = tuple(sorted(state_root.glob("*.sqlite3")))
        if not sqlite_paths:
            sample_db = work / "state" / "drill.sqlite3"
            sample_db.parent.mkdir()
            with sqlite3.connect(sample_db) as connection:
                connection.execute("CREATE TABLE drill (passed INTEGER NOT NULL)")
                connection.execute("INSERT INTO drill VALUES (1)")
            sqlite_paths = (sample_db,)
        archive = create_backup(
            data_root=sample_root,
            sqlite_paths=sqlite_paths,
            destination_dir=backup_dir,
            include_data=True,
        )
        restored = restore_and_verify(archive, restore_dir=work / "restored")
        restored_snapshot = (
            f"data/accepted/{representative.parent.name}/data.parquet"
        )
        backup_passed = restored_snapshot in restored and any(
            name.startswith("state/") for name in restored
        )
    receipt = LocalDrillReceipt(
        receipt_id=receipt_id,
        created_at_utc=created.isoformat(),
        kill_switch_passed=kill_passed,
        broker_calls_during_kill_switch=broker_calls,
        backup_restore_passed=backup_passed,
        restored_file_count=len(restored),
        backup_archive=str(archive),
        representative_snapshot=representative.parent.name,
    )
    receipt_dir.mkdir(parents=True, exist_ok=True)
    receipt_path = receipt_dir / f"{receipt_id}.json"
    receipt_path.write_text(
        json.dumps(asdict(receipt), indent=2, sort_keys=True), encoding="utf-8"
    )
    if not kill_passed or not backup_passed:
        raise RuntimeError(f"local safety drill failed; receipt={receipt_path}")
    return receipt, receipt_path
