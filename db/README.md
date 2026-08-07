# Database ownership and migrations

## Current state

The repository is not a single-database application. It currently has domain-owned
SQLite ledgers and an optional Postgres adapter for agent facts. Several modules
create their tables idempotently at startup; `db/schema.sql` is not a complete
production schema. This document is the inventory boundary until executable
migrations are extracted.

| Owner | Current storage | Current schema location | Migration status |
|---|---|---|---|
| `schedule` | `runs/jobs.sqlite3` | `schedule/state.py` | inline bootstrap |
| `execution` | order, session, SIP, Paper ledgers | `execution/*.py` | inline bootstrap |
| `operations` | safety, notification, Feishu lock ledgers | `operations/*.py` | inline bootstrap |
| `agent_gateway` | SQLite or Postgres | `agent_gateway/store.py` | dual adapter, no version table |
| `data_plane` | immutable Parquet snapshots | `data/accepted` and quarantine paths | filesystem contract |

## Migration contract

Future executable migrations live under `db/migrations/<owner>/` and are applied
by an idempotent runner that records owner, version, checksum, started time,
finished time, and result. A migration must:

- be additive or have an explicit, tested rollback/roll-forward plan;
- preserve order/event ledgers and client-order idempotency;
- run with bounded locks and a visible failure state;
- be tested against a fresh database and a copy of the latest accepted database;
- never read or write production credentials from source control;
- be included in the deployment record before the process is restarted.

No table is moved or dropped in this foundation change. The next database task
must first capture each inline schema as a versioned baseline, then migrate one
owner at a time with backup and restore evidence.

