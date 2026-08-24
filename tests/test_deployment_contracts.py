from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_container_build_context_excludes_secrets_and_runtime_state() -> None:
    ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in ignored
    assert ".venv/" in ignored
    assert "data/" in ignored
    assert "runs/" in ignored


def test_systemd_service_is_non_root_hardened_and_idempotent() -> None:
    service = (ROOT / "deploy/systemd/trading-postmarket.service").read_text(encoding="utf-8")
    timer = (ROOT / "deploy/systemd/trading-postmarket.timer").read_text(encoding="utf-8")
    assert "User=trading" in service
    assert "NoNewPrivileges=true" in service
    assert "ProtectSystem=strict" in service
    assert "--lock-file /run/trading-system/postmarket.lock" in service
    assert "--state-db /var/lib/trading-system/state/jobs.sqlite3" in service
    assert "--llm-mode optional" in service
    assert "Persistent=true" in timer


def test_premarket_and_paper_units_preserve_fail_closed_defaults() -> None:
    premarket = (ROOT / "deploy/systemd/trading-premarket.service").read_text(encoding="utf-8")
    paper = (ROOT / "deploy/systemd/trading-paper.service").read_text(encoding="utf-8")
    environment = (ROOT / "deploy/trading-system.env.example").read_text(encoding="utf-8")
    assert "User=trading" in premarket and "User=trading" in paper
    assert "NoNewPrivileges=true" in premarket and "NoNewPrivileges=true" in paper
    assert "schedule.premarket" in premarket
    assert "scripts.run_paper_session" in paper
    assert "scripts.verify_alpaca_access" in paper
    assert "--sip-lock-file /run/trading-system/alpaca-sip.lock" in paper
    assert "scripts.refresh_maturity_evidence" in paper
    assert "BROKER_WRITE_ENABLED=false" in environment
    assert "TRADING_KILL_SWITCH=true" in environment
    assert "CLOUD_PLATFORM_BASE_URL=https://cloud-strategy-platform.example.internal" in environment
    assert "MARKET_DATA_PROVIDER=cloud_proxy" in environment
    assert "ALPACA_API_KEY_ID" not in environment
    assert "ALPACA_API_SECRET_KEY" not in environment


def test_alert_and_verified_backup_units_are_fail_closed() -> None:
    backup = (ROOT / "deploy/systemd/trading-backup.service").read_text(encoding="utf-8")
    alert = (ROOT / "deploy/systemd/trading-alert@.service").read_text(encoding="utf-8")
    assert "scripts.backup_state" in backup
    assert "--include-data" in backup
    assert "OnFailure=trading-alert@backup.service" in backup
    assert "scripts.send_operational_alert" in alert
    assert "NoNewPrivileges=true" in alert


def test_weekly_research_unit_runs_governed_cycle_without_order_flags() -> None:
    service = (ROOT / "deploy/systemd/trading-research.service").read_text(encoding="utf-8")
    timer = (ROOT / "deploy/systemd/trading-research.timer").read_text(encoding="utf-8")
    assert "schedule.research_cycle" in service
    assert "User=trading" in service
    assert "OnFailure=trading-alert@research.service" in service
    assert "BROKER_WRITE_ENABLED" not in service
    assert "Persistent=true" in timer


def test_monthly_evolution_unit_is_hardened_and_draft_only() -> None:
    service = (ROOT / "deploy/systemd/trading-monthly-evolution.service").read_text(
        encoding="utf-8"
    )
    timer = (ROOT / "deploy/systemd/trading-monthly-evolution.timer").read_text(encoding="utf-8")
    environment = (ROOT / "deploy/trading-system.env.example").read_text(encoding="utf-8")
    assert "User=trading" in service
    assert "NoNewPrivileges=true" in service
    assert "schedule.monthly_evolution" in service
    assert "OnFailure=trading-alert@monthly-evolution.service" in service
    assert "BROKER_WRITE_ENABLED" not in service
    assert "Asia/Shanghai" in timer and "Persistent=true" in timer
    assert "QUANT_AGENT_STATE_DB=/var/lib/trading-system/state/agent-facts.sqlite3" in environment


def test_production_image_does_not_install_test_or_lint_tools() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    requirements = (ROOT / "requirements-prod.txt").read_text(encoding="utf-8")
    assert "requirements-prod.txt" in dockerfile
    assert "pytest" not in requirements
    assert "ruff" not in requirements
    assert "mypy" not in requirements


def test_market_data_scripts_do_not_hardcode_a_fifteen_minute_sip_delay() -> None:
    for relative in (
        "research/history.py",
        "scripts/backfill_historical_premarket.py",
        "scripts/build_premarket_rvol.py",
        "scripts/build_orb5_signals.py",
        "schedule/postmarket.py",
    ):
        raw = (ROOT / relative).read_text(encoding="utf-8")
        assert "PROVIDER_DELAY_MINUTES" not in raw
        assert "PREMARKET_PROVIDER_DELAY" not in raw
        assert "PROVIDER_DELAY =" not in raw


def test_windows_observation_tasks_cover_all_daily_phases_without_order_flags() -> None:
    installer = (ROOT / "scripts/install_local_observation_tasks.ps1").read_text(
        encoding="utf-8"
    )
    premarket = (ROOT / "scripts/run_premarket_tick.ps1").read_text(encoding="utf-8")
    paper = (ROOT / "scripts/run_paper_tick.ps1").read_text(encoding="utf-8")
    postmarket = (ROOT / "scripts/run_postmarket_tick.ps1").read_text(encoding="utf-8")

    assert 'TaskName "Trading System V2 - AI Quant Funnel"' in installer
    assert 'Runner (Join-Path $PSScriptRoot "run_modern_funnel_tick.ps1")' in installer
    assert "-IntervalMinutes 1" in installer
    legacy_paper_registration = (
        'Register-ObservationTask `\n    -TaskName "Trading System V2 - Paper Session"'
    )
    assert legacy_paper_registration not in installer
    assert "Trading System V2 - Postmarket Review" in installer
    assert "MultipleInstances IgnoreNew" in installer
    assert "schedule.premarket" in premarket
    assert "prepare_autonomous_selection_handoff" not in premarket
    assert "start_autonomous_paper_day" not in premarket
    assert "schedule.paper" in paper
    assert "schedule.postmarket" in postmarket
    assert "BROKER_WRITE_ENABLED" not in installer + premarket + paper + postmarket
    assert "TRADING_KILL_SWITCH" not in installer + premarket + paper + postmarket


def test_windows_supervisor_is_a_current_user_startup_fallback() -> None:
    installer = (ROOT / "scripts/install_local_observation_supervisor.ps1").read_text(
        encoding="utf-8"
    )
    runner = (ROOT / "scripts/run_local_observation_supervisor.ps1").read_text(
        encoding="utf-8"
    )

    assert "Local Observation Supervisor.lnk" in installer
    assert "Disable-ScheduledTask" in installer
    assert "schedule.supervisor" in runner
    assert "BROKER_WRITE_ENABLED" not in installer + runner
    assert "TRADING_KILL_SWITCH" not in installer + runner
