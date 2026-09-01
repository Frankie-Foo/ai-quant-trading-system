import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KERNEL_FORBIDDEN_ROOTS = {
    "agent_gateway",
    "anthropic",
    "client",
    "data_plane.providers",
    "execution",
    "httpx",
    "litellm",
    "openai",
    "operations",
    "psycopg",
    "requests",
    "schedule",
    "scripts",
    "sqlite3",
}


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_kernel_does_not_depend_on_outer_adapters() -> None:
    violations: list[str] = []
    for path in sorted((ROOT / "kernel").rglob("*.py")):
        for module in _imported_modules(path):
            if any(
                module == forbidden or module.startswith(f"{forbidden}.")
                for forbidden in KERNEL_FORBIDDEN_ROOTS
            ):
                violations.append(f"{path.relative_to(ROOT)}:{module}")
    assert violations == []
