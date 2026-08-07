# Security policy

## Scope

This repository handles market data, research evidence, Paper trading state,
Feishu audit projections, and optional broker adapters. It must not contain live
credentials, account passwords, private keys, or copied runtime databases.

## Reporting

Do not open a public issue for a credential, access-control, order-routing, or
data-exfiltration problem. Until the GitHub remote and private reporting owner
are configured, preserve the evidence locally and notify the repository owner
through the approved private channel.

## Required controls

- Keep secrets in `.env`, OS secure storage, or the deployment secret manager.
- Do not print request headers, tokens, provider response bodies, or account IDs.
- Keep Paper and live execution credentials and endpoints separate.
- Keep `BROKER_WRITE_ENABLED=false` and `TRADING_KILL_SWITCH=true` by default.
- Treat provider responses, Feishu fields, files, and agent output as untrusted.
- Do not add a short branch, hidden fallback, synthetic performance evidence, or
  an LLM call to `kernel/`.
- Rotate a credential immediately if it appears in a commit, log, screenshot, or
  chat transcript; invalidate the exposed credential before cleanup.

## Security validation

Pull requests run dependency auditing and the repository's deterministic tests.
Production changes require a reviewed commit, migration evidence, health output,
and a documented rollback point.

