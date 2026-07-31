# Intraday Trading System v2

Deterministic, auditable, long-only U.S. equity intraday research and execution
kernel. M0 is complete; no strategy performance claim is produced by this
repository yet.

## Local verification

```powershell
.\.venv\Scripts\python -m pytest -v
.\.venv\Scripts\ruff check .
.\.venv\Scripts\mypy data_plane kernel research execution schedule agent_gateway `
  scripts tests
```

Secrets belong in `.env`, which is ignored. Never paste credentials into reports or
commit them to the repository.

The frozen specification is supplemented by [architecture decisions](docs/ARCHITECTURE.md),
which map data, model, strategy, trading, and application layers onto this repository.

## 本地自主模拟盘

三进程 Docker 运行方式现已覆盖 SIP 数据刷新、催化剂/红队/确定性监督器、
安全信封、Alpaca Paper 执行和利弗莫尔中文通知。默认 compose 只有读取权限；
Paper 写入必须同时通过环境授权、关闭 kill switch，并显式加载带
`--arm-paper` 的覆盖文件。完整启动、紧急停止、状态保留和迁移说明见
[本地自主模拟盘运行手册](docs/AUTONOMOUS_PAPER_LOCAL.md)。

The cloud multi-strategy service is a separate repository and deployment. This
repository consumes its versioned feature API only through the slow-loop synchronization
adapter documented in [cloud feature interface](docs/CLOUD_FEATURE_INTERFACE.md). The
realtime kernel reads the local point-in-time cache and never waits on remote feature
HTTP. Its separate market collector declares a bounded lease, verifies detailed market
health, then consumes resumable cloud SSE into the existing local SIP store.

## macOS 本地研究客户端

`feature/macos-research-client` 提供一个与本地模拟盘隔离、但在 Mac 本机运行完整
研究内核的发行版。它内置 `data_plane/`、`kernel/`、`research/` 和 `schedule/`，
在用户目录保存 accepted 快照、任务账本和复盘结果；Electron 只负责选股、证据答疑、
收盘复盘和三个研究 Agent 的交互。首次启动填写用户自己的 OpenRouter Key，以及
固定 Alpaca SIP 代理的 Key/Secret；四个模型角色可独立选择。

固定代理地址为 `wss://alpaca-trade-api.vertu.cn/v2/sip`，程序会验证报价、成交和
分钟线订阅。代理只覆盖实时 SIP，因此历史日线、新闻和财务输入未配置时仍以
`historical_research_inputs_missing` 阻断完整选股，不使用历史名单伪造今日结果。
代理凭据由 macOS Keychain 加密保存，不写入源码、安装包或 Git。
发行包没有 Broker、OMS 或下单 IPC；IBKR Paper 接口预留但连接和下单均为
fail-closed。完整的本地数据目录、安全 seam、首次使用和双架构打包流程见
[macOS 研究客户端](docs/MACOS_RESEARCH_CLIENT.md)。

## Windows adaptive decision client

The desktop client is a read-only operating console over a deterministic adaptive-plan
engine. It reconciles positions from the Broker, evaluates observed SIP quotes/trades
and completed 1/5/15-minute bars every 15 seconds, and records only material state
changes in an append-only SQLite event stream. Soft plan revisions have a three-minute
cooldown and a per-session cap; hard stops and the UTC time stop remain immediate.
Neither the client nor its HTTP interface contains an order route.

To open the evidence-only console without starting market collection or Paper
monitoring:

```powershell
Set-Location client
npm.cmd run desktop
```

This view remains useful without a registered adaptive plan: it shows the latest
immutable selection and post-close review, durable job failures, runtime Agent
availability, and maturity gates, with stale evidence explicitly marked.

Install the JavaScript dependencies once, copy the secret-free example plan, replace
every placeholder with accepted point-in-time evidence, and start the complete local
loop:

```powershell
Set-Location client
npm install
Set-Location ..
Copy-Item config\adaptive_plans.example.json config\adaptive_plans.local.json
.\scripts\start_adaptive_client.ps1 -Config config\adaptive_plans.local.json
```

The launcher registers immutable baselines, warms the local store with historical SIP
observations, starts the licensed event collector and Broker-authoritative plan monitor,
and opens the Electron client. Closing the client stops only the background processes
owned by that launch. Full contracts, state transitions, safety boundaries, and VPS
deployment topology are documented in
[adaptive desktop client](docs/ADAPTIVE_DESKTOP_CLIENT.md).

Real-data bootstrap and local credential setup are documented in
[data access](docs/DATA_ACCESS.md). Community and undocumented feeds are automatically
quarantined and cannot be used as performance evidence.

Observed download counts and quarantine reasons are recorded in the
[data bootstrap report](docs/DATA_BOOTSTRAP_REPORT.md).

Build and persist the fail-closed daily common-stock pre-universe with:

```powershell
.\.venv\Scripts\python -m scripts.build_daily_universe --trade-date 2026-07-17
```

The first real run and its remaining data gates are documented in the
[daily universe audit](docs/UNIVERSE_AUDIT_2026-07-17.md).

Build the Beijing 08:00 point-in-time overnight catalyst evidence snapshot with:

```powershell
.\.venv\Scripts\python -m scripts.build_catalyst_snapshot --trade-date 2026-07-20
```

Reapply deterministic cleaning rules to accepted raw provider snapshots without making
network requests:

```powershell
.\.venv\Scripts\python -m scripts.build_catalyst_snapshot --trade-date 2026-07-20 `
  --reuse-provider-snapshots
```

The first real evidence run is documented in the
[catalyst audit](docs/CATALYST_AUDIT_2026-07-20.md).

Prefetch the prior 20 same-time premarket windows and, once the Beijing 20:00
decision time has arrived, build the locked-pool RVOL snapshot with:

```powershell
.\.venv\Scripts\python -m scripts.build_premarket_rvol --trade-date 2026-07-20
```

The command is restart-safe: accepted per-session raw snapshots are reused. Before
the decision time it downloads history only and leaves the target feature unavailable;
at or after the decision time it adds the target window allowed by the configured feed
policy and persists the point-in-time result. Licensed `sip` uses a zero-minute delay;
`delayed_sip` is an explicit 15-minute fallback. RVOL is deliberately unsigned, so it
cannot pass the final gate by itself: the same snapshot must also show a positive
04:00-to-cutoff return, a close above aggregate premarket VWAP, and a close in the top
40% of the observed premarket range. The final gate additionally requires the
premarket close to be strictly above the prior close. See the historical
[premarket RVOL audit](docs/PREMARKET_RVOL_AUDIT_2026-07-20.md).

Prefetch and apply the remaining L0 selection gates (point-in-time market cap,
earnings day, current halt, recent LULD/low-float risk, and prior-session bearish
distribution) with:

```powershell
.\.venv\Scripts\python -m scripts.build_selection_gates --trade-date 2026-07-20
```

The bearish-distribution veto rejects a prior session whose open-to-close return is
at most -3%, volume is at least 1.5 times the preceding 20-session average, and close
location is in the bottom 30% of the daily range. These thresholds are frozen and
visible in `config.yaml`; both feature and final-selection snapshots use v2 schemas.

Run the independent pure-factor selector, consolidated-tape order-flow confirmation,
and unified research arbitration with:

```powershell
.\.venv\Scripts\python -m scripts.run_multisignal_shadow_pipeline `
  --trade-date 2026-07-28 --asof-utc 2026-07-28T14:20:00Z
```

This pipeline first collects the shadow-only Hyperliquid/Aevo cross-asset risk target,
then computes broad-universe premarket RVOL without consulting catalyst membership,
scores the eligible daily pool, downloads every SIP trade and NBBO quote for the
configured confirmation window, and ranks the union of catalyst and factor candidates.
The perpetual module records live top-of-book, public aggressor-side trades, funding,
price/OI confirmation, basis, explicit missing fields, cross-venue disagreement,
coverage, and immutable current/prior provenance; it does not yet modify a candidate
or market gate. Global liquidation windows remain unavailable until a dedicated
stream/node provider is configured. Tick Rule order imbalance, buy/sell pressure, VPOC,
quote-size imbalance, microprice, and spread are preserved with point-in-time lineage.
Order flow can confirm or reduce a candidate's score but cannot create a candidate by
itself. See [cross-asset sentiment](docs/CROSS_ASSET_SENTIMENT.md).
Every new output is `production_eligible=false` and `execution_eligible=false`; the
pipeline has no Broker or OMS command. The normal `schedule.premarket` tick runs this
as a separately leased, restart-safe shadow job after primary selection. Completed
daily stages are reused, but the live cross-asset stage is always refreshed. A shadow
failure is logged and retried without invalidating the primary catalyst selection.

Build a point-in-time, explicitly non-actionable ORB-5 research snapshot with:

```powershell
.\.venv\Scripts\python -m scripts.build_orb5_signals --trade-date 2026-07-20
```

The snapshot uses the same explicit feed policy as RVOL, reports whether the session is
still in progress, and never drives an order. Continuous live decisions use
`scripts.run_paper_session` and the licensed SIP WebSocket. The July 20 audit predates
the realtime subscription and remains historical evidence only; see the
[selection accuracy audit](docs/SELECTION_ACCURACY_AUDIT_2026-07-20.md).

Backfill restart-safe monthly Massive catalyst history for out-of-sample research with:

```powershell
.\.venv\Scripts\python -m scripts.backfill_massive_news `
  --start 2024-07-17 --end 2026-07-18
```

The end date is exclusive. Historical news alone is not performance evidence; minute
bars, point-in-time daily pools, labels, costs, and chronological purged validation must
all be joined before any accuracy claim.

Score the locked catalyst evidence with DeepSeek V4-Pro in shadow mode with:

```powershell
.\.venv\Scripts\python -m scripts.score_catalysts_deepseek `
  --trade-date 2026-07-20 --limit 3
```

Set `DEEPSEEK_API_KEY` in `.env` first. Start with a small limit, inspect the accepted
snapshot, then expand to the full locked pool. Every output remains
`unapproved_shadow` and is barred from the deterministic kernel until chronological
out-of-sample calibration approves it.

## Automatic postmarket learning loop

After a session is fully available, replay ORB-5, build a frozen Trading Episode, and
build an immutable top-mover selection postmortem before running the read-only
Research/Critic pair with:

```powershell
.\.venv\Scripts\python -m schedule.postmarket --trade-date 2026-07-20
```

The idempotent job ledger is stored in `runs/jobs.sqlite3`. A retry reuses accepted
snapshots and a completed job cannot run twice. Missing minute bars are never filled;
affected trade outcomes remain explicitly censored. Net returns remain unavailable
until quote-spread data exists, so the review cannot mistake missing costs for zero.
Program diagnostics are always available. Research and Critic agents run automatically
only after deterministic evidence gates pass; their failure cannot bypass those gates.
The selection postmortem separates captured opportunities, intentional gate rejections,
detectable misses, after-cutoff catalysts, and incomplete evidence. It stores close
return and MFE/MAE path facts with provenance, keeps every row
`production_change_allowed=false`, and exposes anonymized records to PDCA through the
`intraday_selection_postmortems` allowlist. The repo-local
`intraday-selection-postmortem` skill may group repeated ticker-free patterns and draft
sandbox hypotheses; it cannot change a gate, submit an order, or approve production.

Install the complete local Windows observation loop with:

```powershell
.\scripts\install_local_observation_tasks.ps1
```

This registers three current-user tasks: a five-minute idempotent premarket tick, a
five-minute DST-safe Paper-session launcher, and a 30-minute postmarket review tick.
The Paper launcher verifies SIP and Paper access before opening the one licensed stream.
It still cannot submit an order while `BROKER_WRITE_ENABLED=false` and
`TRADING_KILL_SWITCH=true`. Runtime output is appended to
`runs/premarket_scheduler.*.log`, `runs/paper_scheduler.*.log`, and
`runs/postmarket_scheduler.*.log`.

The runner itself checks the XNYS close plus the configurable postmarket data-grace
window, so the Windows clock and daylight-saving changes do not define market time.
Agents can only create research
proposals. At least 20 distinct session Episodes and 20 uncensored trade labels are
required before a proposal can enter deterministic sandbox evaluation; production
approval is always false in the agent output. Linux systemd and container deployment
are documented in [production deployment](docs/PRODUCTION_DEPLOYMENT.md).

Product maturity is a coded gate rather than a label. Evaluate the current evidence
for Paper eligibility with:

```powershell
.\.venv\Scripts\python -m scripts.refresh_maturity_evidence
.\.venv\Scripts\python -m scripts.check_product_readiness `
  --evidence runs\maturity-evidence.json --target paper
```

The command exits nonzero until every objective metric and external attestation is
present. Even `live_eligible` never arms the broker; live approval remains a separate
owner action.

## Keyless cloud market data and Paper execution

The AI investment process owns no Alpaca credential. It verifies scoped cloud market
and Paper API access without calling any order endpoint:

```powershell
.\.venv\Scripts\python -m scripts.verify_alpaca_access --symbols AAPL
```

The safe output must report an active, unblocked Paper account, authenticated cloud
market events, and `orders_submitted: 0`. Alpaca WebSocket ownership lives only in the
separate cloud-strategy-platform repository. This local collector leases its symbols,
waits for usable market health, then consumes the scoped SSE event API:

```powershell
.\.venv\Scripts\python -m scripts.stream_alpaca_sip `
  --symbols AAPL,MSFT `
  --state-db runs\sip-stream.sqlite3 `
  --lock-file runs\alpaca-sip.lock
```

It persists every received minute bar and latest NBBO quote for each symbol-second in a
WAL/FULL SQLite ledger. API failure yields no event and therefore no decision; missing
market data is never filled.

Historical cloud bars and quotes must carry a valid per-symbol `coverage` contract.
When the cloud reports regular-session gaps, an empty upstream response, stale realtime
events, or `fallback_recommended=true`, the AI process stops that path. It does not
silently switch to Yahoo/community data or reconnect to Alpaca with a hidden key.

Broker writes remain fail-closed through three independent controls:

1. the AI process has only a scoped cloud Paper token, never an Alpaca Key;
2. both cloud and AI environments default Broker writes to false;
3. `.env` defaults to `BROKER_WRITE_ENABLED=false` and `TRADING_KILL_SWITCH=true`;
4. the execution engine also requires coded Paper product readiness before it can call
   the order endpoint.

`TradePlan` is permanently buy-only, references immutable selection and live event IDs,
and requires a stop, take-profit, and UTC time stop. Bracket orders use deterministic
`client_order_id` values. The SQLite OMS records the complete plan, every state
transition, and the Broker order ID. Recovery reconciles by client ID and cannot create
a duplicate order.

The live decision uses `kernel.signals.orb5_intent`, which acts at the boundary after a
breakout bar completes and never consumes the future fill bar. Historical `orb5()`
retains next-bar VWAP only as an explicit backtest fill proxy. Runtime sizing uses the
smaller of configured capital and actual Broker equity, so the current $100k Paper
account is never sized as the configured $200k account or the displayed $400k buying
power.

Do not change the two write-control variables yet. The realtime data and Paper plumbing
are verified, but the coded maturity report remains `research_only` pending sufficient
point-in-time history, net cost-complete labels, purged OOS folds, and operational drills.

Once the current-date locked selection exists, the complete centralized shadow session
can be started with:

```powershell
.\.venv\Scripts\python -m scripts.run_paper_session `
  --trade-date 2026-07-21 `
  --max-seconds 60
```

This is the actual SIP -> causal ORB intent -> NBBO -> account-aware sizing -> TradePlan
-> P0/P1/P2 -> OMS path. Under the current evidence and default environment it records
plans and decisions but cannot submit an order. A future Paper order requires the
maturity report to reach `paper_eligible`, the broker write flag to be deliberately
enabled, and the kill switch to be deliberately disarmed at the same time.

## Production operations

The production runtime includes idempotent Beijing-time premarket phases, a
New-York-time Paper session, postmarket review, HTTPS failure alerts, weekly online
SQLite/immutable-snapshot backups, and immediate restore verification. Run the local
no-network-write safety drills with:

```powershell
.\.venv\Scripts\python -m scripts.run_local_safety_drills
```

The drill uses the real execution guardrail and backup implementations. It proves that
an active kill switch makes zero Broker calls and that a representative accepted
snapshot plus every current SQLite ledger can be restored and hash/integrity checked.
The receipt is stored under `runs/drills`, and only verified fields are added to
`runs/maturity-evidence.json`.

Paper sessions are audited in the OMS database. Bounded smoke tests and failed sessions
do not count toward the 60-session Live gate. Full sessions record the event count,
submitted order count, and startup reconciliation rate. Time stops are polled every five
seconds even during continuous SIP traffic; premature stream termination fails closed.

The program never fabricates the remaining attestations. A real alert delivery receipt,
historical-data usage confirmation, credential rotation, risk limits, compliance review,
and Live Broker permission still require external evidence. Keep Broker writes disabled
and the kill switch active until the Paper gate is complete.

After a human-owned item is genuinely complete, record its durable reference (contract,
receipt, or signed document identifier) with `scripts.record_attestation`; use `--revoke`
if the evidence expires. The command cannot alter sample counts or approve Live trading.

Automatic evolution is implemented as a governed research loop, not self-editing code.
Every historical gate survivor is cost-labeled so allowlisted challengers are not biased
by the baseline top-eight portfolio. The RVOL sandbox evaluates three frozen challengers
across four purged discovery folds, requires minimum coverage/retention/material uplift,
and confirms the winner once on an untouched fifth holdout. It automatically records a
new research champion or retains the baseline. The decision always has
`production_eligible=false`: agents can propose hypotheses, but neither an agent nor the
sandbox can silently modify the execution kernel or authorize capital.
