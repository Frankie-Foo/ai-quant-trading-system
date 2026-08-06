# 日内量化交易系统 v2

这是一个面向美股、只做多、可审计的日内研究系统。仓库包含确定性选股、盘中监控、
证据答疑、收盘复盘，以及与研究面隔离的人工 IBKR 实盘执行台。当前版本仍处于工程
验证阶段，不代表策略已经成熟，也不对选股准确率或收益作保证。

## 本地验证

```powershell
.\.venv\Scripts\python -m pytest -v
.\.venv\Scripts\ruff check .
.\.venv\Scripts\mypy data_plane kernel research execution schedule agent_gateway `
  scripts tests
```

敏感凭据只能放在被忽略的 `.env` 或客户端系统安全存储中，不能写入报告、安装包、
源码或 Git。

冻结规范的补充说明见[架构决策](docs/ARCHITECTURE.md)，其中记录了数据、模型、策略、
交易与应用层在本仓库中的映射。

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

## 跨平台本地研究客户端

Windows 与 macOS 发行版使用同一套 Electron 界面和本地 Python sidecar。客户端在
用户电脑上完成数据增量同步、今日选股、盘中监控、证据答疑、三个研究 Agent 和收盘
复盘；accepted 快照、任务账本、复盘结果与执行账本都留在本机。内置 bootstrap 只含
研究数据和哈希清单，不含 API Key。

首次启动需要配置用户自己的 OpenRouter Key、Massive Key、Alpaca SIP 代理
Key/Secret 和 SEC 联系信息。模型角色可分别选择。行情、新闻或财务证据缺失时，系统
会明确阻断对应研究流程，不会用旧名单或测试数据冒充今日结果。凭据由 Electron
`safeStorage` 调用 macOS Keychain 或 Windows 系统安全存储加密，渲染页面只接收
脱敏状态。

客户端还提供一个与研究面隔离的“盈透手动执行台”。它只连接已经由用户登录的 TWS
或 IB Gateway，端口固定为 IBKR 实盘端口 `4001`。研究管线、选股脚本、自动复盘和
Agent 永远不能调用该执行入口；只有用户在执行页手工输入订单并逐级确认，才可能发送
实盘委托。研究运行状态中的 `orders_authorized=false` 仍然成立，它描述的是研究面，
不是对人工执行台的授权。

人工执行台当前具备以下约束：

- 每次启动默认关闭，并且写权限默认未解锁；
- 首次连接只允许单一可见 IBKR 账户，确认后加密绑定，后续连接严格核对；多账户环境
  在尚未指定并绑定唯一账户时会失败关闭；
- 只允许 `OpenLong` 和 `ReduceLong`，固定为 `STK / SMART / USD / DAY / LMT`；
- 预览阶段先调用 IBKR What-If，再要求输入包含账户脱敏值、方向、数量、价格和随机码
  的动态确认文字；
- 设有单笔名义金额上限、短时预览、临时写权限、持仓与未完成卖单核对；
- 本机 SQLite 执行账本使用客户端订单号和 `orderRef` 做幂等；提交结果不确定时进入
  `recovery_required`，核对完成前禁止新单。

必须注意：关闭实盘开关、退出客户端或清除账户绑定只会禁止新委托并断开 API，**不会
撤销已经到达 IBKR 的订单**。当前客户端没有撤单功能；已有挂单必须在 TWS / IB
Gateway 中核对和撤销。

当前人工执行台不支持模拟盘、卖空、市价单、自动下单、括号单、止盈止损联动或客户端
撤单，也不能替代 TWS 的风控和成交回报。用户名和密码始终只输入 TWS / IB Gateway，
不会进入本应用。

开发模式启动本地客户端：

```powershell
Set-Location client
npm ci
npm.cmd run desktop:analyst
```

完整使用与安全边界见 [macOS 本地研究客户端](docs/MACOS_RESEARCH_CLIENT.md) 和
[IBKR 人工实盘执行台](docs/IBKR_LIVE_EXECUTION.md)；跨平台打包步骤见
[客户端构建说明](docs/CROSS_PLATFORM_DESKTOP_BUILD.md)。原有自适应监控引擎的状态
契约与部署说明仍见[自适应桌面客户端](docs/ADAPTIVE_DESKTOP_CLIENT.md)，但它本身不能
越过人工执行台触发订单。

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
