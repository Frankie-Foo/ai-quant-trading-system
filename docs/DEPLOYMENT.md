# Deployment

Production is always a reviewed commit merged into `main`. Development happens in a
dedicated worktree and branch; no worktree is itself a deployment target.

## Release gates

1. Review `AGENTS.md`, the diff and migration impact.
2. Run full pytest, Ruff, Mypy and the offline Paper acceptance drills.
3. Verify Python 3.12 compilation and record the exact commit, config hash and
   migration version.
4. Verify Alpaca market data and Paper account read-only, the dedicated Investment
   Base configuration and Livermore bot identity. Never query a disconnected old Base.
5. Merge the reviewed commit to `main`; record the rollback commit or image digest.
6. Keep the Windows funnel disabled until the owner gives the one-time unfreeze
   confirmation.
7. On the next XNYS session, set a maximum Paper notional of $100 for the smoke run.
   Do not count smoke results as strategy evidence or Memory.

## Windows installation

Run `scripts/install_local_observation_tasks.ps1` only after review, passing explicit
paths for the approved Python interpreter, machine-owned environment file and shared
data root. Add `-ArmPaper -PaperSmokeMaxNotional 100` only after owner unfreeze. The
installer creates the one-minute `Trading System V2 - AI Quant Funnel` task and
postmarket review, and disables the old premarket and Paper tasks.
`schedule.modern_funnel` computes ET/DST and XNYS sessions; Windows local time does not
define trading windows. Secrets remain in the machine-owned environment file and are
not copied into a worktree or Task Scheduler arguments.

The installation does not authorize Paper writes. Arming still requires all of:

- `BROKER_WRITE_ENABLED=true`;
- `TRADING_KILL_SWITCH=false`;
- `AI_QUANT_PAPER_RUNTIME_CONFIRMED=true`;
- `AI_QUANT_PAPER_SMOKE_MAX_NOTIONAL` in `(0, 100]`;
- a valid immutable `open_confirmation.json` with Feishu and Livermore receipts.

This release remains capped at $100 after the first smoke session. Removing or raising
the cap requires a separate reviewed release and fresh owner approval; changing an
environment variable alone cannot promote the runtime.

Rollback: disable the funnel task, restore `TRADING_KILL_SWITCH=true`, preserve `runs/`
for reconciliation, and deploy the prior recorded commit. Disabling a process does not
cancel broker orders; inspect Alpaca Paper before any restart.
