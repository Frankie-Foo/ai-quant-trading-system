from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from operations.local_env import load_project_env, project_data_root


def test_load_project_env_promotes_alpaca_aliases_and_scopes_shared_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    external = tmp_path / "alpaca.env"
    shared = tmp_path / "shared.env"
    external.write_text(
        "ALPACA_API_KEY=paper-key\nALPACA_SECRET_KEY=paper-secret\n",
        encoding="utf-8",
    )
    shared.write_text(
        "MASSIVE_API_KEY=massive-key\nPOSTGRES_DSN=must-not-load\n",
        encoding="utf-8",
    )
    (project / ".env").write_text(
        f"ALPACA_ENV_FILE={external}\n"
        f"TRADING_SHARED_ENV_FILE={shared}\n"
        "AI_QUANT_DATA_ROOT=runtime/data\n",
        encoding="utf-8",
    )
    for name in (
        "ALPACA_API_KEY_ID",
        "ALPACA_API_SECRET_KEY",
        "ALPACA_PAPER_KEY_ID",
        "ALPACA_PAPER_SECRET_KEY",
        "MASSIVE_API_KEY",
        "POSTGRES_DSN",
    ):
        monkeypatch.delenv(name, raising=False)

    load_project_env(project)

    assert os.environ["ALPACA_API_KEY_ID"] == "paper-key"
    assert os.environ["ALPACA_API_SECRET_KEY"] == "paper-secret"
    assert os.environ["ALPACA_PAPER_KEY_ID"] == "paper-key"
    assert os.environ["ALPACA_PAPER_SECRET_KEY"] == "paper-secret"
    assert os.environ["MASSIVE_API_KEY"] == "massive-key"
    assert "POSTGRES_DSN" not in os.environ
    assert project_data_root(project) == project / "runtime" / "data"


def test_load_project_env_accepts_an_explicit_machine_runtime_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "worktree"
    project.mkdir()
    runtime_env = tmp_path / "machine.env"
    shared_data_root = tmp_path / "shared" / "ai-quant" / "data"
    runtime_env.write_text(
        "FEISHU_INVESTMENT_BASE_TOKEN=dedicated-base\n"
        f"AI_QUANT_DATA_ROOT={shared_data_root}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AI_QUANT_RUNTIME_ENV_FILE", str(runtime_env))
    monkeypatch.delenv("FEISHU_INVESTMENT_BASE_TOKEN", raising=False)
    monkeypatch.delenv("AI_QUANT_DATA_ROOT", raising=False)

    load_project_env(project)

    assert os.environ["FEISHU_INVESTMENT_BASE_TOKEN"] == "dedicated-base"
    assert project_data_root(project) == shared_data_root


def test_market_data_credentials_are_loaded_only_after_2100_beijing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    sip_env = tmp_path / "gary.env"
    sip_env.write_text(
        "ALPACA_API_KEY=sip-key\n"
        "ALPACA_SECRET_KEY=sip-secret\n"
        "ALPACA_DATA_URL=https://data.alpaca.markets\n"
        "FINNHUB_API_KEY=finnhub-key\n"
        "ALPHAVANTAGE_API_KEY=alpha-vantage-key\n",
        encoding="utf-8",
    )
    (project / ".env").write_text(
        f"ALPACA_ENV_FILE={tmp_path / 'missing.env'}\n"
        f"ALPACA_SIP_ENV_FILE={sip_env}\n",
        encoding="utf-8",
    )
    for name in (
        "ALPACA_ENV_FILE",
        "ALPACA_SIP_ENV_FILE",
        "ALPACA_API_KEY_ID",
        "ALPACA_API_SECRET_KEY",
        "ALPACA_API_KEY",
        "ALPACA_SECRET_KEY",
        "APCA_API_KEY_ID",
        "APCA_API_SECRET_KEY",
        "ALPACA_PAPER_KEY_ID",
        "ALPACA_PAPER_SECRET_KEY",
        "ALPACA_DATA_URL",
        "FINNHUB_API_KEY",
        "ALPHAVANTAGE_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ALPACA_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.setenv("ALPACA_SIP_ENV_FILE", str(sip_env))

    load_project_env(project, now_utc=datetime(2026, 8, 31, 12, 59, tzinfo=UTC))
    assert "ALPACA_API_KEY_ID" not in os.environ
    assert "FINNHUB_API_KEY" not in os.environ
    assert "ALPHAVANTAGE_API_KEY" not in os.environ

    load_project_env(project, now_utc=datetime(2026, 8, 31, 13, 0, tzinfo=UTC))
    assert os.environ["ALPACA_API_KEY_ID"] == "sip-key"
    assert os.environ["ALPACA_API_SECRET_KEY"] == "sip-secret"
    assert os.environ["ALPACA_DATA_URL"] == "https://data.alpaca.markets"
    assert "ALPACA_PAPER_KEY_ID" not in os.environ
    assert "ALPACA_PAPER_SECRET_KEY" not in os.environ
    assert os.environ["FINNHUB_API_KEY"] == "finnhub-key"
    assert os.environ["ALPHAVANTAGE_API_KEY"] == "alpha-vantage-key"
