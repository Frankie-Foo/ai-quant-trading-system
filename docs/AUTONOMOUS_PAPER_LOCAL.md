# Retired autonomous Paper stack

The former Docker autonomous executor, ORB runner and desktop Alpaca autopilot are
retired as of 2026-08-24. Their deterministic libraries and historical ledgers remain
for tests and postmortems, but every executable entrypoint fails closed. The Compose
file contains read-only SIP and agent research services only.

Do not arm, deploy or restore that stack. The only supported automated execution path
is `schedule.modern_funnel` -> `scripts.run_modern_funnel_stage` ->
`scripts.monitor_modern_momentum_paper`, governed by [RUNBOOK.md](RUNBOOK.md) and
[DEPLOYMENT.md](DEPLOYMENT.md). It targets Alpaca Paper only and is frozen by default.
