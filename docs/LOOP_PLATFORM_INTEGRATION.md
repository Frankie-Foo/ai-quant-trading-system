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

先复制并填写 `config/loop_quant_binding.example.json`。合同 ID、FSM 事件和 Golden
结果必须对应 Loop 中已经激活的版本：

```bash
python -m scripts.sync_loop_daily_review \
  --trade-date 2026-09-01 \
  --binding /secure/config/loop-quant-binding.json \
  --active-policy runs/strategy/active.json
```

环境变量：

```text
LOOP_BASE_URL=https://loop.example/api/v1/loop
LOOP_RUNTIME_API_KEY=<secret reference>
AI_QUANT_LOOP_SYNC_ENABLED=true
AI_QUANT_LOOP_BINDING_FILE=/secure/config/loop-quant-binding.json
AI_QUANT_ACTIVE_POLICY_FILE=/absolute/path/runs/strategy/active.json
```

本地 Outbox 默认为 `runs/loop-integration.sqlite3`。来源身份固定为交易日、策略和 active
policy hash；相同身份不同内容会失败关闭。Loop 暂时不可用时，本地复盘仍成功，事件保留
为 failed/pending 供同一命令安全重试。

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

- Loop 当前冻结合同为 `workflow-version-quant-daily-review-v5`；
- Task constraints 是 synthetic/真实来源的权威值；
- Golden Replay 或任一步失败时，Loop 的 `QuantRunBuffer` 丢弃全部领域暂存写入；
- Loop 同步事件始终记录 `orders_submitted=0`；
- 关闭 `AI_QUANT_LOOP_SYNC_ENABLED` 不改变原有日终和交易行为。
