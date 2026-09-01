from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_enterprise_governance_files_exist() -> None:
    required = (
        "AGENTS.md",
        "README.md",
        "PROGRESS.md",
        "CHANGELOG.md",
        "CONTRIBUTING.md",
        "SECURITY.md",
        "docs/ARCHITECTURE.md",
        "docs/DEPLOYMENT.md",
        "docs/RUNBOOK.md",
        "docs/ADR/0001-modular-monolith-and-worktrees.md",
        ".github/CODEOWNERS",
        ".github/pull_request_template.md",
        ".github/workflows/ci.yml",
        "cliff.toml",
        "db/README.md",
    )
    assert all((ROOT / path).is_file() for path in required)


def test_ci_runs_on_main_and_is_read_only() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "pull_request:" in workflow
    assert "main" in workflow
    assert "contents: read" in workflow
    assert "BROKER_WRITE_ENABLED" not in workflow


def test_release_contract_requires_a_recorded_commit() -> None:
    deployment = (ROOT / "docs/DEPLOYMENT.md").read_text(encoding="utf-8")
    assert "reviewed commit merged into `main`" in deployment
    assert "rollback commit or image digest" in deployment
    assert "migration version" in deployment
