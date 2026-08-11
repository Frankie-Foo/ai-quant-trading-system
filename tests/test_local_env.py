from __future__ import annotations

import os

from operations.local_env import load_project_env, project_data_root


def test_load_project_env_promotes_alpaca_aliases_and_scopes_shared_env(
    tmp_path, monkeypatch
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
