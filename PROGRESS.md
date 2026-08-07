# Progress

## Adaptive desktop decision client — 2026-07-28

Status: implemented for local read-only operation; automatic orders remain disabled.

- Added one deterministic adaptive-plan state machine for catalyst and pure-factor
  routes. It separates 15-second fact polling from completed-bar confirmation, a
  three-minute soft-revision cooldown, and a per-session revision cap while allowing
  immediate hard/time-stop decisions.
- Added risk/notional-bounded probe and add sizing, two distinct completed one-minute
  confirmations, 1/5/15-minute technical confluence, SPY/sector state, consolidated-tape
  order flow, frozen catalyst evidence, and broker-authoritative position reconciliation.
- A position that disappears at the Broker closes the plan and cannot be revived from
  stale local state. Add advice is not repeated until the Broker position changes.
- Added WAL/FULL SQLite baselines, runtime state, deduplicated append-only events,
  historical SIP warmup, resumable SIP ingestion, and a read-only localhost HTTP/SSE
  interface. No POST/order route exists.
- Added a Chinese React/Electron Windows client and a one-command PowerShell launcher.
  The launcher owns and cleans up only the processes it starts; the client contains no
  credential.
- Main-repository acceptance: 290 tests passed, Ruff clean, and strict mypy success
  across 149 source files. Vite production build and Electron syntax validation passed.

## M0 — environment and invariants

Status: complete

- Test-first invariant suite added.
- Frozen configuration model and repository skeleton added.
- Isolated `.venv` created; exact versions pinned in `requirements.txt`.
- `python -m pytest -v`: 3 passed.
- `ruff check .`: all checks passed.
- `mypy kernel tests`: success across 19 source files.

Acceptance evidence was produced on 2026-07-16 with Python 3.12.6. No market
data and no simulated strategy results were used in M0.

## Architecture fusion — 2026-07-16

Status: complete

The supplied layered-architecture PDF was rendered and reviewed. Incremental ideas
were added without changing frozen interfaces:

- immutable dataset snapshots with severity-based quality quarantine;
- chronological research-run registry with artifact hashes and trial count;
- deterministic OMS order lifecycle with partial-fill and illegal-transition checks;
- explicit five-layer mapping and scale-up criteria in `docs/ARCHITECTURE.md`;
- fast-loop no-LLM scanning expanded from `kernel/` to `execution/`.

Acceptance evidence: six architecture-contract tests plus the original invariant
suite, ruff, and mypy.

## External blockers

- Authoritative Alpaca and Massive market-data access is operational. Public
  Yahoo/Hugging Face samples remain engineering-only and quarantined by policy.
- M6 Postgres persistence requires `POSTGRES_DSN` in a local `.env` file.
- M8 agent migration requires the seven original Accio `IDENTITY.md` files.

## M1 data bootstrap — 2026-07-17

Status: in progress

- Added canonical UTC one-minute bar schema and provider-neutral quality checks.
- Added immutable Zstandard Parquet snapshots with SHA-256 manifests and automatic
  accepted/quarantine routing.
- Added public Hugging Face and Yahoo staging adapters, direct adjusted Alpaca SIP and
  Massive SIP adapters, and current Nasdaq/SEC reference adapters.
- Public/community data is fail-closed for research provenance; current reference
  directories are explicitly barred from historical-universe use.
- Accepted real-data snapshots: 13,054 current Nasdaq-listed symbols and 1,004 XNYS
  sessions for 2024-2027, including early closes.
- Quarantined engineering snapshots: 26,718 recent Yahoo minute bars (four incomplete
  second-aligned tail bars; trade count/VWAP unavailable) and 4,909 community minute
  bars (SPY absent from the requested set).
- Verification: `pytest` 14 passed; ruff and strict mypy passed across the data plane
  and tests.
- Alpaca authentication and historical SIP access verified with an accepted 390-row
  AAPL RTH snapshot for 2025-01-02. RFC3339 provider timestamps are normalized to UTC;
  verification now stands at 15 passing tests plus clean ruff and strict mypy.
- Thirty-session SIP validation completed for AAPL, SPY, UPWK, and AEHR: 78,520 raw
  all-session bars, 46,028 RTH bars, and a 120-row immutable coverage audit. AAPL/SPY
  had complete 390-minute coverage on all sessions; 772 absent small/mid-cap aggregate
  minutes remain explicit and unfilled. Verification now stands at 16 passing tests.
- Massive authentication passed with a complete 390-row AAPL session. A same-session
  vendor comparison exposed and fixed an Alpaca dividend-vs-split adjustment mismatch;
  aligned OHLC now agrees within $0.0001. Massive is designated primary because VWAP
  and three volume minutes still reflect different vendor eligibility rules.
- Massive grouped-daily ingestion completed its first 30 XNYS sessions: 370,916 rows,
  13,214 union symbols, zero duplicate symbol/date keys, and 30 individually accepted
  snapshots plus one combined lineage snapshot. The full two-year grouped-daily
  backfill is complete: 501 XNYS sessions from 2024-07-17 through 2026-07-16,
  5,677,558 rows, 15,813 union symbols, no missing/unexpected dates, no duplicate
  symbol/date keys, no invalid OHLC, and no negative volume. Daily-schema,
  credential-redaction, pagination, and existing invariants pass 18 tests plus clean
  ruff and strict mypy.

## M2 daily universe precheck — 2026-07-17

Status: complete for daily-bar-supported fields; downstream data gates remain closed.

- Implemented shared point-in-time ATR14, Beta252, and ADV20 functions plus the public
  `build_universe(trade_date)` interface.
- Added accepted Massive `type=CS` reference ingestion with cursor-safe 1,000-row
  pagination. The 2026-07-16 active snapshot contains 5,306 common stocks and passed
  all reference quality checks.
- Real-data audit found and fixed instrument-type contamination and ticker-identity
  reuse. A conservative >90% one-day discontinuity check rejected 377 suspect histories.
- Persisted accepted snapshot
  `kernel.universe.daily_precheck-20260717T161535Z-5dd1b9e8f1ac`: 5,179 symbols,
  2,871 price/elasticity/identity prechecks, zero final passes, 301 parent snapshots,
  and zero failed quality checks.
- RVOL, point-in-time market cap, earnings exclusion, LULD state, and catalyst ranking
  remain explicit missing-data gates. No stock recommendation or performance claim was
  produced.

## M3 catalyst evidence layer — 2026-07-20

Status: complete for deterministic evidence ingestion and cleaning; model scoring and
premarket verification remain closed.

- Added one canonical UTC catalyst contract for Alpaca/Benzinga news, Massive news,
  and SEC candidate filings. Full article bodies are not stored.
- Implemented exact publication/acceptance-time no-lookahead checks, >3-symbol and
  <25-word paper-derived filters, cross-source event-chain deduplication, weekend-to-next-
  session attribution, and daily-precheck membership.
- Added real-data noise gates for editorial articles, securities-law-firm solicitations,
  and backward-looking automated performance articles.
- 2026-07-20 lock run: 350 raw records, 118 eligible evidence records, 97 candidate
  symbols, seven with at least two sources, zero populated model scores, and zero failed
  critical checks.
- Accepted candidate snapshot:
  `kernel.catalysts.overnight_candidates-20260720T080424Z-588747108000`.
- Raw provider snapshot reuse reduces deterministic rule rebuilds from about 291 seconds
  to under seven seconds without network access.

## M4 same-time premarket RVOL — 2026-07-20

Status: deterministic kernel and history prefetch complete; target-date window is
time-gated until Beijing 20:00.

- Implemented one shared offline/online RVOL function: target `[04:00, cutoff)`
  premarket volume divided by the median of the identical New York wall-clock window
  over the prior 20 XNYS sessions.
- The target cutoff is decision time minus the documented 15-minute provider delay.
  For 2026-07-20 this is 07:45 ET exclusive, so 07:44 is the last included bar.
- A successfully queried minute with no emitted Alpaca bar contributes no qualifying
  volume; no price or volume is forward-filled. Failed or incomplete session requests
  make RVOL unavailable, and a zero historical median fails closed.
- Added seven no-lookahead tests covering future mutation, same-time history, cutoff
  exclusion, missing requests, zero baselines, DST, and locked-pool isolation.
- Prefetched 20 accepted history snapshots from 2026-06-18 through 2026-07-17 for the
  97-symbol catalyst lock: 56,822 bars, 93 symbols with at least one emitted bar, zero
  duplicate `(symbol, ts_utc)` keys, and zero failed critical checks.
- The target-date query and final RVOL candidate snapshot are deliberately absent until
  the Beijing 20:00 decision time; no partial current window is presented as a fact.
- Current repository verification: 39 tests passed, Ruff clean, and strict mypy clean
  across 57 source files.

## M5 selection gates and evidence-safe trading kernel — 2026-07-20

Status: code, synthetic acceptance tests, and real point-in-time reference prefetch
complete; the target-session RVOL time gate and historical-news backfill remain in
progress.

- Added Nasdaq/Zacks expected earnings-calendar ingestion and conservative earnings-day
  exclusion. The algorithmic expected-date limitation remains explicit in provenance.
- Added Nasdaq Trader all-exchange halt RSS ingestion, including LUDP/LUDS history,
  unresolved prior-day halt handling, event-chain deduplication, and retry-on-invalid-
  XML protection for transient anti-bot pages.
- Added Massive point-in-time ticker details (market cap and shares) plus latest free
  float ingestion. Market cap determines the frozen mega/large/mid/small sizing tier.
- Added the final locked-pool gate: daily precheck, strict RVOL > 3, valid market cap,
  no earnings day, no unresolved current halt, and no recent LULD combined with less
  than 20 million or unknown free-float shares.
- Implemented bullish ORB-5, next-complete-bar VWAP entry, ATR exits, conservative
  triple barrier, risk/liquidity/tier sizing, and two-leg commission/SEC/TAF/spread/
  impact/stop-slippage costs.
- Implemented deterministic purged walk-forward folds and a meta gate that refuses any
  uncalibrated probability. Provider-neutral catalyst scoring records model, prompt
  hash, evidence IDs, temperature zero, and enforces post-training-cutoff evaluation.
- Added restart-safe monthly Massive historical-news backfill and a delayed-feed ORB-5
  shadow runner. Full real-time SIP remains an external subscription requirement before
  any live signal is actionable.
- Real reference acceptance: all 97 locked symbols have positive point-in-time market
  cap for 2026-07-17; 93 have positive free-float values. Massive did not return
  free-float records for DBVT, FABC, PPLI, and VIVO, so that absence remains visible
  and fails closed when combined with recent LULD risk. The 2026-07-20 earnings
  calendar has 25 rows with zero locked-symbol overlap; the six-session halt set has
  284 unique events.
- Added a DeepSeek V4-Pro slow-loop adapter and executable catalyst shadow scorer. It
  freezes the exact `deepseek-v4-pro` response model, uses non-thinking mode with
  temperature zero, resolves only evidence IDs already present in the morning lock,
  records response/system fingerprints and token usage, and cannot approve its own raw
  probability for the kernel.
- Repository acceptance now stands at 75 passing tests, clean Ruff checks, and strict
  mypy success across 64 source files.

## M8 automatic postmarket learning loop — 2026-07-21

Status: implemented for daily Episode capture and governed two-agent review; model
promotion remains closed pending sufficient labeled observations.

- Replaced the unavailable seven-role Accio identity dependency with two narrow,
  read-only roles: Research and Critic. Both consume the same frozen fact package in
  separate prompt contexts and emit strict JSON contracts.
- Added immutable Trading Episodes joining selection facts, unapproved DeepSeek shadow
  scores, full-session ORB-5 replay, and deterministic triple-barrier outcomes.
- Real 2026-07-20 replay produced seven candidate rows and two ORB triggers (JTAI and
  DGXX). Both paths contained genuine missing trade minutes and were explicitly
  censored; no bars were filled and no performance claim was generated.
- The first Research Agent correctly emitted zero hypotheses because there were no
  completed outcome labels. The Critic rejected advancement. The resulting evolution
  state is `no_actionable_hypothesis`, with production approval false.
- Added a SQLite job ledger with idempotent claim, retry, stale-run recovery, completion
  artifacts, and sanitized error codes. `schedule.postmarket` discovers completed
  selection dates and reuses accepted signal, Episode, and review snapshots.
- Added a Windows Task Scheduler installer for a 30-minute tick. XNYS close and provider
  delay are checked in Python rather than encoded as a fixed Beijing clock.
- Registered `Trading System V2 - Postmarket Review` locally and verified one natural
  timer launch: result code 0, zero missed runs, and the next 30-minute trigger present.
  The idempotency ledger remained at one attempt and one Episode/review artifact.
- Sandbox admission requires at least 20 distinct session Episodes and 20 uncensored
  trade labels. Agents cannot edit production strategy code, approve a model, or place
  orders. Net returns remain unavailable until quote-spread data is captured.
- Current repository acceptance: 83 tests passed, Ruff clean, and strict mypy clean
  across 75 source files.

## M8 production hybrid review hardening — 2026-07-21

Status: the postmarket research path is production-operable as a one-shot service;
live signal/actionability and automatic production promotion remain closed.

- Added a deterministic program review before any model call. It calculates immutable
  quality/sample/outcome/cost facts, creates only an allowlisted RVOL sandbox sensitivity
  specification, and can never approve production.
- Preserved the Research/Critic pair where semantic reasoning adds value. In the default
  `optional` hybrid mode they run automatically only after complete paths, net-cost
  labels, and minimum samples pass program gates. A provider outage cannot destroy the
  program review or silently advance an experiment.
- Agent context is no-lookahead: only Episode dates at or before the reviewed session are
  eligible. The fact package includes the deterministic review and at most the 500 most
  recent evidence-bound rows, including net outcomes when available.
- Upgraded the SQLite job ledger to WAL/FULL synchronization and per-attempt lease tokens.
  A stale process cannot complete a newer attempt. Retries use a 30-minute backoff and a
  five-attempt ceiling.
- Added a cross-platform single-process lock, UTC JSON lifecycle logs, sanitized error
  codes, a read-only health command, and custom data-root propagation to every child job.
- Added hardened Linux systemd service/timer assets and a non-root Docker image with a
  separate runtime-only dependency set. Secrets and runtime data are excluded from the
  image build context.
- The installed Windows timer naturally executed `postmarket_review.v2`. Real 2026-07-20
  output is `blocked_data_quality`: one Episode, zero completed/net labels, two censored
  triggers, Agent status `skipped_by_program_gate`, and production approval false.
- Production image `trading-system-v2:local` built successfully and its non-root Linux
  health check returned `ready`.
- Added coded Paper/Live maturity gates with objective sample, cost, OOS, Paper-session,
  reconciliation, duplication, recovery, backup, alert, secret-rotation, risk, and
  compliance evidence. Current evidence correctly reports `research_only`; eligibility
  can never arm live trading.
- A read-only probe of the configured Alpaca Paper endpoint returned an active,
  unblocked Paper account. No order endpoint was called.
- Repository acceptance is 98 passing tests, clean Ruff, and strict mypy success across
  85 source files.

## M9 Paper execution and realtime SIP foundation — 2026-07-21

Status: the realtime feed, causal online signal contract, Paper-only OMS/adapter, and
recovery path are implemented and verified; broker writes remain intentionally locked by
the research-only maturity gate.

- Rotated credentials were detected without exposing them. The configured Alpaca Paper
  account returned `ACTIVE`, unblocked status over HTTP 200. Latest SIP HTTP quote access
  returned 200 instead of the previous 403.
- A real `wss://stream.data.alpaca.markets/v2/sip` probe connected, authenticated, and
  acknowledged AAPL bars and quotes. The probe reported `orders_submitted: 0`.
- Added a single-owner SIP client with automatic reconnect/resubscribe, bounded protocol
  schemas, a cross-process lock, and WAL/FULL persistence for all minute bars plus the
  last NBBO quote per symbol-second. A bounded 15-second process smoke test exited cleanly;
  it ran before the 04:00 ET premarket session and correctly observed zero events.
- Replaced the trading-layer placeholders with a frozen long-only `TradePlan`, strict
  P0 -> P1 -> P2 arbitration, Paper-only Alpaca adapter, bracket payload, complete plan
  audit, SQLite order/event ledger, deterministic client IDs, and idempotent submission.
- Added restart reconciliation for Broker IDs and new/partial/fill/cancel/reject states.
  A crash after Broker acceptance can recover an approved local order without submitting
  a duplicate; identity or quantity mismatches fail closed.
- Split the causal live ORB intent from the historical next-bar VWAP fill proxy. Live
  intent fires only when the first breakout bar has just completed and rejects late replay
  or any missing minute path.
- Runtime sizing now uses `min(configured capital, actual Broker equity)`. The current
  $100k Paper account therefore cannot inherit the $200k configured risk budget or the
  $400k leveraged buying-power display.
- Safe execution settings default to `BROKER_WRITE_ENABLED=false` and
  `TRADING_KILL_SWITCH=true`. Even a write flag cannot bypass coded Paper readiness.
- Added the complete centralized session processor and CLI: SIP bars/quotes -> causal
  ORB intent -> current NBBO -> Broker-equity-capped sizing -> protected TradePlan ->
  ordered guardrails -> durable OMS. A synthetic end-to-end acceptance run recorded an
  approved shadow plan and submitted zero orders because Paper readiness was false.
- Repository acceptance now stands at 131 passing tests, clean Ruff checks, and strict
  mypy success across 111 source files.

## M10 production evidence, exits, and historical replay — 2026-07-21

Status: production mechanisms are implemented and fail closed; the 252-session research
backfill and externally owned attestations are still in progress, so the system remains
`research_only` and submits no orders.

- Added restart-safe 252-session point-in-time selection replay with weekly Massive
  reference anchors, Beijing 08:00 catalyst locks, same-time 20-session SIP premarket
  RVOL, historical earnings/halts/market-cap gates, and explicit missing-data failures.
- Added RTH ORB replay with capacity censorship, causal entry NBBO, conservative exit
  spread, real net-cost labels, quote-cost coverage, and five chronological purged OOS
  folds with frozen configuration/code/data/feature hashes.
- Added a durable time-exit coordinator that cancels symbol protection and submits one
  idempotent whole-share sell only on the Paper endpoint. Its five-second poll is
  independent of SIP message frequency, so continuous traffic cannot suppress exits.
- Added startup local/Broker reconciliation, fail-closed unmanaged-position detection,
  and a durable Paper-session ledger. Only full successful XNYS sessions count; bounded
  smoke tests and failed sessions do not.
- Added idempotent premarket, Paper, postmarket, weekly backup, and failure-alert systemd
  units. Holidays and late persistent-timer firings skip cleanly.
- Added online SQLite backup plus accepted-snapshot archive manifests, path-safe restore,
  per-file SHA-256 verification, and restored-database integrity checks.
- Added automatic maturity-evidence refresh. Program metrics are recomputed from
  immutable snapshots and the Paper ledger while external attestations are preserved
  and never invented.
- Real read-only access evidence now verifies an active unblocked Alpaca Paper account
  and authenticated SIP bars/quotes with zero orders submitted. A real local safety
  receipt proves the kill switch made zero Broker calls and backup/restore succeeded.
- Completed the missing automatic evolution executor for the allowlisted RVOL research
  family. It labels every survivor before applying per-configuration top-eight capacity,
  uses four purged discovery folds plus one untouched confirmation holdout, and records
  an immutable research Champion/Challenger decision. It never rewrites production
  configuration and always emits `production_eligible=false`.
- Current repository acceptance: 169 tests passed, Ruff clean, and strict mypy clean
  across 151 source files.

## M11 audited Agent PDCA and draft-only evolution - 2026-07-21

Status: the Agent boundary and automatic slow-loop scaffolding are implemented and
fail closed. The system remains `research_only`; no Agent can submit an order or promote
a configuration.

- Replaced placeholder MCP modules with an official FastMCP stdio server and fixed the
  local package-name collision that had shadowed the installed MCP SDK.
- Added role-bound least-privilege policy for eight Agent roles and fourteen tools.
  Every request/result or sanitized failure is retained in SQLite or PostgreSQL.
- Added provenance-bearing fact, thesis, discipline-report, anonymous lesson, and
  draft-proposal contracts. Bare decision numbers in narratives are rejected.
- Added explicit `N/A` degradation for unavailable order-flow, FINRA short-flow,
  sentiment, ledger, barrier, and factor-health inputs.
- Added a non-executable shadow TradePlan sink with zero Broker submissions and a
  deferred fail-closed guardrail state.
- Built and validated the repo-local `quant-agent` plugin plus `postmarket-pdca` and
  `monthly-evolution` skills.
- Extended the postmarket one-shot job to run structured Discipline then PDCA after the
  deterministic program gate. Added a hardened first-XNYS-session monthly evolution
  service/timer; proposals remain draft-only.
- A real local no-LLM smoke run wrote an `incomplete_evidence` discipline report because
  no order/execution/barrier ledger was configured. It wrote no lessons, proposals,
  orders, or production changes. The monthly smoke run correctly returned
  `insufficient_evidence`.
- Diagnosed and fixed a historical RVOL DST parity defect: the feature cutoff now derives
  from Beijing decision time minus provider delay and then converts to New York wall time.
  Summer sessions use 07:45 ET and winter sessions use 06:45 ET; the previously blocked
  2025-11-03 session now passes the no-future-data check. The restart reused all raw caches
  and completed all 252 RVOL sessions before advancing to selection gates.
- Current acceptance is 181 passing tests, clean Ruff, and strict mypy success across
  162 source files. Both skills and the complete plugin manifest validate successfully.

## M12 realtime selection timing and configuration parity - 2026-07-22

Status: the missed-trigger diagnosis is fixed in code and verified against the real
2026-07-21 session; the system remains `research_only` and submits no orders.

- Replaced the legacy fixed 15-minute selection delay with one fail-closed feed policy.
  Licensed `sip` is realtime with zero delay; only explicit `delayed_sip` uses 15 minutes.
  Online RVOL, point-in-time ORB research snapshots, and historical replay now share it.
- Historical feature-cache identity now includes feed and delay, so a delayed snapshot
  cannot be silently reused by a realtime replay.
- The continuous Paper service starts at 09:20 New York time, verifies SIP entitlement,
  owns the single licensed WebSocket connection, and scans every completed minute rather
  than taking one delayed intraday snapshot.
- Intraday research output now distinguishes `in_progress_no_trigger_yet`,
  `in_progress_pending_confirmation`, and `in_progress_with_triggers`; only a cutoff at
  or after the XNYS close is `complete_session`.
- Quote freshness, Beijing selection time, and postmarket data-grace are centralized in
  `config.yaml`; the affected scheduler job versions were advanced to prevent an old
  success ledger from skipping new semantics.
- A real SIP point-in-time replay at 2026-07-21 09:52 ET returned GREE and AMC as the two
  triggered symbols. GREE was therefore visible to the corrected scanner during the
  session, without using future bars or submitting an order.
- Repository acceptance: 188 tests passed, Ruff clean, and strict mypy success across
  163 source files.

## M13 local multi-day observation runtime - 2026-07-22

Status: the safe local observation loop is installed; realtime Paper observation remains
write-disabled and the system remains `research_only`.

- Added Windows tasks for five-minute premarket orchestration, a DST/XNYS-aware Paper
  launcher, and 30-minute postmarket review. All phases append separate stdout/error
  logs under `runs/` and ignore overlapping invocations.
- The Paper launcher starts from the configured lead time, verifies Paper account and
  SIP entitlement, refreshes maturity evidence, then owns the continuous licensed SIP
  stream until the official exchange close. Weekends, holidays, pre-window, and
  post-close invocations skip cleanly.
- Preserved `BROKER_WRITE_ENABLED=false` and `TRADING_KILL_SWITCH=true`; local observation
  records selections, signals, plans, guardrail decisions, and reviews without orders.
- Added immediate scheduler recovery after an abrupt process termination. The outer
  process lock proves that no healthy peer exists before reclaiming an orphaned job
  lease, and a non-succeeded lock/selection phase can no longer be reported as complete.
- Installed all three tasks in Windows Task Scheduler. The Paper tick skips cleanly
  outside its XNYS window, and the repaired premarket task began the 2026-07-22 lock
  stage automatically while retaining write-disabled safety settings.
- The first 2026-07-22 daily refresh received HTTP 403 for Massive's 2026-07-21
  grouped-daily endpoint while the configured key was present. The task failed closed
  and now records `lock_stage_pending_retry`; configured 30-minute retries avoid both
  an early five-attempt exhaustion and aggressive provider polling before data release.
- Repository acceptance: 192 tests passed, Ruff clean, and strict mypy success across
  165 source files.

## M14 external cloud feature interface - 2026-07-22

Status: repository separation and the fail-closed client boundary are implemented;
the existing single-strategy and local observation runtime remain the default.

- Extracted every cloud multi-strategy implementation, strategy sandbox, collaborator
  API, and cloud-specific CLI into the independent `cloud-strategy-platform` repository.
- Restored `scripts.run_paper_session` to its original local single-strategy SIP path.
  No cloud import, database, or optional cloud registry argument remains in the runner.
- Added a versioned `v1` HTTPS feature client for slow-loop synchronization. Tokens are
  represented as `SecretStr`, non-local plaintext HTTP is rejected, contract mismatches
  fail closed, and sanitized errors never include credentials.
- Added a WAL/FULL local point-in-time feature cache. The deterministic fast loop reads
  the cache and never makes a remote HTTP request.
- Added `scripts.sync_cloud_features`; the only required secret is the dedicated
  `CLOUD_FEATURE_API_TOKEN`, which has no raw SIP, Alpaca, Broker, or order permission.
- Existing Broker write defaults, permanent long-only contracts, P0 -> P1 -> P2
  arbitration, and local observation scheduler behavior remain unchanged.
- Repository acceptance: 195 tests passed, Ruff clean, and strict mypy success across
  161 source files.

## M15 keyless cloud market and Paper interface - 2026-07-22

Status: the AI investment process no longer owns Alpaca credentials or connects to
Alpaca market/Paper endpoints; local observation remains write-disabled.

- Replaced direct historical bars, quotes, news, realtime SIP, account, position, and
  Paper-order clients with narrowly scoped cloud-platform API clients.
- Removed Alpaca Key/Secret, direct market-data domains, and direct Paper domains from
  current AI runtime configuration, examples, health checks, and deployment guidance.
- Migrated the local AI `.env` atomically to market-data, Paper, and feature API tokens;
  provider credentials now exist only in the independent cloud platform's ignored
  `.env` file and were never printed.
- Preserved the existing single-strategy kernel, P0 -> P1 -> P2 arbitration, long-only
  contracts, idempotent client order IDs, local scheduler, write-disabled default, and
  active kill switch.
- Verified the running loopback cloud API end to end: Paper account `ACTIVE`, SIP bars
  and quotes received for AAPL, and zero orders submitted.
- Repository acceptance: 194 tests passed, Ruff clean, and strict mypy success across
  168 source files.

## M16 resilient lease-driven realtime observation - 2026-07-22

Status: fixed and running locally; the continuous session is observation-only,
`research_only`, write-disabled, and protected by the active kill switch.

- Added the cloud symbol lease before event consumption and initialized the cursor from
  the requested market-open replay point. Normalized symbol order no longer causes a
  false contract failure.
- Fixed the asynchronous polling defect that cancelled the pending event read every five
  seconds. Time-exit polling and freshness checks now reuse one pending read, while a real
  collector outage still fails closed after the configured stale threshold.
- Added a current-user observation supervisor because this Windows host terminates all
  three Task Scheduler actions with `0xC000013A` before their scripts execute. The
  supervisor launches premarket, Paper, and postmarket lanes without overlap and is
  installed in Startup; the broken legacy tasks are disabled.
- Live acceptance consumed 1,067 events in a bounded run, then kept the continuous Paper
  session alive across multiple polling intervals. Cloud and local stores both received
  minute bars, local quote timestamps continued advancing, and zero orders were submitted.
- A point-in-time SIP replay at 2026-07-22 10:12 ET recorded four research-only triggers:
  JTAI, SMCI, XE, and OKLO. Missed historical entries were not chased or submitted.
- Repository acceptance: 199 tests passed, Ruff clean, and strict mypy success across
  122 source files.

## M17 observable cloud market-data consumer - 2026-07-23

Status: implemented and verified; the AI repository remains fully separated from the
cloud repository and still owns no Alpaca credential.

- Replaced continuous REST event polling with resumable cloud SSE while retaining
  cursor polling only for an explicitly old server that returns `404/405` for the stream.
- Added strict health-contract parsing and a bounded startup wait. Delayed, stale,
  unsubscribed, unavailable, malformed, or fallback-recommended symbols fail closed
  before the Paper observation loop starts.
- Required every historical bars/quotes response to carry per-symbol coverage. A cloud
  gap/empty recommendation now raises a sanitized download failure instead of letting an
  empty frame masquerade as usable input.
- Added a bounded symbol lease to the standalone collector as well as the Paper session;
  no AI process opens an Alpaca connection or receives an Alpaca Key.
- Preserved the existing deterministic single-strategy kernel, local SIP store, Windows
  observation tasks, write-disabled default, active kill switch, long-only contracts,
  and Paper maturity gates.
- No automatic Yahoo/community fallback was added: repository provenance policy requires
  missing or degraded licensed data to remain explicit and quarantined.
- Repository acceptance: 204 tests passed, Ruff clean, and strict mypy success across
  171 source files.

## M18 directional-volume and bearish-distribution hard gates - 2026-07-24

Status: implemented and verified; unsigned high RVOL can no longer become a buy
candidate without price confirmation, and prior-session distribution fails closed.

- Added a point-in-time prior-session distribution veto: open-to-close return at most
  -3%, volume at least 1.5 times the preceding 20-session average, and a close in the
  bottom 30% of the range.
- Added premarket OHLC, aggregate VWAP, return, close-location, and direction evidence.
  Final survivors require RVOL strictly above 3, a positive premarket return, close
  above premarket VWAP, close location at least 0.60, and premarket close strictly
  above the prior daily close.
- Upgraded daily precheck, premarket feature, and selection snapshots to v2. Historical
  replay uses the same code path and thresholds; obsolete selection snapshots cannot
  pass the execution loader.
- Recomputed 2026-07-24 from accepted SIP evidence. DECK was rejected for prior-day
  bearish distribution and a -2.0576% premarket gap; RHI, RNG, TOL, and ITRG failed
  premarket price confirmation. The final locked pool contained zero survivors and no
  order was submitted.
- Evidence snapshots:
  `kernel.universe.daily_precheck-20260724T124931Z-bd17e2d250be`,
  `kernel.premarket.rvol_candidates-20260724T125001Z-f2bfa7fdd851`, and
  `kernel.universe.selection_gates-20260724T125058Z-f948332cedca`.
- Repository acceptance: 209 tests passed, Ruff clean, and strict mypy success across
  171 source files.

## M19 advisory multi-timeframe technical monitor - 2026-07-24

Status: running locally every 15 minutes for RNG; it is read-only, advisory-only,
and automatically stops after the regular session.

- Added point-in-time 1/5/15-minute aggregation that excludes unfinished buckets.
  Deterministic indicators include MACD, Bollinger bands, KDJ, session VWAP,
  confirmed three-bar fractals, intraday Fibonacci levels, and an explicitly
  labelled five-minute wave-structure proxy.
- Added a strict advisory rule: a possible add requires fresh realtime SIP NBBO,
  spread at most 0.30%, price above VWAP, aligned 15-minute and 5-minute trend,
  a confirmed 1-minute trigger, and a green 1-minute bar at least 1.5 times its
  prior-20-bar median volume. Protection levels take precedence over bullish
  indicators, while every result carries `order_authorized=false`.
- Preserved the execution downloader's fail-closed coverage rule. A separately
  named monitoring-only reader may retain sparse/no-trade minutes, reports the
  provider's gap evidence, and cannot be imported as an execution authorization.
- Added a single-instance PowerShell supervisor. It verifies ownership and health
  of the dedicated local cloud API, writes append-only JSONL evidence, aligns
  checks to 15-minute Eastern-time boundaries, stops after 15:58 ET, and exits
  after three consecutive failures.
- Live acceptance at 2026-07-24 11:49 ET returned `HOLD` around a 48.455 midpoint
  with `order_authorized=false`; the background error log was empty.
- Repository acceptance: 214 tests passed, Ruff clean, and strict mypy success
  across 174 source files.

## M20 causal long-green expansion watchlist - 2026-07-24

Status: implemented as an advisory confirmation layer; it does not replace the
premarket selector or turn a completed long-green candle into a late entry.

- Added a point-in-time long-green profile over completed regular-session bars:
  session return at least 4%, body/range at least 0.60, close location at least
  0.80, close above session VWAP, and volume confirmation. Volume can be confirmed
  either by a green minute at least 1.5 times its preceding 20-minute median or by
  premarket RVOL strictly above 3, which avoids comparing an already high-volume
  open only against itself.
- Added a 0-100 deterministic strength score and a locked-pool watchlist scanner.
  The scanner preserves sparse-minute evidence, fails unavailable symbols closed,
  excludes unfinished bars, and carries `automatic_order_authorized=false`.
- Kept the three causal responsibilities separate: premarket selection identifies
  potential, ORB-5 supplies an early entry trigger, and the completed long-green
  profile confirms trend strength for monitoring and staged profit-taking.
- Replay evidence for RNG: selection snapshot
  `kernel.universe.selection_gates-20260724T130308Z-74bca7e19fcc` ranked RNG first
  at 09:03 ET with RVOL 32.073337; the first ORB decision occurred at 09:36 ET
  from the completed 09:35 bar above a 44.33 opening-range high. The full
  long-green profile first qualified at 09:51 ET, so it is explicitly not treated
  as the early entry event.
- Live confirmation at 12:08 ET scored RNG 89.865687 and returned `HOLD` for the
  remaining 20 shares with order authorization disabled.
- Repository acceptance: 217 tests passed, Ruff clean, and strict mypy success
  across 175 source files.

## M21 cross-symbol structured earnings intensity - 2026-07-24

Status: implemented without a symbol/company whitelist; the remembered object is
the catalyst/price/volume pattern, not RNG.

- Fixed a generic cleaning defect: short but machine-parseable earnings wires were
  previously rejected by the 25-word news-noise rule. Structured actual-vs-estimate,
  forward range-vs-consensus, and raised-guidance range headlines are now preserved;
  unstructured short news remains excluded.
- Added deterministic, unit-aware parsing for EPS and sales/revenue actual surprise,
  forward EPS and revenue guidance versus consensus, and EPS and revenue guidance
  raises. K/M/B/T suffixes are normalized before comparisons.
- Added three evidence layers (actual results, forward guide, and guidance raise),
  a bounded 0-100 earnings intensity score, and an explicit strength-confirmed flag.
  The generic tests use an `Example` issuer rather than any production ticker.
- Replayed the 2026-07-24 source evidence. RNG now has three eligible earnings events
  instead of one: EPS surprise +5.1724%, revenue surprise +0.9963%, forward EPS and
  revenue guide +2.0000%/+0.5724% versus consensus, and FY EPS/revenue guidance raises
  +2.0284%/+0.3992%. The resulting intensity is 74.469452 with all three evidence
  layers confirmed.
- Accepted evidence:
  `kernel.catalysts.prepared-20260724T161513Z-b37e0947d774` and
  `kernel.catalysts.overnight_candidates-20260724T161513Z-b593bc51a244`, both on
  version-2 schemas. A redundant final-gate rebuild was stopped after Massive's
  free-float pagination stalled; the next normal selection run will consume the
  accepted candidate snapshot.
- Repository acceptance: 219 tests passed, Ruff clean, and strict mypy success
  across 175 source files.

## M22 independent factor selection and SIP order-flow arbitration - 2026-07-28

Status: implemented and verified in shadow mode; no new path can submit an order.

- Added an independent pure-factor candidate generator over the broad point-in-time
  daily precheck universe. Its RVOL collection does not depend on catalyst membership,
  and its transparent 0-100 score preserves each component, cutoff, reject reason,
  rank, provenance, and parent snapshot.
- Added end-to-end SIP trade support to the cloud service, local WebSocket parser,
  durable event store, and historical client. Every trade print is retained with
  nanosecond timestamps; trade events update the feature store but do not trigger a
  strategy decision on every tick.
- Added deterministic consolidated-tape order-flow features: Tick Rule buy/sell volume,
  classified coverage, order imbalance, pressure ratio, cent-bucket VPOC, NBBO size
  imbalance, microprice, spread, and a bounded confirmation score. Future observations
  are mechanically excluded.
- Added unified shadow arbitration over the union of catalyst and factor candidates.
  Agreement receives a configured bonus; order flow may confirm or reduce an existing
  score but cannot originate a candidate. Missing order flow is neutral and explicit.
- Added a single shadow pipeline command, immutable raw/feature/ranking snapshots, and
  an independently leased automatic premarket job. Completed stages are reused; a
  shadow failure retries without invalidating primary catalyst selection. The Agent
  gateway serves materialized order-flow facts and otherwise fails closed to `N/A`.
- Main-repository acceptance: 255 tests passed, Ruff clean, and strict mypy success
  across 190 source files. Cloud-repository acceptance: 32 tests passed, Ruff clean,
  and strict mypy success across 29 source files.

## M23 deterministic intraday selection postmortem - 2026-07-28

Status: implemented and verified; suitable trade-memory ideas were adapted to the
intraday system without importing the source project's long-horizon portfolio workflow.

- Added `intraday_selection_postmortem.v1`, an immutable post-close opportunity record
  that joins liquid top movers to the frozen selection cutoff. It records selection
  status, close return, MFE/MAE, root cause, ticker-free pattern key, evidence, and an
  explicitly false production-mutation flag.
- Replaced hindsight storytelling with a fixed taxonomy: captured opportunity,
  intentional gate rejection, detectable miss, after-cutoff catalyst, and incomplete
  evidence. Missing news remains incomplete rather than being relabelled as a factor
  failure.
- Upgraded the idempotent postmarket pipeline to `postmarket_review.v7`. It now creates
  the selection postmortem before structured PDCA and includes its dataset ID in the job
  artifacts; no message is pushed by the scheduled research step.
- Added the `intraday-selection-postmortem` skill and extended `postmarket-pdca`.
  Agent access is allowlisted, snapshot-backed, ticker-anonymized, and able to read
  bounded multi-session history. Agent output may only create evidence-bound sandbox
  hypotheses; one-off movers, late catalysts, and incomplete evidence cannot become
  lessons.
- A real no-push replay for 2026-07-27 produced eight accepted opportunity rows:
  three pre-cutoff news ingestion/classification reviews and five price/order-flow
  factor reviews. All critical quality checks passed and every row retained
  `production_change_allowed=false`. Accepted evidence:
  `research.intraday_selection_postmortem-20260728T033817Z-1732804ea2fb`.
- Repository acceptance: 261 tests passed, Ruff clean, strict mypy success across
  138 source files, and the new skill passed `quick_validate.py`.

## M24 bounded autonomous Paper client foundation - 2026-07-29

Status: implemented and verified as a local-first Alpaca Paper foundation. It is not
authorized for live trading and still requires multi-session Paper acceptance before
any production-readiness claim.

- Added a deterministic intraday policy for the independent catalyst and
  factor/order-flow routes. It enforces the 07:00-09:25 ET premarket entry window,
  the 09:25-09:31 no-entry lock, 09:35/10:00 confirmation exits, 12:00 entry stop,
  13:00 flat rule, route-specific score/risk thresholds, and fail-closed 2.5R first
  target plus 3R weighted reward requirements.
- Added separate standard, high-right-tail, and A++ exit policies. Initial position
  sizing is identical; only the retained tail changes from 20% to 25% or 30%.
  Structural, anchored-VWAP, order-flow, giveback, hard-negative, and time exits are
  deterministic.
- Added the ten-second premarket limit-order lifecycle and a durable one-second
  synthetic stop. Stop command intent is persisted before broker I/O, uses idempotent
  client IDs, recovers after restart, reprices at bounded 0.25%/0.50%/1.00% buffers,
  adjusts for partial fills, and fails closed on market-data or broker faults.
- Added Paper-account exclusivity. Unknown/manual orders or positions cause all open
  orders to be cancelled, all reconciled long positions to be flattened, and the day
  to remain locked across restart. Extended-hours sells are quantity bounded and can
  never open a short.
- Upgraded the Electron client to six Chinese pages: today, opportunities, positions,
  automatic review, Agent audit, and system. It has no manual order controls. Its only
  mutation is a durable one-way global emergency stop; live mode remains explicitly
  unavailable.
- Added bidirectional decision-quality review with disciplined-win,
  disciplined-loss, lucky-win, and avoidable-loss labels. Standard 20%, high 25%,
  and A++ 30% tail outcomes are kept in separate research-only shadow ledgers and
  cannot mutate production parameters.
- Repository acceptance: 345 tests passed, Ruff clean, strict mypy success across
  230 source files, and the Electron production build completed successfully.

## M25 fail-closed autonomous Paper runtime - 2026-07-29

Status: implemented and locally packaged; Alpaca Paper remains disarmed by default,
the desktop client remains paused, and no live-trading route exists.

- Added strict, atomic evidence contracts for catalyst, red-team, deterministic
  supervisor, and Livermore push health. Direct Alpaca news and current SIP quotes
  feed bounded fact packages; missing, malformed, stale, or unhealthy evidence
  produces a fail-closed safety envelope.
- Kept the deterministic kernel authoritative. Catalyst and red-team roles currently
  use the configured DeepSeek V4-Pro JSON client; the supervisor is programmatic.
  Cached classifications may be renewed only when role, model, and prompt hash are
  unchanged. Deep-learning training remains deliberately deferred.
- Completed restart-safe position sizing and lifecycle control. Target quantities use
  the immutable plan reference price, standard/high/A++ tails retain 20%/25%/30%,
  main and tail entries have separate protected order components, and main-target
  realization survives restart.
- Added the 09:30 ET handoff for premarket fills. A regular-hours protective stop is
  attached idempotently before any upgrade. Tail management persists MFE, requires
  uninterrupted weak-order-flow duration, and combines anchored VWAP, failed reclaim,
  Chandelier, structure, and hard-breakdown evidence.
- Closed the empty-position race in runtime failure handling: pending entry orders are
  cancelled even when Broker position state has not yet appeared. A failed action
  notification records a five-minute delivery latch and immediately invokes the same
  fail-closed cancellation/exit path using only a quote no older than 30 seconds.
- Centralized UTF-8 Chinese Livermore delivery with verified `sender_type=bot`,
  restart-safe deduplication, and percentage-only return/protection reporting. Channel
  discovery cannot overwrite a current actual-delivery failure; a later successful
  delivery can prove recovery.
- Added local Docker packaging for three isolated processes: SIP refresher, runtime
  agents, and Paper executor. The default compose is read-only; Paper writes require
  `BROKER_WRITE_ENABLED=true`, `TRADING_KILL_SWITCH=false`, and the separate
  `--arm-paper` override. Containers are non-root, read-only, capability-free, and
  share only the explicit config and durable `runs` mounts.
- Repository acceptance: 429 tests passed in 64.07 seconds, Ruff clean, and strict
  mypy success across 260 Python files. Both compose configurations passed quiet
  validation, all three images built successfully, and the executor image passed a
  container import smoke test. No trading service was started and no order was sent.

## M26 cross-asset perpetual sentiment shadow - 2026-07-30

Status: implemented and verified as a deterministic shadow factor. It has no scoring,
guardrail, sizing, Paper, or live execution authority.

- Added one deep deterministic interface around normalized `PerpObservation` values.
  Two read-only public adapters currently satisfy the external seam: Hyperliquid
  `metaAndAssetCtxs` (including explicit HIP-3 DEX selection) and Aevo
  `markets`/`funding`. Neither adapter accepts trading credentials or exposes writes.
- Added quality-gated 0-100/-100-0 component scoring for price trend, continuous
  price/OI confirmation, funding crowding, signed flow, liquidations, and
  Mark/Oracle basis. Missing venue fields remain unavailable; raw volume supplies only
  liquidity evidence and never direction.
- Added weighted-median cross-venue aggregation with explicit coverage, disagreement,
  confidence, and market/sector/theme scope. Tiny price/OI polling changes scale toward
  zero instead of being promoted to full-strength quadrant signals.
- Added validated `config/cross_asset_sentiment.yaml`. It is permanently shadow-only
  and currently maps only Hyperliquid/Aevo BTC and ETH into `global-risk`. HIP-3
  equity, commodity, semiconductor, or private-market mappings require a separate
  oracle/deployer/liquidity review before configuration.
- Added immutable raw and target snapshots with UTC provenance, critical quality
  checks, warning-only provider degradation, prior-snapshot parent lineage,
  `production_eligible=false`, and `execution_eligible=false`. The existing premarket
  shadow DAG runs this independent stage without adding a dependency to primary
  selection or execution.
- Three real public-API smoke runs returned four normalized observations with both
  providers healthy. The final run used its prior raw snapshot, produced all four
  configured sources, passed every critical check, submitted zero orders, and remained
  non-executable. Accepted target evidence:
  `kernel.cross_asset.sentiment_shadow-20260730T013857Z-bed96c605528`.
- Repository acceptance: 441 tests passed in 31.30 seconds, Ruff clean, and strict
  mypy success across 270 Python files.

## M27 cross-asset evidence and PIT hardening - 2026-07-30

Status: the ten code-audit findings are remediated; the module remains shadow-only.

- Added live top-of-book collection from Hyperliquid `l2Book` and Aevo `orderbook`.
  Added notional aggressor imbalance from Hyperliquid `recentTrades` and Aevo public
  instrument trade history with a 60-second window and a three-trade minimum.
- Preserved global liquidation amounts as explicitly unavailable. Neither configured
  venue exposes a complete one-shot public liquidation window, so large trades and
  price jumps are not relabeled as liquidations.
- Removed caller-controlled observation timestamps from live adapters. Historical
  `--asof` runs no longer call current-state endpoints. Previous observations must be
  strictly causal and no more than 180 seconds old.
- Selected prior raw snapshots by observation cutoff instead of file persistence time,
  moved critical raw checks ahead of scoring, made nested score/provenance mappings
  deeply immutable, and persisted current/prior component provenance.
- Live cross-asset stages now refresh instead of reusing same-session evidence.
  Historical reuse requires an exact data cutoff. Coverage is explicit and a target
  below 35% coverage cannot emit risk-on or risk-off.
- Two real public-API smoke runs returned four observations with both providers healthy.
  Hyperliquid Bid/Ask and aggressor flow were populated; Aevo Bid/Ask were populated,
  while its empty 60-second trade windows remained correctly unavailable. The second
  accepted target `kernel.cross_asset.sentiment_shadow-20260730T034651Z-f77dc1098be6`
  had all four sources, `coverage=0.72`, remained non-executable, and submitted zero
  orders.
- Repository acceptance: 447 tests passed, Ruff clean, and strict
  mypy success across 270 Python files.

## M28 portable perpetual risk positioning Skill - 2026-07-30

Status: implemented, independently forward-tested, and installed locally as a
read-only Codex Skill. It cannot select securities, authorize execution, or submit
orders.

- Added the portable `monitor-perp-risk-positioning` Python 3.11+ Skill with a
  deterministic CLI, SQLite state, `latest.json`, versioned JSON Schemas, optional
  encrypted local backup, generic JSONL/HTTP liquidation input, optional generic
  webhook, Docker packaging, and Windows/macOS/Linux operating instructions.
- Added public read-only Hyperliquid and Aevo adapters for BTC/ETH plus reviewed
  Hyperliquid HIP-3 `xyz:CL` energy and `xyz:SMH` semiconductor representatives.
  Bid/Ask, active signed order flow, funding, price/open-interest, basis, liquidity,
  provenance, partial-provider isolation, and explicit missing evidence are retained.
- Added global, energy, and semiconductor risk overlays with weighted-median
  aggregation, disagreement/venue-conflict gates, asymmetric confirmation windows,
  risk vetoes, long-only 0/0.5/1.0/1.2 position multipliers, and an inactive
  volatility target pending a real VIX source.
- A 1.2x boost requires at least 75% coverage, two venues, two independent windows,
  no relevant conflict, and real liquidation coverage. With no configured liquidation
  source, the boost is fail-closed. All snapshots and recommendations require
  `production_eligible=false`, `execution_eligible=false`, and `orders_submitted=0`
  as both runtime and JSON Schema constants.
- Added benchmark/trade outcome separation, review reporting, threshold-only
  challenger generation after at least 100 benchmark outcomes, exact config hashes,
  and explicit human hash confirmation before an approved config can replace a file.
- Two independent agent forward tests verified fresh installation, provider health,
  non-persistent live smoke, persisted snapshot/status recovery, multi-window
  hysteresis, recommendation propagation, and external schema safety. Their two
  findings—missing confirmation-gate explanation and overly broad schema fields—were
  fixed and reverified.
- Skill acceptance: 26 tests passed, Ruff clean, strict mypy success across 16 source
  files, Skill validator success, clean-venv package install and `pip check` success,
  Docker non-root `doctor` success, and live public smoke with Hyperliquid 4/4 plus
  Aevo 2/2 observations healthy. No notification was sent and no order path exists.

## M29 evidence-backed desktop operating console - 2026-07-31

Status: implemented and locally verified as a read-only research console. Paper writes
remain unapproved and no live order route exists.

- Added `trading_desk_evidence.v1`, a white-listed projection over immutable selection
  and post-close review snapshots, the durable scheduler ledger, validated runtime
  Agent assessments, and maturity evidence. API output cannot expose scheduler tokens,
  credentials, `.env` values, or order authorization.
- Replaced misleading static/transport health with evidence health. The client now
  distinguishes current, waiting, blocked, missing, and stale selection states; a
  prior-session candidate list is visibly historical and cannot masquerade as today's
  executable selection.
- The six Chinese pages now show the deterministic gate ranking, RVOL/gap/earnings
  evidence, dynamic-plan availability, accepted missed-mover attribution, actual
  Agent availability, maturity gates, and recent failed/successful jobs. Paper/live
  eligibility and manual client ordering remain explicitly false.
- Real local QA on 2026-07-31 displayed the failed current catalyst lock and the
  2026-07-30 twelve-symbol snapshot as stale evidence, eight accepted review rows,
  three unavailable runtime roles, and twelve durable job rows. Browser interaction
  produced no console errors, page errors, or failed requests.
- Repository acceptance: 451 tests passed in 23.08 seconds, Ruff clean, strict mypy
  success across 178 source files, Vite production build success, Electron main-process
  syntax validation success, and multi-page headless Chrome visual verification.

## M30 distributable macOS research client - 2026-07-31

Status: implemented on `feature/macos-research-client` as a separately packaged,
read-only client. It preserves the existing local client while removing Paper and
order capabilities from the macOS distribution boundary.

- Added a dedicated Electron main/preload pair and an explicit packaging allow-list.
  The built ASAR contains the analyst renderer, model/data services, encrypted settings,
  and the reserved IBKR interface; it excludes the existing desktop main process,
  tests, Python services, `.env` files, and execution modules.
- Added a two-step first-run flow for a remote read-only evidence service and the
  user's own OpenRouter Key. Four model roles are independently configurable:
  evidence Q&A, catalyst analyst, red team, and supervisor. Model calls occur only in
  the Electron main process and are bounded by a compact white-listed evidence payload.
- Secrets are encrypted with Electron `safeStorage`, backed by macOS Keychain, and
  settings fail closed when OS encryption is unavailable. Remote data requires HTTPS
  outside loopback, supports an optional bearer token, and accepts only
  `trading_desk_evidence.v1` with `stage=research_only` and
  `orders_authorized=false`.
- The IBKR Paper seam reports reserved/unconfigured status. Both connection and order
  methods throw, and no renderer IPC channel exposes order submission.
- Added Intel and Apple Silicon DMG/ZIP configuration plus a macOS GitHub Actions
  build. Artifacts remain unsigned until an Apple Developer ID and notarization
  credentials are supplied; this is intentionally not reported as a signed release.
- Acceptance: 452 Python tests passed, nine Electron service tests passed, Ruff clean,
  strict mypy success across 272 source files, Vite production build success, seven
  CommonJS entry/service syntax checks passed, and a Windows directory packaging
  contract check confirmed required ASAR files were present with forbidden execution
  paths absent. Configured renderer QA showed 12 evidence candidates, eight review
  rows, three research Agent cards, zero order controls, and no console/page/request
  errors.

## M31 self-contained macOS local research runtime - 2026-07-31

Status: the macOS edition now copies the main system's local research execution model.
The earlier remote desk transport was removed from this distribution. Market data is
an explicit empty Adapter and remains fail-closed.

- Added a deep `LocalResearchRuntime` module over the existing deterministic
  `data_plane`, `kernel`, `research`, `schedule`, and snapshot-backed desktop evidence.
  Its small interface exposes status, one honest desk snapshot, and an idempotent
  due-task tick; every result fixes `orders_authorized=false` and
  `orders_submitted=0`.
- Added a real market-data seam with two Adapters. The default unconfigured Adapter
  blocks before download or selection. The environment compatibility Adapter detects
  the existing Massive/cloud-market/SEC requirements but returns only missing variable
  names, never values. The release does not yet expose a way to enable it.
- Added an authenticated loopback sidecar. Electron generates a fresh 256-bit token
  each launch, starts the runtime inside the user's Application Support directory,
  and retains local accepted snapshots and job ledgers across restarts. No data-service
  URL, remote access token, Broker, Paper process, or order route remains in the Mac UI.
- Added a frozen `python -m` dispatcher so the existing premarket and postmarket DAGs
  can spawn their current child modules inside a PyInstaller executable. The native
  sidecar includes the deterministic research modules and configuration, while the
  Electron ASAR remains allow-listed to the analyst UI and local launcher.
- Replaced the macOS CI build with native ARM64 and Intel jobs. Each runner builds its
  own PyInstaller sidecar before Electron Builder packages the matching DMG/ZIP.
  Artifacts remain unsigned pending Apple Developer signing and notarization.
- Real local smoke verification started Electron plus its managed Python sidecar,
  reported `local_execution=true`, `provider_id=unconfigured`,
  `selection_status=blocked`, `market_data_provider_unconfigured`, and
  `orders_authorized=false`; stderr was empty. Visual QA confirmed the first screen
  asks only for OpenRouter and explicitly shows local execution plus the empty market
  Adapter.
- Repository acceptance: 456 Python tests passed in 22.22 seconds, ten Electron
  service tests passed, Ruff clean, strict mypy success across 277 source files, Vite
  production build success, and all analyst CommonJS files passed syntax validation.

## M32 fixed Alpaca proxy SIP for macOS - 2026-07-31

Status: the supplied Alpaca-compatible proxy was validated and integrated as the
macOS client's fixed realtime market-data endpoint without committing credentials.

- Added a protocol-level SIP probe for
  `wss://alpaca-trade-api.vertu.cn/v2/sip`. It authenticates and verifies AAPL
  quote, trade, and minute-bar subscriptions while returning only sanitized health,
  host, reason, and capability fields.
- Added a typed continuous `AlpacaProxySipStream.events()` interface that converts
  proxy frames into the system's existing `SipQuote`, `SipTrade`, and `SipBar`
  contracts for downstream SIP storage and intraday monitoring.
- Added `AlpacaProxyMarketDataAdapter`. It distinguishes realtime readiness from
  complete research-input readiness, so a healthy SIP stream does not incorrectly
  unlock selection without historical, news, and financial evidence.
- Extended macOS first-run settings with the proxy Key/Secret. Electron safeStorage
  encrypts all three secrets with macOS Keychain and passes market credentials only
  through the managed sidecar environment; they never appear in process arguments,
  renderer state, source, build config, or Git.
- Live verification using the owner-provided local credential file reported
  `configured=true`, `healthy=true`, `realtime_ready=true`, all three SIP
  capabilities, and the honest blocker `historical_research_inputs_missing`.
- Repository acceptance: 461 Python tests passed in 26.04 seconds, Ruff clean,
  strict mypy success across 253 source files, ten Electron tests passed, Vite
  production build succeeded, and tracked-file scans found zero supplied-key or
  supplied-secret matches.

## M33 guarded IBKR live execution and Windows 0.2.0 package - 2026-08-03

Status: implemented as an isolated, human-authorized live execution desk on the
cross-platform research-client branch. Research, Agents, monitoring, and scheduled
workflows still have no order authority.

- Added an official `ibapi` adapter and a deep `ExecutionDesk` boundary restricted to
  IBKR live port 4001, `STK/SMART/USD`, long-only `DAY LMT` orders, and a securely
  bound single managed account. IBKR login, password, and MFA remain entirely inside
  TWS / IB Gateway.
- Added a fail-closed human workflow: live master switch, first-use masked account
  binding, five-minute write arming, fresh position/open-order checks, broker What-If,
  warning-bound dynamic confirmation, one-order-one-arm, persistent SQLite idempotency,
  and explicit recovery for uncertain submissions. Disabling the local switch does
  not cancel orders already held by IBKR.
- A real read-only handshake to the configured Gateway on port 4001 succeeded. A real
  AAPL one-share, one-dollar limit What-If request was accepted after normalizing the
  current IB API order fields; write authority was never armed and no live order was
  submitted.
- IBKR information code 2107 is treated as a non-fatal historical-data-farm standby
  notice. It means the broker channel reconnects when a historical request is made;
  it is separate from the client's bundled Massive history and incremental sync.
- Built the self-contained Windows 0.2.0 installer with a verified 1,771-dataset
  bootstrap archive. The packaged runtime and bootstrap hashes match their build
  outputs, and packaged-resource scans found no `.env`, imported profile, deprecated
  paper module, username, or password file.
- Repository acceptance: 512 Python tests passed, Ruff clean, strict mypy success
  across 261 source files, 27 Electron tests passed, 11 UI tests passed, and the Vite
  production build and Windows installer build succeeded.
- Honest remaining production gaps: unique `conId` / primary-exchange contract
  resolution, in-client cancel/modify commands, and continuous broker fill/order
  reconciliation. Those gaps prevent calling this execution module fully mature.

## M34 IBKR Paper automatic-execution audit boundary - 2026-08-03

Status: the Paper-only automatic execution path now records every executable
boundary before it can reach IBKR. It remains off until a current frozen plan,
current safety envelope, and broker-account risk baseline are all available.

- Added an append-only SQLite audit chain to the existing Paper session ledger.
  Each record has UTC time, canonical payload, previous hash, and event hash;
  credentials are rejected from audit payloads.
- Paper start records its frozen plan, policy evidence, source snapshot IDs and
  SHA-256 hashes. Every tick records broker account/positions/orders reads,
  exact SIP fact reads or failures, policy result, and every broker command
  request before the broker call plus its result or failure.
- Any audit-write failure blocks a new broker command. Existing durable command
  state remains the recovery source, so restart cannot issue a duplicate order.
- Account-summary compatibility now accepts either official prior-day-equity tag,
  while still failing closed if neither is returned. Security-definition-farm
  status 2157 no longer incorrectly rejects an otherwise established API socket.
- Acceptance: 51 targeted Paper/IBKR tests passed, Ruff clean, and strict mypy
  passed for all changed execution, autopilot, and test modules. A live read-only
  Paper probe remains blocked by IBKR status 2110 (TWS-to-server connection
  interrupted); no Paper order was submitted.

## M35 current-selection Paper plan preparation - 2026-08-03

Status: the local client can now prepare one bounded, immutable Paper plan from
today's accepted selection without giving an LLM authority over price or orders.

- Added a deterministic plan compiler. It accepts only the current usable
  `selection_gates.v2` snapshot, uses rank one only, rechecks RVOL, directional
  volume, premarket-VWAP confirmation, finite price facts, and timestamps, then
  atomically writes exactly one secret-free Paper config. Missing or stale facts
  produce no plan.
- The compiler sets a mechanical 2% hard stop, 10% maximum notional, 0.35%
  account-risk ceiling, 2.5R first target, and 3.0R weighted floor. These are
  conservative Paper defaults, recorded in plan provenance; the deterministic
  intraday policy still requires current technical facts, fresh safety envelope,
  live agent health, push health, and broker checks before any entry.
- Added an explicit client action, “从今日选股生成计划”. It only creates the
  frozen plan and adds a hash-chained audit record; it cannot connect IBKR,
  arm writes, or start execution. A separate Paper connection, safety validation,
  and typed start confirmation remain mandatory.
- Intraday monitoring now also subscribes to SPY and preloads recent SIP minute
  bars for the leading twelve candidates plus SPY. The warmup is append-only and
  idempotent; a retrieval or quality failure stops monitoring rather than
  manufacturing technical history.
- Acceptance: 14 focused Python tests passed, 31 Electron tests passed, 12 UI
  tests passed, Ruff clean, strict mypy passed for touched modules, and Vite
  production build succeeded. IBKR Paper status 2110 remains an upstream gateway
  blocker; no automated or manual Paper order was sent in this milestone.

## M36 desktop dynamic Paper safety loop - 2026-08-03

Status: Paper-only 4002 automation now refreshes its safety envelope from current
evidence before each deterministic decision. A missing dependency blocks new entry;
the automation never falls back to live 4001.

- Added a pinned OpenRouter JSON client for Catalyst and Red-Team safety outputs.
  The agent result must be one schema-valid JSON object and report the exact configured
  model; malformed, partial, timeout, or route-mismatch responses are unsafe.
- The local runtime now combines ticker-filtered Massive news, append-only SIP quotes,
  a read-only IBKR Paper account check, deterministic supervisor checks, and verified
  Livermore-channel health. Assessment files, final schema-valid model JSON with
  prompt hashes and provider usage, safety envelope, event inputs, broker reads,
  refresh outcomes, and hash-chain audit events are retained under the local research
  runs directory.
- The desktop encrypts the two configured runtime models and optional Livermore App
  ID/Secret/channel ID in OS secure storage. Secrets are passed only as child-process
  environment values, never command arguments, renderer state, Git, bootstrap data,
  or audit rows. Paper requires all three push values before safety refresh is enabled.
- Paper workflow is now: create today’s rank-one frozen plan, connect Paper 4002,
  refresh safety, validate current envelope, then type the dynamic confirmation to
  start. Each 15-second cycle refreshes safety before reading facts. Failure writes an
  immediately unsafe envelope, so the deterministic executor fails closed.
- News-agent classifications are cached only when the bounded fact package is unchanged;
  an observed new/removed source changes the prompt hash and triggers both agents again.
  This preserves fresh safety evidence without paying for duplicate model calls.
- Acceptance: 536 Python tests passed in 30.94 seconds; Ruff and strict mypy passed
  across 186 source files; 32 Electron tests, 12 UI tests, and Vite production build
  passed. No Paper or live order was submitted by this milestone’s verification.
- Windows packaging uses the project virtual environment explicitly, not a caller’s
  system Python. A local unsigned 0.2.1 installer is built after this milestone;
  its bundled runtime hash is checked against the source runtime before handoff.

## M37 desktop exchange-clock routing and runtime recovery - 2026-08-03

Status: the desktop now separates selection from review by the XNYS clock and
keeps an old review from being mistaken for current selection evidence.

- Selection is the only runnable workflow from Beijing 20:00 (US Eastern 08:00)
  through the current XNYS close. Post-close review opens 20 minutes after close.
  Other times are waiting states; the client disables the wrong button and states
  the next allowed workflow.
- Scheduler dispatches exactly one due stage. It no longer starts both the
  premarket and postmarket DAGs on each 15-second tick. A review receives the
  completed session date, never the next selection date.
- Read-only desktop status calls recover a stopped local runtime once, with one
  shared restart. Order, execution, and automatic-trading commands are never retried.
  A child-process exit clears its stale handle so the next start spawns a fresh runtime.
- Acceptance: 539 Python tests passed in 39.29 seconds; Ruff clean; strict mypy
  passed across 193 production source files; 33 Electron tests, 12 UI tests, and
  Vite production build passed. No Paper or live order was submitted.

## M38 three-symbol read-only monitor - 2026-08-04

Status: added independent MRVL, ON, and ALAB monitor entry points using the
existing 8765 Alpaca market-data API and UTF-8 VPS push client. Each polls every
10 seconds, pushes a first/changed buy, abandon, stop, or target event at most
once per event per 10 minutes, and sends a price/volume/VWAP summary every 15
minutes. No broker order endpoint is called.

- Entry points: `scripts/monitor_mrvl.py`, `scripts/monitor_on.py`, and
  `scripts/monitor_alab.py`; shared logic is in `scripts/monitor_target.py`.
- Buy-point state now persists the trigger count and writes the recommended next
  tranche size/notional; summaries include the recorded position from
  `runs/target-monitors/positions.json`. Stop/target alerts require a recorded
  positive position, and `--once --no-push` smoke runs do not write production state.
- The active runtime is now explicit `exit_only`: entry/abandon signals are
  disabled, ON is stopped, and only MRVL/ALAB exit alerts remain active.
- Secrets are read from `VPS_BUFFETT_APP_SECRET`; no secret is stored in source.
- Acceptance: full test suite 543 passed in 36.98 seconds; targeted monitor tests,
  Ruff, and formatting clean. Real read-only smoke reached 8765 and returned
  MRVL/ON/ALAB prices; two formal exit-monitor processes are running, and no
  order was submitted.

## M39 NVDA/DIS/LLY $30k watchlist plan - 2026-08-06

Status: added three watchlist entry points with a $10,000 per-symbol cap,
five staged tranches (15%/20%/25%/20%/20%), two consecutive 10-second entry
confirmations, and no broker order calls.

- Entry points: `scripts/monitor_nvda.py`, `scripts/monitor_dis.py`, and
  `scripts/monitor_lly.py`; shared logic remains in `scripts/monitor_target.py`.
- The plan uses the user-provided trigger, stop, and target levels. LLY has no
  invented fixed stop or target; its cost stop and 0.97 holding-ratio veto remain
  dependent on actual fills/manual confirmation.
- Acceptance: full test suite 568 passed in 34.55 seconds; Ruff, formatting, and
  strict mypy clean for all seven monitor modules. A real SIP smoke reached all
  three symbols; three formal watchlist processes are running and no order was
  submitted.

## M40 investment flywheel selection projection - 2026-08-06

Status: connected the accepted selection snapshot to the user's own Feishu
investment Base. The projection uses the Base's writable `运行ID` key and its
actual four-table field names; it records selection reasons, market-cap
availability, premarket evidence, next action, and paper-only plan state.

- Selection events are idempotent and read-back verified. Feishu datetime
  readback is normalized as UTC; Windows uses `lark-cli.cmd` with UTF-8 output.
- Only state transitions are projected. Per-second polling remains local and
  is not written to Feishu.
- Acceptance: 9 targeted Feishu/selection tests passed; Ruff clean on all
  changed Python files. The approved Base now contains 31 selection records for
  2026-08-06: MGNI, AEVA, SOUN, CLOV, CCI, RVMD, VSEC, CAI, EZPW, BBBY, SATL,
  TPC, TBLA, TTMI, EBAY, SBLK, MTDR, GTM, EXPE, RDW, MCK, PR, VECO, NUCL,
  GPN, DHT, ORA, EDIT, VVV, RIG, and KLIC. No broker order was submitted.

## M41 Alpaca direct market-data priority - 2026-08-07

Status: routed the public bars, quotes, trades, and monitoring-coverage seams to
direct Alpaca SIP REST by default. The broken local market proxy is no longer on
the default path; `MARKET_DATA_PROVIDER=cloud_proxy` is required to opt into it.
Direct credentials accept the canonical `ALPACA_API_*` names and the existing
Paper key aliases, remain in environment variables, and are never logged.

- Direct rows retain UTC timestamps, SIP feed, split adjustment, and Alpaca
  provenance. Empty or incomplete upstream coverage fails closed; no filling or
  interpolation was added.
- Health checks now validate credentials for the selected provider. The
  production deployment example explicitly selects the cloud proxy because that
  deployment owns its credentials; local `.env.example` defaults to direct Alpaca.
- Acceptance: full test suite 570 passed in 34.70 seconds; 51 targeted provider,
  health, deployment, and notification tests passed; Ruff and strict mypy clean
  on the changed production modules. No broker order was submitted.

## M42 enterprise repository foundation - 2026-08-07

Status: established the first enterprise governance layer in the dedicated
`codex/enterprise-foundation` worktree. `main` remains untouched. The repository
now documents task-isolated worktrees, reviewed-commit deployment, rollback,
security handling, operational recovery, and the modular-monolith decision.

- Added `CONTRIBUTING.md`, `SECURITY.md`, `CHANGELOG.md`, `cliff.toml`,
  `.github/pull_request_template.md`, and the pending-owner `.github/CODEOWNERS`
  placeholder. Added `docs/DEPLOYMENT.md`, `docs/RUNBOOK.md`, and ADR-0001.
- Added PR/main CI for Python tests, Ruff, strict mypy, dependency consistency,
  Electron tests, UI tests, Vite build, and pip-audit. No broker-write flag is
  present in CI.
- Added the database ownership inventory and migration contract without moving,
  dropping, or fabricating any existing table.
- Added an executable SQLite migration runner and connected the scheduler job
  ledger and execution order ledger. Legacy `run_token` schemas upgrade in place;
  failed migrations roll back both schema and version record.
- Acceptance: governance tests 3 passed, dependency-boundary tests 7 passed, and
  migration/ledger tests 13 passed; full Python tests 578 passed in 39.27
  seconds; Ruff clean; strict mypy clean across 290 source files; Electron tests
  33 passed; UI tests 12 passed; Vite production build passed. `pip-audit` was
  installed but its PyPI advisory lookup failed with a local TLS EOF, so the
  security audit remains a CI follow-up rather than a passing local result.

## M43 execution-boundary hardening - 2026-08-07

Status: extended the migration contract to the Paper session ledger and the
Alpaca SIP event store. Existing SQLite files remain readable and receive an
immutable owner/version/checksum record on first startup; migration callbacks
use separate statements inside the runner's transaction rather than
`executescript`, preserving rollback behavior.

- Added a shell-free, timeout-bounded child-process seam for the premarket,
  postmarket, research, Paper, and monthly schedulers. The seam returns a
  stable result object and never logs child stdout/stderr contents.
- Scheduler wrappers retain their existing event names and test seams while
  sharing command execution, timeout validation, and elapsed-time measurement.
- Acceptance: 11 migration/session/SIP tests passed; scheduler and deployment
  regression tests 30 passed; Ruff clean; strict mypy clean across 7 changed
  scheduler/test modules. No broker order was submitted.

## M44 order-protection ledger governance - 2026-08-07

Status: connected the time-exit and synthetic-stop SQLite ledgers to the same
versioned migration runner. These ledgers are part of the trading plane's
idempotency and outbox recovery path; their schemas now have explicit owners,
immutable checksums, and transactional first-startup initialization.

- Existing files remain compatible because migrations use `CREATE TABLE IF NOT
  EXISTS`; no order or stop records are moved or deleted.
- Acceptance: time-exit, synthetic-stop, and Alpaca Paper broker tests 24
  passed; Ruff clean; strict mypy clean across 4 changed production/test
  modules. No broker order was submitted.

## M45 safety and notification ledger governance - 2026-08-07

Status: migrated the account-exclusivity guardian, global emergency stop, and
autonomous notification outbox to the shared SQLite migration contract.

- The notification ledger preserves the legacy three-column schema through a
  two-step additive migration, so existing claimed/sent records remain
  readable while delivery fields are introduced transactionally.
- Safety stores now have explicit migration owners and immutable checksums;
  the emergency-stop singleton is initialized as part of its migration.
- Acceptance: account guardian, emergency stop, adaptive API, HTTP auth, and
  notification tests 30 passed; Ruff clean; strict mypy clean across the
  changed production/test modules. No broker order was submitted.

## M46 adaptive and broker ledger governance - 2026-08-07

Status: connected the adaptive-plan event store, autonomous Paper session
ledger, IBKR Paper idempotency ledger, and IBKR execution ledger to the
versioned SQLite migration runner. The largest trading-plane outbox schema no
longer relies on `executescript` during process startup.

- Existing tables remain additive and compatible; no plans, audit events, or
  broker receipts are deleted or rewritten.
- Acceptance: adaptive-plan/API, autonomous Paper, and IBKR tests 87 passed;
  Ruff clean; strict mypy clean across the changed production/test modules.
  No broker order was submitted.

## M47 cache, Feishu lock, and agent-fact governance - 2026-08-07

Status: migrated the cloud-feature cache, Feishu local write lock, and SQLite
agent fact store to the shared migration runner. The Feishu change only
governs the local mutex database; it does not access or alter any remote Base.

- The agent fact store keeps its existing allowlisted tables, constraints, and
  indexes while applying each DDL statement inside the migration transaction.
- The cloud cache retains point-in-time rows and the Feishu lock retains its
  singleton mutual-exclusion semantics.
- Acceptance: agent gateway, cloud-feature, Feishu Base client, and related
  tests 30 passed; Ruff clean; strict mypy clean across 6 changed modules.
  No remote Feishu Base was read or written; no broker order was submitted.
