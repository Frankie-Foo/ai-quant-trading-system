# Loop Platform 量化复盘闭环

## 边界

`ai-quant-trading-system` 是行情、特征、策略、风控和执行事实源；Loop Platform 是
PAPER-only 学习与策略治理控制面。Loop 产物始终要求
`production_eligible=false`、`allow_order_execution=false`，不能调用 Broker、修改
`active.json` 或绕过本地 P0/P1/P2 风控。

## 上行复盘

日终 `schedule.postmarket` 完成本地 accepted 快照和账本后，可以非阻塞调用
`scripts.sync_loop_daily_review`。适配器从日终机会复盘中保留独立的研究 Top10，不扩大
最多六只的实际执行池；没有物化的一分钟路径保留空数组和 unavailable 状态，不能伪造。

控制面初始化不会随日终任务隐式执行。首次接入或升级合同版本时，研发必须显式运行版本化
初始化器；同一清单重复运行只做一致性验证，不重复创建，已存在 ID 的内容发生变化则失败关闭：

```bash
python -m scripts.init_loop_control_plane \
  --manifest config/loop_control_plane/us_equity.v1.json \
  --binding-out runs/loop-control-plane/us_equity.binding.json
```

生成的 v2 binding 固定三份合同 ID 和各自配置 SHA-256。每日复盘创建远端 Task 前，会通过
`control-artifacts` 逐一验证 ID、类型、active 状态、`US-equity` 范围、`PAPER_ONLY`、
`allow_order_execution=false`、`production_eligible=false`、配置 Hash 和 `available_at`：

```bash
python -m scripts.sync_loop_daily_review \
  --trade-date 2026-09-01 \
  --binding runs/loop-control-plane/us_equity.binding.json \
  --active-policy runs/strategy/active.json
```

环境变量：

```text
LOOP_BASE_URL=https://aisales-v2.vertu.cn/api/v1/loop/machine
LOOP_RUNTIME_API_KEY=<secret reference>
AI_QUANT_LOOP_SYNC_ENABLED=true
AI_QUANT_LOOP_BINDING_FILE=/secure/config/loop-quant-binding.json
AI_QUANT_ACTIVE_POLICY_FILE=/absolute/path/runs/strategy/active.json
```

本地 Outbox 默认为 `runs/loop-integration.sqlite3`。来源身份固定为交易日、策略和 active
policy hash；相同身份不同内容会失败关闭。合同缺失或不一致时事件标记为
`blocked_precondition`，且不会创建远端 Task。复盘 `as_of` 早于任一合同 `available_at` 时仅标记
`audit_only_backfill`，不会进入 Loop 策略治理样本。Loop 暂时不可用时，本地复盘仍成功，事件
保留为 failed/pending 供同一命令安全重试。

HTTP 2xx 只表示接口调用成功，不表示复盘成功。只有返回的 `Run.status=COMPLETED` 才会把
Outbox 标记为 delivered；`FAILED` 会保留远端 Task ID、Run ID、失败节点和错误码供审计与重试。

## 延迟 Outcome

1d、5d、20d Outcome 在真正可用后单独回填，不放入当天 Task。输入必须包含精确
`decision_event_id`、`source_run_id`、`strategy_revision_id`、evaluation role 和通过的
point-in-time guard：

```bash
python -m scripts.sync_loop_outcomes --file /secure/outcomes/2026-09-01-1d.json
```

## 下行策略候选

交易系统只读取 Loop 的 `strategy_policy_candidate`。首阶段唯一允许参数是
`universe.min_rvol`，范围仍由本地 `kernel.strategy_policy` 校验。任何 trading policy、
订单权限、生产资格或额外参数都会拒绝整个候选：

```bash
python -m scripts.sync_loop_policy_candidates \
  --market-scope US-equity \
  --artifact-id quant_policy_xxx
```

成功只写 `runs/strategy/challenger.json`。之后继续使用现有同池 Shadow、明确 policy hash
确认、人工批准、Paper Canary 和回滚流程；本集成没有自动晋级 active 的入口。

## 运行验证

- Loop 当前冻结合同为 `workflow-version-quant-daily-review-v6`；v6 使用明确的收盘收益率
  聚合字段，不再把标的收益率求和命名为 PnL。
- `top10_close_return_sum` 与 `non_top10_close_return_sum` 的单位固定为小数收益率，
  聚合方式固定为标的收盘收益率等权求和；`top10_positive_close_return_rate` 的分母是
  有有效收盘收益率的 Top10 标的。它们都不是现金盈亏或组合收益。
- 只有存在成交、仓位、成本和归因证据时，才可另报独立的组合 PnL 字段，并通过
  `metric_semantics.portfolio_pnl_available=true` 指向该字段。
- Task constraints 是 synthetic/真实来源的权威值；
- Golden Replay 或任一步失败时，Loop 的 `QuantRunBuffer` 丢弃全部领域暂存写入；
- Loop 同步事件始终记录 `orders_submitted=0`；
- 关闭 `AI_QUANT_LOOP_SYNC_ENABLED` 不改变原有日终和交易行为。
