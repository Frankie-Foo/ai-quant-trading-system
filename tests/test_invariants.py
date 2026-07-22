from __future__ import annotations

import ast
from pathlib import Path

from kernel.config import Config, load_config

ROOT = Path(__file__).resolve().parents[1]
DETERMINISTIC_DIRS = ("kernel", "execution")
FORBIDDEN_LLM_IMPORTS = {"openai", "anthropic", "litellm"}


def test_kernel_has_no_llm_imports() -> None:
    violations: list[str] = []
    for directory in DETERMINISTIC_DIRS:
        for path in sorted((ROOT / directory).rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    root_name = name.split(".", maxsplit=1)[0]
                    if root_name in FORBIDDEN_LLM_IMPORTS:
                        line = getattr(node, "lineno", 0)
                        violations.append(f"{path.relative_to(ROOT)}:{line}:{name}")
    assert violations == []


def test_config_loads() -> None:
    cfg = load_config(ROOT / "config.yaml")
    assert isinstance(cfg, Config)
    assert cfg.capital == 200_000
    assert cfg.long_only is True
    assert cfg.max_concurrent == 8
    assert cfg.tiers.mega.weight == 0.15
    assert cfg.exits.time_stop_et == "15:55"
    assert cfg.market_data.max_quote_age_seconds == 90
    assert cfg.market_data.paper_start_lead_minutes == 10
    assert cfg.market_data.postmarket_data_grace_minutes == 20
    assert cfg.guardrails.lock_time_beijing == "08:00"
    assert cfg.guardrails.selection_time_beijing == "20:00"
    assert cfg.scheduler.premarket_retry_minutes == 30
    assert cfg.scheduler.premarket_max_attempts == 24
    assert cfg.scheduler.postmarket_retry_minutes == 30
    assert cfg.scheduler.postmarket_max_attempts == 5


def test_secrets_are_not_in_config() -> None:
    raw = (ROOT / "config.yaml").read_text(encoding="utf-8").upper()
    assert "POLYGON_API_KEY" not in raw
    assert "POSTGRES_DSN" not in raw
