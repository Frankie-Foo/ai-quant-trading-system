"""Retry frozen investment projections, never regenerate signals or submit orders."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from operations.feishu_base import FeishuBaseEventClient
from operations.local_env import load_project_env
from operations.paper_state import PaperStateStore
from operations.vps_investment_base import VpsInvestmentClient
from scripts.monitor_modern_momentum_paper import publish_monitor_transitions


def paper_journal_root(project_root: Path) -> Path:
    return project_root / "runs/modern-momentum"


def paper_journal_roots(project_root: Path) -> tuple[Path, ...]:
    return (
        paper_journal_root(project_root),
        project_root / "runs/paper-recovery",
    )


def recover_monitor_journals(root: Path, client: VpsInvestmentClient) -> int:
    """Drain retained-day state journals only; no broker or notification client is created."""
    failures = 0
    for path in sorted(root.glob("*/paper-state.sqlite3")):
        resolved = path.resolve()
        if not resolved.is_relative_to(root.resolve()):
            raise RuntimeError("monitor journal escaped persistent root")
        with closing(sqlite3.connect(resolved.as_uri() + "?mode=ro", uri=True)) as connection:
            pending = connection.execute(
                "SELECT 1 FROM paper_outbox WHERE event_type='monitor_transition' "
                "AND status!='sent' LIMIT 1"
            ).fetchone()
        if not pending:
            continue
        events: list[dict[str, object]] = []
        publish_monitor_transitions(PaperStateStore(resolved), client, events)
        failures += len(events)
    return failures


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    load_project_env(root)
    client = FeishuBaseEventClient.from_environment()
    if not isinstance(client, VpsInvestmentClient):
        raise RuntimeError("projection recovery requires the configured VPS investment provider")
    result = client.flush_pending()
    journal_failures = sum(
        recover_monitor_journals(journal_root, client)
        for journal_root in paper_journal_roots(root)
    )
    print(json.dumps({"investment_projection_recovery": result,
                      "monitor_journal_failures": journal_failures}))
    return int(result["failed"] > 0 or journal_failures > 0)


if __name__ == "__main__":
    raise SystemExit(main())
