# B5/B6 implementation and verification

## Resumed production wiring plan (2026-09-07)

The renewed user scope includes `schedule/postmarket.py` and its Loop tests.
Earlier unimplemented-provider statements below describe the previous handoff.
Use existing models, stdlib and injected read-only broker boundaries; never read
secrets, call live services, submit orders, modify other worktrees or commit.

- [x] Test scheduler through `run`: a zero-exit blocked receipt stays pending;
  a previously successful local job retries Loop without rebuilding local artifacts.
  Change `schedule/postmarket.py`, review/outcome CLI receipts and Loop tests.
- [x] Test native context exporter with pinned plan, confirmation and full first
  pool; missing/conflicting facts reject and unadjudicated rows keep null verdict.
  Reuse `execution_summary.py` models; add a local stdout exporter script.
- [x] Test injected broker FILL activity/order reads, exact strategy ownership,
  duplicate execution IDs, cumulative snapshots and unknown costs. Preserve raw
  broker facts; local order intent establishes ownership only, never execution.
- [x] Wire explicit pinned evidence through scheduled review and due Outcomes;
  test date/strategy joins and business receipts with synthetic fixtures only.
- [x] Run focused pytest, ruff, mypy and diff whitespace checks. Record exact
  results, runner inputs and remaining real-session/backend acceptance evidence.

Each boundary is user-requested: first add its failing test, run it, implement
the minimal slice and rerun before proceeding. Evidence belongs here rather than
the out-of-scope root PROGRESS file. No commit step is authorized.

Scope: `operations/loop_integration/`, Loop review scripts and Loop tests only.
Base: `990f896`; offline, no orders, no uploads, no commits. Existing production
configuration, tasks, legacy Base and delivered artifacts remain untouched.

## Plan

Use ponytail and test-first at the user-specified public boundaries. No new
dependencies or framework. The review/fill contract consumes explicit local,
hash-pinned evidence; it never discovers a live broker or assumes fills from
order intents. Existing modern exports missing effective risk fields are not
silently upgraded. This file replaces the out-of-scope root PROGRESS update.

- [x] Outcome: reproduce accept-without-fills; retain compatible v2 research
  fields with explicit counterfactual semantics and unavailable factual PnL.
  Verify through `sync_due_outcomes` in `tests/test_loop_integration.py`.
- [x] Review: reproduce generic-config risk leakage; validate effective modern
  plan hash/provenance and fail submission before network when unavailable.
  Separate the complete frozen pool from the after-close winner population.
- [x] Factual execution: add a local JSON-to-stdout entrypoint and tests for
  confirmed fill lineage, partial fills, unknown fees and mismatched inputs.
  Compute only matched long-only fill PnL, never winner-derived selected returns.
- [x] Run focused pytest, ruff and mypy; record exact red/green evidence and
  remaining upstream/downstream integration requirements here.

## Validation commands

Use `D:/cdoeX-work/.venv/Scripts/python.exe` with `PYTHONDONTWRITEBYTECODE=1`.
Run `python -m pytest tests/test_loop_integration.py tests/test_loop_execution.py
-q -p no:cacheprovider`; run `python -m ruff check --no-cache` and
`python -m mypy --follow-imports=silent --cache-dir=...` on changed Loop Python
files. Test and type-check temporary files stay under the Loop test directory.

## Evidence

Initial system Python could not collect tests (`No module named 'polars'`) and
had no ruff/mypy. This is an environment check, not a regression red result.
An initial pytest temporary-directory setup also failed before the test body;
subsequent runs use `tests/.pytest_cache/loop/` with distinct basetemp directories.

## Red/green evidence

All fixtures are synthetic unit-test data, not strategy-performance evidence.

| Boundary | Observed red | Green |
| --- | --- | --- |
| accept delayed Outcome | `KeyError: 'performance_kind'` | Original 14 tests pass with explicit counterfactual assertions |
| absent effective plan | `KeyError: 'status'` | Risk unavailable, no execution PnL, submission blocked before request |
| explicit plan and morning pool | unexpected `effective_plan_path` | 17 focused tests pass |
| confirmed empty ledger | unexpected `fills_path` | `no_trade`, no actual PnL; order intents rejected |
| partial fills / unknown fees | expected `partial_fills`, got `unavailable` | FIFO matched 5 shares, gross 90 USD; net null when fees unknown |
| local CLI | module did not exist | stdout JSON; input bytes and directory contents unchanged |
| broker lineage | same order id with different sides did not raise | Rejected; duplicate ids, bad dates/hash and oversells also covered |
| cumulative snapshot contract | missing explicit incremental contract | `broker_order_cumulative` rejected; never summed |
| factual Outcome link | `attach_factual_execution` absent | Date/hash/instrument join, new artifact id, original unchanged |
| review fill integration | unexpected `fill_evidence_path` | Actual summary carried independently of winner metrics; 15-member pool preserved |

Focused suite: **51 passed**. Ruff passes across the entire Loop integration
directory and the four changed script/test files. Strict mypy with
`--follow-imports=silent` passes for **9 changed Python files**.

The broader initial mypy run over the entire Loop directory also found three
pre-existing errors in `control_plane.py`: missing annotations at the original
lines 48 and 87 and an Any return at the original line 73. The Loop worker left
them unchanged; **Main subsequently corrected all three with type annotations
only**, without behavioral changes. See Main's final checks below.
No subagent review tool was available to the Loop worker; its local self-review
was not independent acceptance. Main subsequently performed independent checks.

## Explicit offline input contract

`scripts.summarize_loop_execution` reads only named local files and prints JSON.
Required flags: `--plan`, `--plan-sha256`, `--trade-date`, `--as-of` (UTC).
Optional paired flags: `--fills`, `--fills-sha256`. Missing fills are unavailable.
It does not load env, discover evidence, initialize ledgers, upload or call brokers.

1. `loop_effective_modern_plan.v1` requires a strategy object containing id,
   version, active-policy hash, effective parameters and explicit risk policy.
   Risk fields are `symbol_risk_fraction`, `maximum_all_in_stop_pct`,
   `new_entry_cutoff_et`, `flatten_et`, `attempt_weights`. No defaults are supplied.
   Current contract supports the reviewed regular-session 15:00/15:50 and 60/40
   policy, with maximum symbol risk 0.5% and all-in stop 2%.
2. `strategy_sha256` is SHA256 of canonical JSON for the entire strategy object
   (`sort_keys=True`, compact separators, JSON-mode types). The separate externally
   supplied plan SHA256 pins exact file bytes. Provenance requires source snapshot
   ids and separate UTC effective/available/cutoff times. Only the morning pool's
   `candidate_pool_available_at_utc` must be at/before its selection cutoff.
   Risk-plan effective/available times may be later (e.g. 09:35 after a 09:25 pool),
   but must not exceed review as_of or any actual fill they explain. The review
   additionally checks date, pool cutoff and active-policy hash against its inputs.
3. `candidates` is the **complete frozen morning pool**, including rejected/watch
   candidates and their original additional fields, not the winners or only filled
   symbols. `candidate_pool_source=complete_frozen_morning_pool` and
   `candidate_pool_complete=true` attest this. Explicit `candidates=null` and
   completeness false mean unavailable; never substitute post-close winners.
4. `loop_broker_fills.v1` requires `evidence_kind=broker_confirmed_fills` and
   `quantity_semantics=incremental_execution`, matching trade date, strategy and
   plan hashes, broker/account/environment/currency, source, validated-by identity,
   UTC coverage, flat opening inventory and reconciliation/cost completeness.
   Every incremental event retains fill id, broker order id, symbol/side, UTC
   execution time, quantity, price, source and broker-confirmation assertion.
   Known fees, including zero, require a fee source. Unknown fees remain null.
5. Local validation proves integrity and contract consistency, **not independent
   broker authenticity**. External verified broker exports remain authoritative.
   Complete reconciliation must cover the whole XNYS session. No complete evidence
   means no factual PnL; complete empty evidence means no_trade. Realized PnL uses
   FIFO matched long fills; open quantities are retained, never marked with winner
   returns. Entry fees are allocated proportionally to matched quantity; execution
   prices already incorporate execution-price effects. No synthetic slippage or
   portfolio-return denominator is added. Current contract is USD, flat-opening,
   same-day long-only; unsupported inventory/currency is rejected.

## Remaining integration / acceptance requirements

- Main's new `state.fill_observations` entries keyed by
  `fill:{brokerid}:{cumulative_qty}` hold `BrokerOrder` cumulative snapshots
  (`filled_qty`, `filled_avg_price`, `filled_at`, side, observation time). **No
  converter was added.** Do not pass these as increments or sum same-order
  cumulative quantities. A separately reviewed adapter must reconcile them with
  authoritative broker executions, handle repeated/corrected snapshots, preserve
  original sources and unknown costs, and produce validated increments if possible.
- Native `modern_h15_paper_plan.v1` is now accepted directly with an explicit
  hash-pinned `--review-context` sidecar using the existing effective-plan model,
  not another model or a rewritten native artifact. The sidecar must reference
  `native_plan_sha256`. The adapter verifies manifest config SHA256, exact full
  parameters, effective manifest strategy version, and every risk-policy field
  against native facts. It also cross-checks native all-in stop, target and maximum
  entry spread against manifest effective_config. Static authorization version
  `modern-h15.v1` is recorded separately from the effective strategy version.
  Native plans missing
  context are rejected; fields missing from native exports are never defaulted.
  Half-day/session-specific policies require a reviewed extension.
- `sync_loop_daily_review` accepts explicit plan/fill paths and hashes. It records
  a local blocked precondition on submission when risk is unavailable. `--stage-only`
  still saves research-only evidence with explicit risk status and submitted=false.
  Existing scheduled tasks
  were not changed. Missing pool is separately unavailable; remote acceptance of
  an empty rescan universe is not asserted.
- Outcome v2 wire `strategy_return`, excess/drawdown and cost fields remain
  compatibility **market-counterfactual research aliases**, never intraday realized
  PnL. Downstream consumers must honor the added semantics; the old top10 metrics
  and `portfolio_pnl_available=false` were retained.
- Outcome assignments may provide `strategy_sha256` for factual joins.
  `attach_factual_execution` reads pinned local inputs and returns a new Outcome
  with per-instrument factual performance and original broker evidence. It rejects
  missing/mismatched strategy identity. This opt-in helper is **not automatically
  wired into scheduled `sync_due_outcomes`**; Main must decide that integration.
- New review/outcome identities avoid rewriting old delivered artifacts. No
  remote Loop schema/endpoint compatibility, live data or Paper behavior was tested.
  No production tasks, env, legacy Base, delivered artifacts or other modules changed.

## Changed files

- `operations/loop_integration/contracts.py`: document counterfactual alias; optional assignment hash.
- `operations/loop_integration/outcome_reporter.py`: research semantics and read-only factual join.
- `operations/loop_integration/execution_summary.py`: pinned plan/fill contracts and factual summary.
- `operations/loop_integration/review_builder.py`: effective risk and separate evidence populations.
- `operations/loop_integration/client.py`: missing-risk submission guard and separate payload fields.
- `operations/loop_integration/control_plane.py`: Main's annotation-only follow-up (`Self`, tuple return type, typed local dict); no behavior change.
- `scripts/summarize_loop_execution.py`: local read-only JSON entrypoint.
- `scripts/sync_loop_daily_review.py`: explicit input flags and missing-risk precondition.
- `tests/test_loop_execution.py`, `tests/test_loop_integration.py`: focused regression tests.
- `operations/loop_integration/REVIEW_FIXES.md`: plan, contracts, evidence and handoff.

## Native producer-to-review chain (integration review correction)

The initial constraint `risk available <= morning selection cutoff` was incorrect
and has been removed. The added native-plan test is red/green: the initial loader
rejected native schema; the adapter accepts 09:35 risk plus 09:25 morning pool and
rejects a 09:34 fill. The existing CLI is tested end to end with these files,
including local outbox payload and unchanged input bytes. A second red/green test
restores no-plan `--stage-only` research staging without claiming remote submission.

Data providers must supply:

1. **Native final plan:** the unchanged `modern_h15_paper_plan.v1` file emitted
   by the modern funnel, plus its exact byte SHA256.
2. **Review context sidecar:** reuse `loop_effective_modern_plan.v1` with
   `native_plan_sha256` set to that native hash. Populate the strategy's existing
   stop/time/target fields from the native plan; validate agreement, do not silently
   override them. Obtain the actual 09:35 plan availability from frozen
   `open_confirmation.v1.generated_at_utc`, verifying its authorization config hash
   matches the native plan. Obtain the full 09:25 morning pool and its actual
   availability from the frozen morning selection snapshot, not final survivors.
   Carry these original source ids. Capture missing symbol-risk/attempt weights
   and effective time from verified effective runtime/owner-approved plan evidence;
   if no such evidence exists, submission remains unavailable. Compute the
   strategy hash from the complete effective strategy object and pin the sidecar
   bytes separately. The provider must not substitute generic kernel defaults.
3. **Fills:** authoritative verified incremental broker export, with the native
   plan hash and the context's strategy hash. No Main cumulative-state conversion
   is implemented or inferred in this branch.
4. **Existing CLI:** pass `--effective-plan` / `--effective-plan-sha256`,
   `--review-context` / `--review-context-sha256`, and optional
   `--fill-evidence` / `--fill-evidence-sha256` to `scripts.sync_loop_daily_review`.
   Existing binding/date/data-root/active-policy arguments remain unchanged.
   First use `--stage-only` to inspect the saved local evidence. The independent
   summary CLI accepts the same context flags with its existing `--plan`/`--fills`
   names; the factual Outcome attachment helper also accepts the context pair.

This branch implements the native-file adapter and CLI plumbing, but **does not
deploy or invent the production evidence exporter**. Main must supply/review that
provider and scheduled invocation; a frozen native file alone lacks sufficient
facts for full risk submission. Network/backend acceptance is still untested.

## Final strict-manifest review round

Main's updated native producer was inspected read-only in the review-fixes
worktree. Its explicit symbol risk, attempt weights, entry spread and
`modern_strategy_manifest.v1` are now mandatory native evidence for risk submission.

- New red evidence: six native-adapter regressions failed with `DID NOT RAISE`
  for conflicting symbol risk, spread, full parameters, manifest version, config
  hash, and missing manifest. All six now pass.
- A standalone sidecar previously produced risk `available`; its regression now
  passes with risk `unavailable`. Review generation requires verified native
  evidence; missing native facts are caught as unavailable, while conflicting
  facts remain hard errors. Legacy normalized files remain usable for research
  and factual-fill bookkeeping, but cannot enable risk submission.
- The native manifest's `effective_config` must equal context strategy parameters
  exactly; its canonical hash is recomputed. Manifest version, not the legacy
  authorization label, defines strategy identity. Native risk-policy fields must
  exactly match context risk; entry spread is verified against effective_config
  and carried separately in review liquidity constraints.
- **Context exporter remains unimplemented**, per the final request to stop
  expansion. Its exploratory failing test was removed, not shipped as an xfail
  or an incomplete feature. No new exporter CLI or production provider is claimed.
- Future minimal exporter: read pinned native plan, pinned confirmation, full
  frozen first-wave pool and matching active hash; reuse confirmation validation;
  verify confirmation config hash, identity/date and first-pool active hash;
  derive risk/parameters exclusively from native+manifest and availability from
  the confirmation; keep pool availability separate; print existing context JSON
  to stdout. Missing original risk, manifest, receipt or pool evidence must hard
  reject. No fabricated default verdicts should be added to unadjudicated pool rows.

Final acceptance boundaries: production context exporter/adapter, independent
confirmation/pool provenance verification, scheduled invocation, cumulative
snapshot-to-incremental conversion, actual broker-fill integration, automatic
Outcome attachment and remote Loop acceptance all remain for Main. Only the
explicit offline contracts, local CLI/outbox path and synthetic regression suite
were exercised here. No commit, network/upload, orders or production mutation.

## Main final checks (reported by Main)

Main independently reported the following final verification results:

- Full Loop suite: **826 passed in 51.55 seconds**.
- Expanded static-check scope: the complete Loop directory plus the four
  script/test files, **13 Python files** total; **mypy passes**.
- Main fixed the three previously recorded `control_plane.py` typing errors
  using `Self`, an explicit tuple return annotation and a local `dict` annotation.
  These are type-annotation-only changes, with no behavioral change. The earlier
  statement that these three errors remained outstanding is now superseded.
- Corrected ruff chronology: the initial run found an **E501, 101-character
  annotation line**; the earlier green report was premature. Main wrapped that
  line and actually reran ruff: **passed**. This latest result is authoritative.
- After that formatting correction, Main reran the focused suite: **51 passed**.

These are Main-provided independent results, not a rerun by the Loop worker in
this documentation-only update. No code was changed during this update.

**The production provider/context exporter remains unimplemented.** These test
and static-check results do not imply production-provider delivery, cumulative
snapshot conversion, actual broker-fill integration or remote Loop acceptance;
those previously documented integration boundaries remain outstanding.

## Resumed wiring handoff: mergeable plumbing, NOT a production closed loop

This section supersedes the earlier statement that no exporter exists. It does
not supersede the requirement for authentic upstream evidence. No deployment,
live API request, secret read, order, upload or git commit was performed here.

### Implemented and exercised with no-secret fixtures

- `execution_summary.export_native_context` and `scripts.export_loop_context`
  read byte-pinned native plan, open confirmation and complete first-wave pool.
  They verify native manifest/risk, confirmation identity/config hash, final
  authorized symbols as a subset of the separate complete pool, active hash and
  actual availability. Unadjudicated pool verdicts remain null. Native 09:56
  entry-after also constrains execution availability; no historical defaults.
- `broker_fills.collect_broker_fills` reads an in-memory copy of a pinned,
  checkpointed native `paper_orders` SQLite export. It matches exact client and
  broker IDs, verifies actual order symbol/side and broker-confirmed bracket legs,
  excludes manual orders even on the same symbol, and retains raw activity/order
  facts. Unacknowledged intents cannot establish complete reconciliation.
- `scripts.export_loop_broker_fills --read-paper-broker` is a real read-only HTTP
  adapter, not merely a callback requirement. It uses `alpaca_paper_credentials`
  on the inherited environment, a fixed `https://paper-api.alpaca.markets`, GET
  only, no redirects and no .env discovery. Reads account identity/currency,
  `/v2/account/activities/FILL` and `/v2/orders/{id}?nested=true`.
  Pages use date, ascending direction, page_size=100, and the last activity ID
  as page_token until the final page. Equal execution IDs deduplicate; conflicting
  corrections or repeated cursors fail. Incremental `qty`, never `cum_qty`, feeds
  PnL. Cumulative `filled_qty` is only a reconciliation check, including empty-page
  contradictions. No `state.fill_observations` conversion was invented.
- `--output-dir` writes independent content-addressed raw audit and normalized
  evidence plus a pinned pairing receipt. No existing file is overwritten.
  Mutable account balances change audit evidence but not execution identity.
  Unknown fees remain null and force costs_complete=false; no zero-cost default.
- `scripts.prepare_loop_execution` actually invokes the context and broker
  exporters, persists their outputs and emits a pinned index. It does not require
  Main to implement `read_fill_page`. Its upstream inputs are still externally
  supplied (see the blocking boundary below).
- `schedule.postmarket` can invoke that preparation CLI before review/outcomes.
  Review requires a delivered business receipt with task/run IDs; blocked,
  audit-only, staged, malformed stdout and zero exit alone are not success.
  Outcome requires an explicit completed receipt and matching due/delivered
  counts; only not-yet-mature horizons may remain pending. Successful local jobs
  retain their artifacts and retry failed Loop handoffs on later ticks. Missing
  provider evidence remains blocked before remote task creation. Already delivered
  review outbox items return their stored receipt without a new task submission.
- Both sync CLIs accept `--execution-index` and `--execution-index-sha256`.
  Due Outcomes join by decision date AND exact strategy hash before staging;
  missing indexed evidence stays pending, not a wrong-day/wrong-strategy fallback.
  Research counterfactual values are unchanged by factual attachment.

### Actual producer blockers: nobody generates these automatically yet

There is **no daily native discovery / prior-evidence producer in this change**.
The preparation CLI consumes a pinned JSON config; it does not create the
upstream native plan, confirmation, morning pool, ledger backup or reconciliation
attestation. In particular:

1. `opening_positions_flat` and `reconciled_complete` are still input-contract
   assertions, not independently derived truth. The adapter checks pagination,
   known-order totals, dates, ownership consistency and account identity, but
   cannot prove the ledger includes every strategy order or historical opening
   inventory. It cannot turn today's zero positions into a historical flat open.
2. The legacy native SQLite ledger has date/client/order identities but no
   plan/strategy hash. A real producer must bind its frozen backup to the run's
   native plan, authorization and account. Passing matching metadata hashes is
   not independent verification of that run relationship.
3. Required source evidence is an immutable pre-session strategy inventory/account
   observation and a complete end-of-session order/activity reconciliation,
   including broker-generated legs, run ownership and coverage timestamps.
   No such real evidence was supplied here. Do not hand-fill flat/complete=true.
   Non-flat opening inventory remains unsupported by the existing FIFO contract.
4. Per Main, historical 2026-09-04 has only first-wave pool, no confirmation/new
   native manifest. It stays blocked; do not synthesize an effective plan or
   authorization for a remote acceptance test. 2026-09-07 is an XNYS holiday;
   no actual-fill acceptance or no-trade session reconciliation was claimed.
5. A future zero-candidate day may legitimately have no native authorized plan.
   This branch can stage unavailable-risk research locally but has **no verified
   remote research-only/no-trade contract** independent of order authorization.
   That producer/contract integration remains unresolved, not silently defaulted.
6. Real Loop endpoint acceptance, active v6 binding and next-session producer
   integration remain Main's acceptance tasks. Main reported sync was disabled
   by `AI_QUANT_LOOP_SYNC_ENABLED=false`; this worker did not inspect/change the
   machine environment. Main owns enabling sync, capital changes and deployment.

Thus this is mergeable **read-only adapter and scheduler plumbing**, not a claim
of completed production Loop closure. Blocked historical retries do not create
new remote tasks; they remain observable failures. Immutable input/config changes
produce new evidence identities intentionally.

### Runner interface and required inputs

Local context stdout:

```text
python -m scripts.export_loop_context --plan PLAN --plan-sha256 SHA
  --confirmation RECEIPT --confirmation-sha256 SHA
  --first-pool POOL --first-pool-sha256 SHA --active-policy-hash HASH
  --selection-cutoff-utc UTC --as-of UTC
```

Actual read-only Paper export (implemented, NOT run against a service here):

```text
python -m scripts.export_loop_broker_fills --read-paper-broker
  --plan PLAN --plan-sha256 SHA --review-context CONTEXT --review-context-sha256 SHA
  --ledger FROZEN_SQLITE --ledger-sha256 SHA --trade-date YYYY-MM-DD --as-of UTC
  --metadata METADATA --metadata-sha256 SHA --output-dir OUTPUT
```

METADATA is the existing `loop_broker_fills.v1` envelope without fills: schema,
evidence_kind, quantity_semantics, date, strategy/plan hashes, broker=alpaca,
account_id, environment=paper, currency=USD, source, validated_by,
coverage_start_utc, coverage_end_utc, opening_positions_flat,
reconciled_complete and costs_complete. Those fields must originate in real
evidence; this CLI supplies no defaults for inventory or completeness. Credentials
come from supported Paper environment aliases, never CLI values or output.
An alternative `--broker-export PATH --broker-export-sha256 SHA` consumes pinned
offline JSON containing metadata, pages (`page_token`, `activities`,
`next_page_token`) and orders keyed by broker order ID.

Preparation CLI for postclose:

```text
python -m scripts.prepare_loop_execution --trade-date YYYY-MM-DD
  --config PROVIDER_JSON --config-sha256 SHA
```

PROVIDER_JSON keys (all evidence paths are relative to the config or absolute):

- `context`: `plan_path`, `plan_sha256`, `confirmation_path`,
  `confirmation_sha256`, `first_pool_path`, `first_pool_sha256`,
  `active_policy_hash`, `selection_cutoff_utc`, `as_of`.
- `ledger_path`, `ledger_sha256`, `output_dir`.
- Explicit `read_paper_broker: true` plus `metadata_path`, `metadata_sha256`, OR
  `broker_export_path`, `broker_export_sha256` for pinned offline input.
- Optional `prior_execution_index_path`, `prior_execution_index_sha256` retain
  previous dates for delayed Outcomes. The old index is never overwritten.

The prepared receipt carries `execution_index_path`, `execution_index_sha256`,
raw audit/fill/receipt paths and hashes. Pass the index pair to both existing sync
CLIs. It contains `executions` entries with trade_date, strategy_sha256,
plan_path/hash, fills_path/hash and review_context_path/hash.

For automatic invocation, set `AI_QUANT_LOOP_PROVIDER_CONFIG_FILE` and
`AI_QUANT_LOOP_PROVIDER_CONFIG_SHA256` along with the existing sync enable,
binding, active-policy and approved Outcome config settings. Do not also set
`AI_QUANT_LOOP_EXECUTION_INDEX_FILE/SHA256`; that pair is the alternative for
preproduced inputs. Each config is date-specific. Provisioning the daily pinned
config and truthful source evidence is the unresolved producer task, not something
these environment switches solve.

### Current-turn red/green and changed files

Observed regression reds: local succeeded job returned 0 without retry;
context exporter absent; broker adapter absent; empty pages failed to reject
nonzero broker cumulative fills; broker-generated exits were excluded; Outcome
index argument and review CLI index flags absent; Paper output-dir/preparation
entrypoints absent; scheduled provider was not called; mutable account audit
changed the normalized fill hash. Each received a fixture green after its fix.
Test setup errors (missing fixture policy, logger capture, WAL fixture and helper
name) were corrected and are not counted as implementation red evidence.

Current additions: `operations/loop_integration/broker_fills.py`,
`scripts/export_loop_context.py`, `scripts/export_loop_broker_fills.py`,
`scripts/prepare_loop_execution.py`, `tests/test_loop_providers.py`,
`tests/test_loop_postmarket.py`. Current modified files:
`execution_summary.py`, `outcome_reporter.py`, `scripts/sync_loop_daily_review.py`,
`scripts/sync_loop_due_outcomes.py`, `schedule/postmarket.py`,
`tests/test_loop_integration.py`, and this document. Earlier uncommitted fixes
remain preserved. `_opportunity` now returns `tuple[Path, DatasetSnapshot]`;
obsolete snapshot ignore and indexed-evidence ignore were removed. Main's three
`control_plane.py` annotation repairs remain intact.

Final worker verification, after the last code change: **74 passed in 9.84s**;
ruff **All checks passed**; mypy **21 source files, no issues**; `git diff --check`
passes. These are local fixture results, not remote or actual-session acceptance.
No full repository test claim is made; Main will validate the merged release.

Reproduction (PowerShell, from this worktree):

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
$python = 'D:/cdoeX-work/.venv/Scripts/python.exe'
& $python -m pytest tests/test_loop_postmarket.py tests/test_loop_execution.py tests/test_loop_integration.py tests/test_loop_providers.py tests/test_postmarket_learning.py -q -p no:cacheprovider --basetemp=tests/.pytest_cache/loop/wiring-main-review
$checkFiles = @('operations/loop_integration', 'scripts/summarize_loop_execution.py', 'scripts/sync_loop_daily_review.py', 'scripts/sync_loop_due_outcomes.py', 'scripts/export_loop_context.py', 'scripts/export_loop_broker_fills.py', 'scripts/prepare_loop_execution.py', 'schedule/postmarket.py', 'tests/test_loop_execution.py', 'tests/test_loop_integration.py', 'tests/test_loop_postmarket.py', 'tests/test_loop_providers.py')
& $python -m ruff check --no-cache @checkFiles
& $python -m mypy --follow-imports=silent --cache-dir=tests/.mypy_cache/loop @checkFiles
git -c core.safecrlf=false diff --check
```

## Final native daily producer round (supersedes missing-daily-producer blocker)

`operations/loop_integration/daily_provider.py` and `scripts/produce_loop_daily.py`
now implement native discovery and real read-only Paper reconciliation. No daily
handwritten metadata, config hashes, flat flags, completeness flags or prior-index
arguments are required in native mode. Existing explicit-export CLI mode remains
compatible, but native daily mode derives its metadata from the checks below.

### Main startup producer contract actually inspected

Main's `operations/paper_run_evidence.py` was read-only inspected in the
`ai-quant-review-fixes` worktree; it was not edited or imported into this worktree.
The implemented input is **`paper_run_startup.v1`**, not the earlier proposed
`modern_h15_startup.v1`. Files are `startup-{exact-byte-sha256}.json` under
`RUN_ROOT/modern-momentum/YYYY-MM-DD/` and contain:

```text
schema_version, trade_date, broker="alpaca", environment="paper",
broker_base_url="https://paper-api.alpaca.markets",
observed_start_utc, observed_end_utc,
account (including actual id and currency), positions[], open_orders[],
ledger_path, plan_path, plan_sha256, confirmation_path, confirmation_sha256
```

The monitor writes these facts before broker writes. Missing id/currency may be
recorded by Main without weakening its trading protections, but is invalid for
Loop reconciliation. No flat or reconciled declaration is consumed.

### Discovery, evidence and fail-closed boundaries

- The caller supplies explicit `RUN_ROOT` (the `runs` directory) and date. Native
  files are discovered only under `autonomous/DATE/first_wave_pool.json`,
  `modern_h15_paper_plan.json`, `open_confirmation.json`, plus
  `modern-momentum/DATE/paper-state.sqlite3` and `startup-*.json`. Escaping paths,
  missing files, missing manifests and hash/authorization conflicts block.
- All startup hashes and identities are checked and retained. Every restart must
  match account/plan/confirmation/ledger identity. The earliest observation is
  the baseline; a later empty restart cannot replace an earlier non-flat baseline.
  Invalid or missing baseline evidence blocks before a real broker read.
- SQLite's read-only connection and backup API capture committed WAL into a new
  content-addressed copy; there is no source checkpoint or source database write.
  A second backup after broker reconciliation must match, otherwise retry.
- The fixed Paper GET adapter paginates **all account activity types** for the day,
  not just `/FILL`. Any FILL at/before the earliest startup read end blocks the
  opening-flat inference. **All non-FILL activity types conservatively block**;
  no cash/transfer/corporate-action whitelist was guessed. Combined with a complete
  empty startup position/order snapshot, this proves the accepted flat baseline.
- Incremental FILL IDs deduplicate and retain source facts; conflicts, quantity
  mismatch versus actual parent/leg cumulative fills, unresolved intent, unknown
  ownership, foreign orders, nonempty final account positions/open orders,
  inventory imbalance or changing ledger block. Unknown/manual activity is not
  attributed to the strategy and can no longer become confirmed_empty/no_trade.
- Runtime `status='aborted', broker_order_id=None` means proven pre-POST refusal:
  it is skipped for broker-id reads and is not unresolved. Plain intent with no
  broker ID still blocks; any actual FILL attributed to an aborted intent rejects.
- The day-order query requests all status orders with nested legs and limit 500;
  hitting that ceiling **blocks**, never silently asserts completeness. Activity
  pagination continues by last activity ID. This deliberate bounded order scan
  supports the low-volume runtime without inventing ambiguous timestamp paging.
- The current daily producer accepts only flat final accounts with no external
  trading. Partial/open inventory can still be represented by the lower-level
  explicit fill contract, but does not pass this conservative daily producer.
  Unknown fees remain null/net PnL unavailable. FIFO validation runs before the
  daily index is published. All performance test values remain synthetic fixtures.
- The raw broker audit is persisted independently, including on reconciliation
  failure after reads. Successful outputs are content-addressed context, ledger,
  fills, index and pairing receipt. There are no orders or remote Loop writes in
  the daily producer itself; sync CLIs remain the separate delivery boundary.

### Static scheduler settings, no daily hash provisioning

Set **`AI_QUANT_LOOP_NATIVE_RUN_ROOT`** to the explicit `runs` directory, alongside
the existing `AI_QUANT_ACTIVE_POLICY_FILE`, `AI_QUANT_LOOP_BINDING_FILE` and enabled
review flag. Do not also configure the older provider-config or execution-index
pair for the daily review path. `schedule.postmarket` passes the date/data root
and active-policy path to the actual CLI:

```text
python -m scripts.produce_loop_daily --run-root RUN_ROOT --trade-date YYYY-MM-DD
  --active-policy ACTIVE_POLICY --data-root DATA_ROOT
```

Selection cutoff comes from the usable accepted postmortem for that date; it is
never a guessed 09:25 default. For an explicit offline fixture invocation the CLI
also accepts `--selection-cutoff-utc` and `--as-of`. The active policy is loaded
and verified through the existing policy loader. Native files supply the actual
manifest/risk facts; machine credentials use `alpaca_paper_credentials` aliases.

Outputs live under `RUN_ROOT/loop/DATE/`. Previous content-addressed execution
indexes are discovered, hash-verified and retained automatically. The standalone
read-only history path requires neither today's plan nor broker credentials:

```text
python -m scripts.produce_loop_daily --run-root RUN_ROOT --trade-date YYYY-MM-DD --history-only
```

If today's provider blocks, today's review is not submitted, but historical
Outcomes continue using the history-only index. An explicitly configured
`AI_QUANT_LOOP_OUTCOME_EXECUTION_INDEX_FILE/SHA256` takes precedence for that
independent path. No Outcome enable flag or promotion policy was relaxed.
Zero/missing-plan days retain unavailable factual/risk semantics; this does not
manufacture an order authorization or a new remote no-trade contract.

### Current remaining acceptance boundaries

The **daily discovery/producer implementation blocker is resolved for future
fact-complete native-plan days**. Main's actual startup-monitor integration must
be merged and independently exercised on a future session. No actual broker call,
secret read, order or Loop upload was performed by this worker. Per Main, the
real 2026-09-04 stage-only run reported risk unavailable and submitted nothing;
that missing-native-history result is correct and is not backfilled here.
2026-09-07 is not presented as an actual trading-session test.

Additional observed regression reds: excluded/manual FILL still had complete=true;
missing daily discovery module; conflicting restart account was silently ignored;
known pre-POST aborted intent was treated as unresolved; failed current provider
prevented historical Outcome retry; scheduler did not invoke native discovery.
All now have passing fixture checks. Main's reported `min_rvol=3` test setup issue
was corrected to **3.0**; no production strategy-hash semantics were changed.

New files in this last round: `operations/loop_integration/daily_provider.py`,
`scripts/produce_loop_daily.py`, `tests/test_loop_daily_provider.py`. Existing
`broker_fills.py`, `schedule/postmarket.py`, `tests/test_loop_postmarket.py`,
`tests/test_loop_providers.py` and this document received the corresponding fixes.

Final verification after the last code change: **88 passed in 13.87s**, ruff
**All checks passed**, mypy **24 source files / no issues**, and diff whitespace
check passes. Test command adds `tests/test_loop_daily_provider.py` to the five
files listed in the previous reproduction block; static scope additionally adds
`scripts/produce_loop_daily.py` and `tests/test_loop_daily_provider.py` (the
integration-directory argument already includes `daily_provider.py`). Use a new
basetemp name when repeating. No full-repository, live broker, deployment or
remote acceptance claim. Worker stops writing after this handoff; no commit.

## Main final acceptance checks and enablement handoff

Main reported fresh independent verification after the daily-producer work:

- Full repository suite: **850 passed in 52.36 seconds**.
- Full repository Ruff: **passed**.
- Mypy: **447 files passed**.
- Avicenna reported **no confirmed fatal daily-provider defect**; the documented
  conservative limits remain known. This is not a claim of live-session acceptance.

These are Main-reported results, not additional runs by this worker. The earlier
74/88-test worker scopes and previous missing-producer notes are historical.
**The native daily producer and scheduler integration are implemented; no code
work remains in this handoff.** Future fact-complete native-plan days can use the
implemented path; missing historical plans and zero-plan days remain unavailable.

Concrete deployment variables for Main (documentation only; not changed here):

| Variable | Required value / purpose |
| --- | --- |
| `AI_QUANT_LOOP_NATIVE_RUN_ROOT` | Absolute production `runs` directory, e.g. `D:/cdoeX-work/runs`; contains `autonomous/DATE` and `modern-momentum/DATE`. |
| `AI_QUANT_LOOP_SYNC_ENABLED` | `true` to enable daily review delivery. |
| `AI_QUANT_LOOP_BINDING_FILE` | Approved binding file; Main identified `D:/cdoeX-work/runs/loop-quant-binding.json` (v6). |
| `AI_QUANT_ACTIVE_POLICY_FILE` | Actual approved active-policy JSON path. |
| `LOOP_BASE_URL` | Existing configured Loop endpoint. |
| `LOOP_RUNTIME_API_KEY` | Existing secret, supplied by the runtime; never copied into this document. |
| `ALPACA_PAPER_KEY_ID`, `ALPACA_PAPER_SECRET_KEY` | Existing Paper credentials, or supported `alpaca_paper_credentials` aliases; producer endpoint remains fixed to Paper. |
| `AI_QUANT_LOOP_OUTCOME_SYNC_ENABLED` | Separate Outcome sync switch; enable only under existing authorization, not implicitly with daily review. |
| `AI_QUANT_LOOP_OUTCOME_CONFIG_FILE` | Approved Outcome cost/reporter config when Outcome sync is enabled. |

Native mode needs no daily config/hash or manually maintained prior index. Do not
simultaneously set `AI_QUANT_LOOP_PROVIDER_CONFIG_FILE`,
`AI_QUANT_LOOP_PROVIDER_CONFIG_SHA256`, `AI_QUANT_LOOP_EXECUTION_INDEX_FILE` or
`AI_QUANT_LOOP_EXECUTION_INDEX_SHA256` for the daily path. Optional historical
Outcome override uses the separate `AI_QUANT_LOOP_OUTCOME_EXECUTION_INDEX_FILE`
and `AI_QUANT_LOOP_OUTCOME_EXECUTION_INDEX_SHA256`; otherwise native history
discovery retains the prior entries automatically. Promotion remains unchanged.

This final update changes only this document. No secrets, production environment,
code, other worktrees or delivered artifacts were changed; no commit was made.
Worker is stopped for Main's commit/merge. Actual future-session evidence and
remote delivery acceptance remain deployment checks, not fabricated test results.
