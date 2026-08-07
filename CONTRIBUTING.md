# Contributing

## Repository contract

`main` is the only production baseline. Do not commit directly to `main`, and do
not deploy a personal branch or worktree. Every change starts from the latest
`main`, uses one branch and one worktree, and reaches production through review.

The project is a modular monolith. Keep deterministic policy in `kernel/`, keep
external systems behind adapters, and preserve the long-only, fail-closed and
point-in-time rules in [AGENTS.md](AGENTS.md).

## Worktree workflow

Use a task-specific worktree outside the repository root:

```powershell
git fetch --prune origin
git worktree add -b codex/<task-name> `
  D:\cdoeX-worktrees\ai-quant-<task-name> main
Set-Location D:\cdoeX-worktrees\ai-quant-<task-name>
```

A parallel change gets its own branch and worktree. Do not share a worktree
between agents or tasks. Remove a worktree only after its branch is merged or
explicitly abandoned:

```powershell
git worktree remove D:\cdoeX-worktrees\ai-quant-<task-name>
git branch -d codex/<task-name>
```

## Before opening a pull request

Run the same checks used by CI:

```powershell
python -m pytest -q
python -m ruff check .
python -m mypy data_plane kernel research execution schedule agent_gateway scripts tests
Set-Location client
npm ci
npm run test:electron
npm run test:ui
npm run build
```

Use Conventional Commits, keep commits focused, and update `PROGRESS.md` with
exact validation evidence for each milestone. Never commit `.env`, API keys,
runtime databases, snapshots, or generated releases.

