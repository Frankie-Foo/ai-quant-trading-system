from __future__ import annotations

import argparse

from scripts.run_autonomous_sip_refresher import refresh_command


def test_refresher_runs_one_full_warmup_then_incremental_updates() -> None:
    args = argparse.Namespace(
        config="config/autonomous.json",
        sip_db="runs/sip.sqlite3",
        refresh_lock_file="runs/warm.lock",
        history_days=10,
    )

    full = refresh_command(args, incremental=False)
    incremental = refresh_command(args, incremental=True)

    assert "scripts.warm_autonomous_sip_store" in full
    assert "--incremental" not in full
    assert incremental[:-1] == full
    assert incremental[-1] == "--incremental"
