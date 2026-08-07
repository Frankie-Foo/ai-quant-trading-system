# Deployment

## Release source

Production deploys only a reviewed commit merged into `main`. A personal branch,
Codex worktree, dirty checkout, or unreviewed artifact is not a release source.
The deploy record must contain:

- repository and commit SHA;
- release version and changelog entry;
- test, lint, type, build, and security results;
- migration version and backup/restore evidence;
- health-check output and operator;
- rollback commit or image digest.

The detailed Linux and systemd procedure remains in
[PRODUCTION_DEPLOYMENT.md](PRODUCTION_DEPLOYMENT.md). This file defines the
release contract; the platform-specific document defines the commands.

## Pre-deploy gates

1. Pull request is approved and all required CI checks pass.
2. The target commit is on `main` and the worktree is clean.
3. Configuration is rendered from the deployment secret manager; no secret is
   copied into the repository or image.
4. Database migrations are reviewed, idempotent, and have a tested rollback or
   roll-forward path. Confirm the target SQLite owner/version rows from
   [db/README.md](../db/README.md); Postgres uses the checked-in SQL baseline.
5. Paper/live write controls and kill switches are verified before startup.
6. A backup or immutable snapshot exists before a stateful migration.

## Deploy and verify

Deploy the exact recorded commit. Run migrations before traffic or scheduled
work is enabled. Readiness must validate the real provider, storage, state
ledger, and migration version; process liveness alone is insufficient.

After startup, verify:

- the process can read approved market data without using an undeclared fallback;
- the scheduler has one owner and no stale lease;
- Paper order writes remain disabled unless the separately approved Paper gate is
  active;
- Feishu projection is restricted to the approved Base and is idempotent;
- logs contain correlation IDs, version, provider, latency, and sanitized errors.

## Rollback

Stop new scheduled work, preserve logs and state evidence, and restore the last
known-good commit or image. Never delete the order/event ledger during rollback.
If a migration is not safely reversible, roll forward with a compatible schema
and keep the old reader until the migration is verified.
