# Governed Agent evolution

## What is implemented

The Agent layer is a research-only slow loop around the deterministic trading kernel.
It uses the official MCP Python SDK over stdio and provides fourteen audited tools:
selection/features, deterministic sizing/exits, parameterized fact queries, shadow
TradePlan drafts, theses, discipline reports, lessons, and proposals.

Eight MCP server entries bind one process to one role (`commander`, `risk`,
`factor-hunter`, `order-flow`, `short-thesis`, `sentiment`, `discipline`, and `pdca`).
The server rejects a caller that does not match the process identity or its tool
allowlist. This makes permissions executable policy rather than prompt text.

Two validated plugin skills are active:

- `postmarket-pdca`: discipline first, then ticker-anonymous selection review;
- `monthly-evolution`: evidence clustering and draft-only research proposals.

The postmarket systemd job invokes structured PDCA after immutable Episode creation and
the deterministic program gate. Complete selection outcomes are also materialized into the
ticker-free `lessons` ledger even when the LLM program gate is closed; after-cutoff and
incomplete-evidence rows remain only in the immutable postmortem snapshot. The monthly timer
runs only on the first XNYS session and requires both enough observations and independent
trading sessions before it can ask for a draft proposal. Provider calls and every MCP
tool request/result are retained in the audit fact store.

## Safety boundary

- The Agent package imports no Broker adapter and exposes no order-submission method.
- `tradeplan_submit` stores only `shadow_draft`, sets `execution_eligible=false`, records
  `broker_submission_count=0`, and does not enter the OMS.
- Missing data remains `N/A`; no model is allowed to fill it.
- Lesson text is rejected if a known ticker appears.
- Model-created numeric facts are impossible: models may cite only fact references that
  the program resolves back to frozen provenance-bearing values.
- Monthly proposals are constrained to `draft` and `production_eligible=false` in Pydantic,
  service logic, SQLite checks, and PostgreSQL checks.
- Selection lessons are evidence, not automatic parameter changes. Monthly proposals remain
  draft-only and require human review plus sandbox validation.

## Deliberately unavailable today

Order imbalance/VPOC and related SIP features are now deterministic shadow snapshots.
The order-flow Agent reads them when the target-date snapshot exists and degrades every
metric to `N/A` when it does not. FINRA short-volume and materialized factor-health
snapshots still lack verified sources, so those tools remain `N/A`. The current local
environment also has no durable Paper order ledger or barrier-event table, so the real
postmarket discipline report correctly records `incomplete_evidence`.

These gaps prevent false confidence but do not block continued selection-history research.
They must be supplied before execution-gap or cost-drift lessons can be considered complete.

## Deployment inputs still required

- a PostgreSQL DSN for the production audit store, or acceptance of the SQLite fallback;
- the deployed Paper order-ledger path and persisted barrier events;
- a verified FINRA daily short-volume source if that role is to move beyond `N/A`;
- deployment of the versioned cloud SIP trades route and a valid licensed SIP entitlement;
- a stable private anonymization salt;
- the already configured DeepSeek key in the server environment;
- external alert acknowledgement, data-license evidence, credential rotation, and the
  existing Paper/live maturity evidence described in `PRODUCTION_DEPLOYMENT.md`.
