# ADR 0001: One automated Paper runtime

- Status: accepted
- Date: 2026-08-24

## Decision

`scripts.monitor_modern_momentum_paper` is the sole automated broker-writing module and
may target only Alpaca Paper. `schedule.modern_funnel` is the sole production scheduler
for its three selection stages. Buffett monitoring remains read-only.

## Consequences

Legacy per-symbol, dated H30 and alternate Paper launchers are removed. A stage cannot
be marked successful without a machine-readable receipt. Missing data, mismatched state,
missing external audit receipts or any live broker host fail closed. Parallel strategy
research remains allowed, but promotion requires a new ADR and explicit owner approval.
The retained ORB/autonomous libraries support historical tests and review only: their
CLI entrypoints and the desktop Alpaca autopilot are permanently blocked, and the old
Compose executor and PowerShell launcher are removed.
