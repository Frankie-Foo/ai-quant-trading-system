# Changelog

## Unreleased

- Consolidated automated trading into one Modern H15 Alpaca Paper path.
- Added durable ET/XNYS three-stage funnel execution with immutable receipts.
- Added protected bracket entries, restart reconciliation, Outbox delivery and runtime
  fault escalation.
- Enforced 09:56–15:00 entry time, 15:45 cancellation and 15:50 flattening.
- Added dedicated Feishu/Livermore authorization, UTF-8 checks and a release-level
  $100 Paper notional cap.
- Removed obsolete single-symbol, dated H30 and alternate order-capable monitors.
- Hard-retired the legacy ORB/autonomous CLIs and desktop Alpaca autopilot; removed the
  old Compose executor and PowerShell launcher.
- Made full pytest, Ruff and strict Mypy checks green on the hardening branch.
- Made Windows funnel installation work across Git worktrees by explicitly pinning the
  Python interpreter, machine environment file and shared data root; installed the
  one-minute Paper-only funnel and retired the duplicate Codex heartbeat.
