# Database ownership and migrations

## Current state

The repository is not a single-database application. It has domain-owned SQLite
ledgers and an optional Postgres adapter for agent facts. SQLite startup schemas
are applied through `db/migrations/sqlite.py`; `db/schema.sql` remains a pointer,
not a complete production schema. Postgres is governed by the checked-in
`deploy/postgres/001_agent_facts.sql` baseline and is intentionally not silently
mutated by the SQLite runner.

| Owner | Current storage | Current schema location | Migration status |
|---|---|---|---|
| `schedule.job_ledger` | `runs/jobs.sqlite3` | `schedule/state.py` | versions 1-2 |
| `execution.order_ledger` | order plans/events | `execution/ledger.py` | versions 1-2 |
| `execution.paper_session_ledger` | complete Paper sessions | `execution/session_ledger.py` | version 1 |
| `execution.sip_event_store` | SIP bars/quotes/trades | `execution/sip_store.py` | version 1 |
| `execution.autonomous_paper_session` | autonomous Paper outbox/audit | `execution/autonomous_paper_session.py` | version 1 |
| `execution.account_guardian` | account day locks | `execution/account_guardian.py` | version 1 |
| `execution.time_exit_ledger` | time-exit actions | `execution/time_exit.py` | version 1 |
| `execution.synthetic_stop_ledger` | synthetic-stop outbox | `execution/synthetic_stop_controller.py` | version 1 |
| `execution.ibkr_paper_broker` | IBKR Paper receipts | `execution/ibkr_paper_broker.py` | version 1 |
| `execution.ibkr_execution` | IBKR manual execution receipts | `execution/ibkr_execution.py` | version 1 |
| `operations.adaptive_plan_store` | adaptive plans/events | `operations/adaptive_plan_store.py` | version 1 |
| `operations.autonomous_notifications` | push delivery outbox | `operations/autonomous_notifications.py` | versions 1-2 |
| `operations.emergency_stop` | global stop singleton | `operations/emergency_stop.py` | version 1 |
| `operations.feishu_write_lock` | local Feishu mutex only | `operations/feishu_base.py` | version 1 |
| `agent_gateway.sqlite_fact_store` | local agent facts | `agent_gateway/store.py` | version 1 |
| `data_plane.cloud_feature_cache` | PIT cloud feature cache | `data_plane/cloud_features.py` | version 1 |
| `agent_gateway` Postgres | production agent facts | `deploy/postgres/001_agent_facts.sql` | external baseline |

## Migration contract

Executable SQLite migrations are declared beside the owning adapter and applied
by the shared runner in `db/migrations/sqlite.py`. The runner records owner,
version, checksum, applied UTC time, and result through transaction success or
rollback. A migration must:

- be additive or have an explicit, tested rollback/roll-forward plan;
- preserve order/event ledgers and client-order idempotency;
- run with bounded locks and a visible failure state;
- be tested against a fresh database and a copy of the latest accepted database;
- never read or write production credentials from source control;
- be included in the deployment record before the process is restarted.

All current SQLite owners have a versioned baseline. No table is moved or dropped
by these migrations. Future schema changes must migrate one owner at a time with
backup and restore evidence, and must add a regression test for both a fresh
database and the latest accepted legacy shape.

## Operator verification

Startup applies only the owning database's pending migrations. To inspect the
record without exposing application data:

```sql
SELECT owner, version, name, applied_at_utc
FROM schema_migrations
ORDER BY owner, version;
```

Never delete `schema_migrations`, order ledgers, event ledgers, or broker
receipts during rollback. Use a compatible roll-forward when a schema change is
not safely reversible.
