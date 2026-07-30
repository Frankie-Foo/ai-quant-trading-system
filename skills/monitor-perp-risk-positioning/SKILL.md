---
name: monitor-perp-risk-positioning
description: Collect and monitor read-only Hyperliquid and Aevo perpetual-market evidence, score cross-venue global/energy/semiconductor risk, and return auditable intraday position multipliers. Use when Codex needs to inspect perpetual sentiment, active order flow, Bid/Ask quality, funding, price/open-interest regimes, basis, or liquidation evidence; determine risk_on/neutral/risk_off direction; adjust a caller-supplied long-only position plan; run continuous monitoring; diagnose provider health; record outcomes; review signal quality; or configure this portable collector. Do not use it to select stocks, create orders, connect trading credentials, or authorize execution.
---

# Monitor Perpetual Risk Positioning

Use the bundled deterministic CLI as the source of truth. Treat model-written
commentary as an explanation of CLI output, never as a replacement score.

## Establish the runtime

Resolve `SKILL_ROOT` to the directory containing this file. Do not assume the
current working directory.

If `perp-risk` is not installed, create or reuse a Python 3.11+ virtual
environment and install the Skill:

```bash
python -m pip install "$SKILL_ROOT"
perp-risk doctor
```

On Windows, invoke the environment's `Scripts/perp-risk.exe` when it is not on
`PATH`. For Docker, run `docker compose up --build` from `SKILL_ROOT`.

Run `perp-risk smoke-live` after installation or when a provider contract may
have changed. It is read-only, does not persist, does not notify, and cannot
trade.

## Choose the workflow

### Inspect current risk

1. Run `perp-risk snapshot --no-notify`.
2. Read `provider_status`, target `coverage`, `regime`,
   `effective_multiplier`, `reasons`, and `actionable`.
3. Report missing evidence as unavailable. Never convert missing liquidation,
   flow, Bid/Ask, or provider data to zero.
4. If `actionable=false`, label the result research-only.

Cold starts lack a causal previous observation, so price-trend and price/OI
components remain unavailable. Allow the monitor to collect a second
independent window before interpreting full coverage.

### Apply the overlay to a position plan

Require the caller or parent strategy to supply relevant targets. Do not infer
a stock's industry:

```bash
perp-risk recommend \
  --targets global-risk,semiconductor-risk \
  --base-position-pct 10
```

Use only the returned `position_multiplier` or
`adjusted_target_position_pct`. Never create an order. Risk vetoes dominate:
any relevant multiplier below `1.0` caps the combined result; `1.2` is possible
only with complete strong evidence and no relevant risk veto.

### Run continuous monitoring

```bash
perp-risk watch
```

The default interval is 60 seconds. State changes are persisted in SQLite.
Webhook notification is disabled by default; when enabled, identical state is
deduplicated and a low-frequency heartbeat remains.

### Diagnose or integrate

- Run `perp-risk doctor` for offline configuration and storage checks.
- Run `perp-risk status` to read the latest persisted snapshot.
- Run `perp-risk schema --output-dir <dir>` to export JSON Schemas.
- Read [provider-contracts.md](references/provider-contracts.md) when adding an
  instrument, venue, VIX source, or liquidation source.
- Read [integration-contracts.md](references/integration-contracts.md) when
  wiring a parent strategy, webhook, outcome feed, or JSONL/HTTP liquidation
  adapter.
- Read [operations.md](references/operations.md) when installing, monitoring,
  backing up, restoring, or troubleshooting the runtime.

### Review and propose changes

Record benchmark and trade outcomes separately:

```bash
perp-risk record-outcome --file outcome.json
perp-risk review
```

Run `propose-config` only after at least 100 benchmark outcomes. It creates a
challenger and evidence report but cannot modify the active config. Before
running `approve-config`, show the candidate diff and hash to the user and
obtain explicit confirmation.

## Enforce the safety boundary

- Keep the Skill long-only and read-only.
- Never add broker keys, wallet signing, exchange actions, order construction,
  or execution branches.
- Let `risk_off` reduce exposure or recommend cash; never recommend a short.
- Apply multipliers to the parent strategy's target position, not account
  equity or current holdings.
- Do not allow `1.2x` without real liquidation evidence, at least two venues,
  at least 75% coverage, no venue conflict, and two independent windows.
- On unavailable or highly conflicted data, cap the recommendation at `0.5x`.
- Use actual provider receive time. Historical queries may read only persisted
  point-in-time data; never backdate a current HTTP response.
- Read secrets from OS keyring first and environment variables second. Never
  print, persist, package, or commit secret values.
- Keep JSON field names in English. Human explanations and notifications
  default to Chinese.
