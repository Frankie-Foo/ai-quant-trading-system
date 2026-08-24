# Architecture decisions

This document maps the useful parts of *AI Quant Platform Layered Architecture
Design* into the frozen Trading System v2 specification. The PDF is treated as a
high-level design reference, not as evidence that any trading signal is profitable.

## Layer mapping

| PDF layer | Repository boundary | Responsibility |
|---|---|---|
| Data | `data_plane/`, `db/`, M1/M2 adapters | Ingestion, quality, immutable snapshots, lineage, point-in-time features |
| Model | `research/`, training jobs, `kernel/meta.py` inference | Train, validate, register, and serve model outputs; never create orders directly |
| Strategy | `kernel/universe.py`, `factor_selection.py`, `selection_arbitration.py`, `signals.py`, `meta.py`, `sizing.py`, `exits.py` | Generate independent candidate evidence, arbitrate research ranks, and convert approved inputs into a long-only `TradePlan` |
| Trading | `execution/`, `kernel/guardrails.py`, future broker adapters | OMS state, EMS child orders, pre-trade checks, fills, reconciliation |
| Application | `agent_gateway/`, `plugins/`, `schedule/` | Human interaction and slow-loop orchestration only |

The existing fast-loop/slow-loop split remains authoritative. This mapping adds
clear ownership boundaries without moving or renaming any frozen interface.

## Adopted changes

### 1. Immutable data snapshots and four quality checkpoints

Every consumed dataset receives a content hash, schema version, source, UTC `asof`,
parent snapshot IDs, and quality results. Critical failures quarantine the snapshot;
warnings remain visible but do not silently change data.

M1 will enforce checks at:

1. adapter output;
2. normalization output;
3. database write/read reconciliation;
4. feature/backtest consumption.

Missing or halted intraday bars are never forward-filled. This deliberately rejects
the PDF's generic suggestion that missing financial data may be imputed: doing so in
minute data would create artificial tradability and false returns.

### 2. Offline/online feature parity

Backtests and live inference must call the same point-in-time feature functions.
Precomputed features are a cache, not an alternative definition. Each feature will
carry a definition version, code hash, input snapshot IDs, `asof` policy, null policy,
and frequency. A change to any of these creates a new feature version.

### 3. Research and model registry before model selection

Each experiment is registered with immutable data, feature, config, and code hashes,
a fixed random seed, chronological train/validation/test windows, and the number of
attempted configurations. A model artifact is not eligible for M7 unless its test
window is untouched and strictly later than its tuning window.

Automatic retraining is not automatic promotion. Drift or performance decay may
open a new research run, but human approval and the M7 validation gates remain
mandatory before production use.

### 3a. Two-agent postmarket research, deterministic arbitration

Postmarket learning uses two independent prompt contexts over the same immutable
Trading Episode. A read-only Research Agent may emit zero to five falsifiable
hypotheses. A read-only Critic Agent attempts to reject the structured proposal for
look-ahead leakage, overfitting, unsupported causality, missing costs, or insufficient
evidence. Neither role has execution tools or production-promotion authority.

Task scheduling, point-in-time facts, labels, minimum sample requirements, purged
walk-forward evaluation, Champion/Challenger comparison, and promotion remain coded
contracts. Missing minute paths are censored rather than interpolated. Agent output can
reach only `eligible_for_sandbox_experiment`; it can never approve itself for the kernel.

The research registry also applies the governed AI4S cycle described in
`AI4S_RESEARCH_LOOP.md`. Every evaluated strategy claim records one falsifiable
hypothesis, changed variable, control, validation plan, evidence package, and
deterministic admission decision. Historical success can admit only a Paper experiment;
Paper evidence can admit only human review. Neither stage grants production eligibility.

Before either agent runs, a deterministic program review calculates history counts,
censored paths, gross and net label availability, and allowlisted experiment metadata.
History is restricted to sessions at or before the reviewed date; agent context contains
the program review plus at most the 500 most recent bound rows. The default hybrid mode
uses agents when these gates pass and degrades to a recorded, non-advancing program
review when the model provider is unavailable.

The server runtime is a one-shot job rather than an application daemon. systemd, cron,
or a cloud scheduler invokes it; a cross-process file lock and SQLite lease token prevent
overlap and stale-owner completion. Each lifecycle event is one UTC JSON log record.

### 3b. Least-privilege Agent gateway and governed PDCA

The optional multi-agent surface is an audited MCP boundary around the deterministic
kernel, not a second trading engine. Eight role-bound server processes expose only their
allowlisted tools. Every call stores the raw structured request/result and sanitized
failure code. Numeric facts require provenance; missing order-flow, short-flow, sentiment,
or factor-health data is returned as `N/A`.

Agent writes are limited to theses, discipline reports, ticker-anonymous lessons,
non-executable TradePlan drafts, and evolution proposals. TradePlan drafts always have
`execution_eligible=false`, a deferred fail-closed guardrail state, and zero Broker
submissions. Evolution proposals are constrained by both application and database checks
to `status=draft` and `production_eligible=false`.

Postmarket scheduling runs deterministic Episode and program review first, then the
discipline/PDCA slow loop. The monthly timer clusters only accepted anonymous lessons and
may create a research draft after minimum evidence gates. Neither path can modify
configuration, retrain a production model, write an OMS order, or call a Broker.

### 3c. Independent candidate generators and order-flow confirmation

The premarket research surface now has two independent candidate generators. The
catalyst branch retains its event-evidence lock and hard gates. The factor branch starts
from the broad point-in-time daily precheck universe, computes its own premarket RVOL,
and assigns a transparent 0-100 score without reading catalyst membership.

The order-flow module consumes consolidated SIP trades and top-of-book NBBO quotes. It
materializes Tick Rule buy/sell volume, order imbalance, pressure ratio, cent-bucket
VPOC, quote-size imbalance, microprice, and spread. This is consolidated-tape evidence,
not Level-2 or market-by-order depth. Every trade is retained; timestamps remain at
nanosecond precision through the cloud API and local snapshot.

`kernel.selection_arbitration` ranks the union of catalyst and factor candidates.
Cross-branch agreement earns a configurable bonus. Available order flow can only adjust
an existing candidate within a configured bound; absent order flow is neutral and
explicitly unavailable. The factor, order-flow, and unified snapshots are shadow-only,
and the orchestration script has no execution dependency.

### 3d. Cross-asset perpetual sentiment

The shadow DAG also reads public Hyperliquid and Aevo perpetual contexts through two
read-only adapters at one normalization seam. `kernel.cross_asset_sentiment` receives
only immutable `PerpObservation` values and has no HTTP, LLM, Broker, or OMS dependency.
It quality-gates freshness, liquidity, oracle basis, and spread before combining price,
open-interest confirmation, funding crowding, signed flow, liquidations, and basis.

Raw volume never supplies direction. Price/open-interest quadrants are scaled by the
continuous magnitude of both changes so tiny polling noise cannot become a full-strength
regime signal. Cross-venue disagreement reduces confidence. Missing venue fields remain
unavailable, and provider failure degrades only this shadow stage.

The default configuration maps only BTC and ETH to a global risk-appetite target. HIP-3
equity, commodity, and private-market mappings require explicit oracle/deployer/liquidity
review before configuration. Both raw and aggregate snapshots are immutable and
`production_eligible=false`; no live or Paper decision consumes them.

### 4. OMS/EMS separation and an explicit order state machine

`TradePlan` expresses intent. The OMS owns order identity, lifecycle, fills, and
audit history. The EMS owns tranche scheduling and participation-limited child
orders. Broker adapters only translate these contracts into broker-specific calls.

Illegal transitions are rejected. Partial fills are first-class events. Recovery
must be idempotent using `client_order_id`; process restart must not duplicate an
order. The initial state-machine contract lives in `execution/order_state.py`.

### 5. Cross-layer observability

M9 will attach one trace ID from data snapshot through feature computation,
`TradePlan`, guardrail verdict, child order, fill, and barrier exit. Required
operational measurements are data latency, feature latency, decision latency,
submission latency, fill latency, rejection reason, and P50/P95/P99 distributions.

## Explicitly not adopted now

- Kafka, Flink, Spark, Kubernetes, service mesh, and microservices: unnecessary for
  one owner, minute bars, and a $200k starting account. A modular monolith is safer
  and easier to audit at this stage.
- Reinforcement-learning trading and autonomous LLM execution: outside the frozen
  system objectives. LLMs stay in the slow loop; the fast loop remains deterministic.
- Independent agent veto power: conflicts with invariant I3. Only coded P0/P1/P2
  guardrails can block an order.
- Automatic feature/model promotion based only on online metrics: financial regimes
  and multiple testing require a human validation gate.
- Generic missing-value imputation for market bars: halted or absent bars stay
  missing and are surfaced through data quality results.

## Scale-up triggers

Distributed infrastructure is reconsidered only after measured pressure, such as a
single process failing the required latency SLO, storage exceeding the chosen engine's
tested capacity, multiple independent strategies requiring isolation, or broker/data
redundancy demanding separate failure domains. Technology fashion is not a trigger.

## External cloud feature boundary

The cloud multi-strategy platform is a separate folder, Python package, process,
database set, deployment identity, and Git repository. This repository does not import
it or open its databases. A versioned HTTPS API is the only integration boundary.

Slow-loop synchronization fetches authorized point-in-time feature vectors and writes
them to `runs/cloud-feature-cache.sqlite3`. Realtime strategy code may read that local
cache, but it must never make a decision-time HTTP request. Cloud outages therefore
degrade remote features to unavailable without blocking the existing single-strategy
SIP, selection, local observation, guardrail, or OMS paths.

The feature-service token is separate from collaborator signal grants and has no raw
market-data proxy or order scope. See [cloud feature interface](CLOUD_FEATURE_INTERFACE.md).
