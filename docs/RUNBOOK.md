# Operations runbook

## First response

1. Record UTC time, commit SHA, process, task/worktree, and the first sanitized
   error.
2. Check readiness and the latest scheduler lease before restarting anything.
3. Keep `BROKER_WRITE_ENABLED=false` and `TRADING_KILL_SWITCH=true` while the
   failure mode is unknown.
4. Do not retry an order or Feishu event by hand; use the idempotent recovery
   command and inspect the ledger first.

## Market data unavailable

The default local path is direct Alpaca SIP. Check the provider selection and
credential presence without printing values:

```powershell
python -m schedule.health --check-credentials
python -m data_plane.cli credentials
```

If Alpaca data is unavailable, the affected selection or monitoring stage must
stop. Do not substitute Yahoo, community data, stale snapshots, or interpolated
bars. A cloud proxy is valid only when explicitly selected and its coverage
contract passes.

## Scheduler stopped or duplicated

- Inspect the SQLite job ledger and process lock.
- Confirm the latest state transition and retry count.
- Remove only a verified stale lease through the documented recovery command.
- Restart one stage once; do not start parallel copies of the same stage.

## Feishu projection failure

Feishu is an audit projection, not a source of trading truth. Verify the approved
Base/table allowlist, event identity, local idempotency ledger, and read-back
result. Never access the old automation Base or a reference Base. A failed
projection must not create a duplicate Paper order or be reported as a fill.

## Paper safety incident

1. Enable the kill switch and disable broker writes.
2. Preserve the order ledger, broker response, notification, and runtime logs.
3. Reconcile by deterministic client order ID before any retry.
4. If state is uncertain, enter `recovery_required`; do not create a new order.
5. Record the incident and rollback point in the release record and `PROGRESS.md`.

