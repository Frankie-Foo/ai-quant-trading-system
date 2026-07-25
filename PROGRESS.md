# Progress

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
