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
7. The owner approved a separate $200,000 aggregate Paper release on 2026-09-07.
   Existing positions and unfilled buys consume the ceiling, which is also capped by
   account equity and remaining risk. Do not create trades merely to pass acceptance.
   Synthetic drills and deployment checks are not strategy evidence or Memory.

## Windows installation

Run `scripts/install_local_observation_tasks.ps1` only after review, passing explicit
paths for the approved Python interpreter, machine-owned environment file and shared
data root, `-ActivePolicyFile`, `-ChallengerPolicyFile`, and `-RuntimeStateRoot`.
The installer never bootstraps a replacement active policy. Before running it:

1. Export the current task definitions and record all authoritative state roots.
2. Disable and drain the owned scheduler tasks, including legacy premarket. Disable
   and preserve the legacy `Local Observation Supervisor.lnk` startup launcher and
   drain `schedule.supervisor` / `run_local_observation_supervisor.ps1`; verify
   no detached Paper monitor remains. Preserve broker protection orders and reconcile
   positions/open orders read-only before restarting a writer. Leave Buffett untouched.
3. Back up SQLite ledgers with the SQLite backup API after quiescence. Reconcile
   jobs, funnel, Paper/order, notification, Loop-outbox and immutable authorization
   evidence from the previous roots into the approved persistent state directory.
   Do not overwrite colliding evidence or rewrite embedded historical paths/hashes.
   Preserve the old roots for receipt references and rollback.
4. Bind the new release's `runs` directory to that existing state directory with a
   Windows directory junction. The installer checks directory identity and refuses
   missing, new release-local or mismatched state. `DataRoot` is not order state.
5. Pass the existing approved policy paths, then install. All replacement tasks are
   registered **disabled**, including on partial installation failure. Inspect every
   definition, re-run external dependency checks and reconciliation, then enable one
   modern funnel owner and its review/research tasks. Never re-enable legacy selection.

Add `-ArmPaper -PaperSmokeMaxNotional 200000` only after owner unfreeze. The
installer creates the one-minute `Trading System V2 - AI Quant Funnel` task and
postmarket review, and disables the old premarket and Paper tasks.
`schedule.modern_funnel` computes ET/DST and XNYS sessions; Windows local time does not
define trading windows. Secrets remain in the machine-owned environment file and are
not copied into a worktree or Task Scheduler arguments.

The funnel uses the exchange clock: first wave 08:30 ET, second wave 09:00 ET,
final rank 09:30 ET, and opening confirmation 09:35 ET. There is no Beijing-time
gate in the funnel scheduler, so daylight-saving changes cannot shift a wave into
the next stage. Gary SIP credentials remain subject to their own explicit
authorization window; an unavailable credential or pre-authorized data snapshot
must fail closed instead of shifting the funnel clock. XNYS holidays do not run
selection or place orders.

The installation does not authorize Paper writes. Arming still requires all of:

- `BROKER_WRITE_ENABLED=true`;
- `TRADING_KILL_SWITCH=false`;
- `AI_QUANT_PAPER_RUNTIME_CONFIRMED=true`;
- `AI_QUANT_PAPER_SMOKE_MAX_NOTIONAL` in `(0, 200000]` (legacy variable name retained);
- a valid immutable `open_confirmation.json` with Feishu and Livermore receipts.

The 2026-09-07 release replaces the former $100-only smoke ceiling. The installer
retains a conservative default; the approved deployment must pass 200000 explicitly.
Removing or raising the compiled $200,000 ceiling requires a separate reviewed release
and fresh owner approval; an environment variable alone cannot promote the runtime.

Rollback: disable the funnel task, restore `TRADING_KILL_SWITCH=true`, preserve `runs/`
for reconciliation, and deploy the prior recorded commit. Disabling a process does not
cancel broker orders; inspect Alpaca Paper before any restart.
