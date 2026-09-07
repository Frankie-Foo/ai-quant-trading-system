# AI-Quant review remediation implementation plan

> Worker guidance: execute independent slices in parallel with test-first changes; integrate centrally. The named superpowers execution skills are not installed, so use the existing tools, TDD skill, and verification-before-completion checks instead. No production activation or broker requests.

**Goal:** Correct the reviewed execution failures and separate factual execution performance from research counterfactuals without weakening Paper safety controls.

**Architecture:** Preserve the existing funnel, state store, broker adapter and deterministic strategy. Add small shared seams where order recovery and current-time signals need independent verification. Work on isolated branches; never mutate production ledgers, immutable past artifacts, credentials or task definitions during implementation.

**Stack:** Python, existing Polars/Pydantic/httpx, SQLite, pytest, Ruff and mypy.

## Scope and ownership

- Main: scripts/monitor_modern_momentum_paper.py, operations/paper_state.py, operations/paper_runtime_policy.py, operations/runtime_alerts.py; integration tests and documentation.
- Scheduler worker: schedule/modern_funnel.py, scripts/run_modern_funnel_stage.py, operations/feishu_base.py and their tests.
- Strategy worker: research/modern_momentum.py, research/modern_momentum_forward.py, scripts/run_current_modern_backtest.py, scripts/monitor_modern_momentum_forward.py and focused tests.
- Loop worker (separate branch/worktree): operations/loop_integration, related tests, scripts/build_loop_review.py or actual entrypoint. Never import a different strategy's risk policy under Modern H15 identity.

## Task 1: scheduler handoff and publication

- [x] RED: through run_tick and a boundary executor, return an eligible pool with paper_started=false; assert the result is not terminal succeeded and a subsequent valid attempt can repair handoff.
- [x] GREEN: require positive execution acknowledgement, retain immutable selection/publication receipts, supervise execution separately from selection within declared limits.
- [x] RED/GREEN: bounded publication retries retain structured redacted CLI errors; never place orders before receipts. Existing failed windows remain auditable, never fabricate a missed stage or replay a historical entry.
- [x] Run pytest tests/test_schedule_modern_funnel.py tests/test_modern_funnel_stage.py tests/test_feishu_base.py.

## Task 2: order lifecycle

- [x] RED: fake broker leaves flatten order active for two ticks; assert it is not canceled/recreated. On confirmed canceled terminal, assert fresh persisted attempt ID and reconciled residual quantity.
- [x] GREEN: shared recovery logic handles entry/exit partial fills and terminal statuses, protecting actual filled quantity and never selling more than verified holdings.
- [x] RED/GREEN: recovering an unsubmitted entry after cutoff, while frozen, or with stale quote produces zero submits. Existing broker orders are reconciled, not duplicated.
- [x] RED/GREEN: one unbuyable symbol produces a structured rejection and does not freeze other candidates. Genuine transport/reconciliation faults retain freeze behavior.
- [x] RED/GREEN: missing/nonfinite/over100 smoke cap fails at the execution entrypoint; prior-day owned positions require recovery, unknown positions remain blocked.
- [x] Run focused Paper lifecycle/broker/state/risk tests with only fake external boundaries.

## Task 3: deterministic signal parity

- [x] RED: sequential completed-bar inputs return only current actionable signals, not the day's already exited first trade; signal evaluation needs no future fill bar.
- [x] GREEN: separate latest signal from fill/exit simulation; runtime exits use actual persisted position state.
- [x] RED/GREEN: first/reentry obey one declared cutoff, spread and cost policy; default entry cutoff 15:00 ET, flatten 15:50 ET; retain >=$1B, +4%, RVOL, long-only and all-in2% gates.
- [x] RED/GREEN: manifest reports actual modern parameters and hash, independent of legacy RVOL policy label.
- [x] Recalculate available historical trade eligibility without overwriting old evidence; do not call reused holdouts blind or claim improved returns.

## Task 4: factual Loop feedback

- [x] RED/GREEN: accepted research verdict without fills never creates realized trading PnL. Distinguish counterfactual market outcomes from actual fills and no-trade reasons.
- [x] RED/GREEN: actual performance requires complete verified fill lineage, quantity/cost availability; unknown fees remain unknown, not zero.
- [x] RED/GREEN: risk description derives from the effective Modern H15 plan, not generic ATR config; include full frozen pool selection observations separately from post-close winners.
- [x] Run offline Loop tests; no upload and no rewrite of delivered events.

## Task 5: integration and release boundary

- [x] Review worker patches and run cross-module tests, Ruff and strict mypy; run the full offline suite when safe.
- [x] Document exact passing counts, pre-existing failures, current evidence invalidations and remaining operator checks in PROGRESS.md and remediation status.
- [x] Keep production branch, task arguments, risk cap, kill switch, freeze state and broker unchanged. No commit/push unless requested. A smoke deployment is a separate explicitly observed acceptance step, not inferred from unit test success.

## Acceptance examples

```python
assert not (receipt['symbols'] and receipt['paper_started'] is False and stage_terminal)
assert broker.submitted_after_entry_cutoff == []
assert broker.sell_quantity <= broker.verified_long_quantity
assert existing_live_flatten_order not in broker.canceled_orders
assert unfilled_acceptance.realized_pnl is None
```

These express invariants; runnable tests must exercise the public tick/signal/reconciliation interfaces with literal independent expected values, not assertions derived from the implementation itself.

## Delivery boundary

These checkboxes cover the local implementation and offline tests above, not production activation or evidence that the strategy is profitable. Runtime: 911 tests, Ruff, strict mypy (444 files), compileall and diff check passed. Loop: 826 tests; integration-directory Ruff and strict mypy (13 files) passed. Both branches are uncommitted.

Still outstanding: approved Paper smoke acceptance and release; the production Loop context/fill evidence provider and scheduled factual Outcome integration; remote contract acceptance; corrected performance replay and new out-of-sample evaluation; a causal comparison before changing the 09:35 exclusion policy. See PROGRESS.md M90 and docs/REVIEW_REMEDIATION_2026-09-07.md. No live orders, production mutations, unfreeze or risk-cap changes were authorized or performed.
