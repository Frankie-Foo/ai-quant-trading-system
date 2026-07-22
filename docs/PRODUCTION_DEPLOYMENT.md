# Production deployment

The supported production shape is a Linux one-shot process launched by a systemd
timer. The process is not a daemon: every invocation acquires a cross-process lock,
discovers due XNYS sessions, claims a versioned SQLite lease, reuses immutable accepted
artifacts, writes JSON events to stdout, and exits. systemd owns scheduling, logs,
timeouts, and restart visibility.

## Review policy

The postmarket path is hybrid rather than agent-only or program-only:

1. deterministic code builds the immutable Episode and calculates data-quality,
   sample-size, outcome, and cost diagnostics;
2. missing bars, missing net costs, or inadequate samples stop research admission;
3. after all program gates pass, DeepSeek Research and Critic contexts run independently
   when `--llm-mode optional` or `required` is selected;
4. structured agent output can recommend a sandbox test but cannot write strategy code,
   approve production, or submit an order;
5. deterministic purged walk-forward and Champion/Challenger gates remain authoritative.
6. the structured PDCA pass writes a discipline report and ticker-anonymous lessons only
   after the same deterministic evidence gates;
7. the monthly evolution job can write only idempotent draft proposals and never applies
   them.

`optional` is the production default: an LLM outage is recorded but does not destroy
the deterministic review. It also does not silently advance an experiment. `off` is
available for fully programmatic operation; `required` makes an eligible agent review
failure fail the scheduled job.

## Host layout

- code and virtual environment: `/opt/trading-system` (read-only to the service);
- data and SQLite ledger: `/var/lib/trading-system`;
- agent audit/fact database: PostgreSQL through `QUANT_AGENT_POSTGRES_DSN`, or the
  fail-closed SQLite path in `QUANT_AGENT_STATE_DB`;
- runtime lock: `/run/trading-system/postmarket.lock`;
- verified backup archives: `/var/backups/trading-system`;
- secrets: `/etc/trading-system/trading-system.env` with mode `0600`;
- logs: systemd journal as one JSON object per line.

## Install on Linux

Create an unprivileged account and install the code:

```bash
sudo useradd --system --home /opt/trading-system --shell /usr/sbin/nologin trading
sudo install -d -o root -g trading -m 0750 /opt/trading-system
sudo rsync -a --delete --exclude .env --exclude .venv --exclude data --exclude runs \
  ./ /opt/trading-system/
sudo chown -R root:trading /opt/trading-system
sudo install -d -o trading -g trading -m 0700 /var/lib/trading-system/data
sudo install -d -o trading -g trading -m 0700 /var/lib/trading-system/state
sudo install -d -o trading -g trading -m 0700 /var/backups/trading-system
sudo python3.12 -m venv /opt/trading-system/.venv
sudo /opt/trading-system/.venv/bin/pip install \
  -r /opt/trading-system/requirements-prod.txt
```

Install secrets without copying the developer `.env`:

```bash
sudo install -d -o root -g root -m 0700 /etc/trading-system
sudo install -o root -g root -m 0600 \
  /opt/trading-system/deploy/trading-system.env.example \
  /etc/trading-system/trading-system.env
sudoedit /etc/trading-system/trading-system.env
```

Install all one-shot services and timers:

```bash
sudo install -o root -g root -m 0644 \
  /opt/trading-system/deploy/systemd/*.service /etc/systemd/system/
sudo install -o root -g root -m 0644 \
  /opt/trading-system/deploy/systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now \
  trading-premarket.timer trading-paper.timer trading-postmarket.timer \
  trading-backup.timer trading-research.timer trading-monthly-evolution.timer
```

If local accepted snapshots are being migrated, copy the entire immutable data tree to
`/var/lib/trading-system/data` before the first manual run and preserve manifests and
directory names.

## Verify and operate

Run the read-only readiness check and one job invocation:

```bash
sudo -u trading /opt/trading-system/.venv/bin/python -m schedule.health \
  --data-root /var/lib/trading-system/data \
  --state-db /var/lib/trading-system/state/jobs.sqlite3 \
  --check-credentials
sudo systemctl start trading-postmarket.service
```

Before enabling the Paper timer, generate the first evidence file and run the local
safety drills. Neither command submits orders:

```bash
sudo -u trading /opt/trading-system/.venv/bin/python \
  -m scripts.refresh_maturity_evidence \
  --data-root /var/lib/trading-system/data \
  --order-db /var/lib/trading-system/state/paper-orders.sqlite3 \
  --evidence /var/lib/trading-system/state/maturity-evidence.json
sudo -u trading /opt/trading-system/.venv/bin/python \
  -m scripts.run_local_safety_drills \
  --data-root /var/lib/trading-system/data \
  --state-root /var/lib/trading-system/state \
  --backup-dir /var/backups/trading-system \
  --receipt-dir /var/lib/trading-system/state/drills \
  --evidence /var/lib/trading-system/state/maturity-evidence.json
```

Inspect status and structured logs:

```bash
systemctl list-timers 'trading-*'
systemctl status trading-premarket.service trading-paper.service \
  trading-postmarket.service trading-backup.service
journalctl -u 'trading-*' --since today -o cat
```

`trading-postmarket.service` now runs the deterministic review before structured PDCA.
If trade plans, executions, or barrier events are absent from the configured read-only
order ledger, it writes `incomplete_evidence` rather than claiming discipline compliance.
`trading-monthly-evolution.timer` checks the first XNYS session from the exchange calendar;
insufficient lesson clusters or missing factor-health snapshots produce no proposal.

For PostgreSQL, apply `deploy/postgres/001_agent_facts.sql` with an administrative account,
then give the service account access only to the `quant_agent` schema. Keep the DSN in the
root-owned environment file. If PostgreSQL is omitted, set `QUANT_AGENT_STATE_DB` to the
writable state directory. Set `QUANT_AGENT_ANONYMIZATION_SALT` to a stable private value.

`trading-backup.timer` runs weekly. It uses SQLite's online backup API, includes the
immutable accepted-data tree, writes a SHA-256 manifest, restores the archive into a
temporary directory, verifies every hash, and runs `PRAGMA quick_check` on restored
databases before reporting success. A failure triggers the alert unit.

`trading-research.timer` runs the cross-platform weekly learning cycle. It incrementally
fills the 504-session feature/target daily window and aligned news/reference history,
rebuilds 252 point-in-time selections, refreshes SIP costs and purged OOS folds, runs the
allowlisted Champion/Challenger sandbox, and refreshes maturity evidence. Provider and
snapshot caches make reruns restart safe. Research decisions cannot update production
configuration or submit orders.

Set `ALERT_WEBHOOK_URL` to an HTTPS endpoint that returns HTTP success. Prefer a response
containing `ack_id` or `id`; the durable receipt records that acknowledgement. Conduct a
real alert test after server installation—unit tests and a local mock receipt do not
satisfy the external alert-delivery gate.

Record a completed external control only with its durable evidence identifier:

```bash
sudo -u trading /opt/trading-system/.venv/bin/python \
  -m scripts.record_attestation \
  --field historical_data_license \
  --evidence-ref 'contract-or-plan-receipt-id' \
  --data-root /var/lib/trading-system/data \
  --order-db /var/lib/trading-system/state/paper-orders.sqlite3 \
  --evidence /var/lib/trading-system/state/maturity-evidence.json
```

The CLI supports revocation and never changes objective sample metrics. `live_eligible`
still leaves `approved_for_live=false`; no attestation command arms Broker writes.

## Container execution

The `Dockerfile` creates a non-root, one-shot image. Supply secrets as runtime
environment variables and mount `/var/lib/trading-system`; never bake `.env` into an
image. Scheduling should remain outside the container (systemd, Kubernetes CronJob, or
the cloud provider's job scheduler).

Before production deployment, rotate any credential that has ever been pasted into a
chat or terminal transcript. Delayed full-market data, censored minute paths, and
missing quote-spread costs still prohibit live trading and performance claims.

## Keyless cloud market-data process

The cloud-strategy-platform process owns the only Alpaca SIP WebSocket and all Alpaca
credentials. This repository consumes its scoped HTTPS event API and stores a local
SQLite view; it never opens an Alpaca connection.

Verify entitlement first:

```bash
sudo -u trading /opt/trading-system/.venv/bin/python \
  -m scripts.verify_alpaca_access \
  --symbols AAPL \
  --lock-file /run/trading-system/alpaca-sip.lock
```

Run the bounded-universe collector with the symbols from the accepted locked selection:

```bash
sudo -u trading /opt/trading-system/.venv/bin/python \
  -m scripts.stream_alpaca_sip \
  --symbols AAPL,MSFT \
  --state-db /var/lib/trading-system/state/sip-stream.sqlite3 \
  --lock-file /run/trading-system/alpaca-sip.lock
```

The AI service environment uses only scoped platform tokens:

```text
CLOUD_PLATFORM_BASE_URL=https://cloud-strategy-platform.example.internal
CLOUD_MARKET_DATA_API_TOKEN=<secret-manager-reference>
CLOUD_PAPER_API_TOKEN=<secret-manager-reference>
CLOUD_FEATURE_API_TOKEN=<secret-manager-reference>
CLOUD_MARKET_DATA_FEED=sip
BROKER_WRITE_ENABLED=false
TRADING_KILL_SWITCH=true
```

The market token cannot read Paper state or place orders. The Paper token cannot access
collaborator signals. Never copy the cloud service's Alpaca credentials into this
repository or its runtime environment.
