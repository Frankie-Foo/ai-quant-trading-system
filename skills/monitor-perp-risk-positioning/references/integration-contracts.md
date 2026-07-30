# 集成契约

## 目录

1. 输出协议
2. 仓位组合
3. Webhook
4. 结果回写
5. 配置晋级

## 输出协议

`snapshot`/`watch` 输出 `perp_risk_snapshot.v1`。必须先检查：

- `provider_status`
- `data_cutoff_utc`
- `actionable`
- 目标的 `coverage`、`regime`、`effective_multiplier`、`reasons`
- `execution_eligible=false`
- `orders_submitted=0`

`schema` 命令导出四份 JSON Schema。破坏性字段变更必须升级协议主版本。

## 仓位组合

主系统必须显式传入相关目标。例如：

- 普通股票：`global-risk`
- 能源股：`global-risk,energy-risk`
- 半导体股：`global-risk,semiconductor-risk`

组合规则：

1. 任一相关目标低于 `1.0x` 时取最低值；
2. 没有风险否决且任一相关目标确认 `1.2x` 时允许 `1.2x`；
3. 其他情况为 `1.0x`；
4. 倍率乘主策略原计划目标仓位，不乘账户净值，不生成订单。

`actionable=false` 时 `recommend` 返回 `research_only`，即使倍率仍可用于研究。

## Webhook

配置启用后发送通用 JSON：

```json
{
  "event_type": "perp_risk_positioning",
  "schema_version": "perp_risk_snapshot.v1",
  "body": "中文或英文摘要",
  "data": {}
}
```

只在目标方向、有效倍率或供应商健康发生变化时立即发送；相同状态只发送低频
心跳。密钥优先取 OS keyring，环境变量作为容器后备。不得在 URL、配置、日志
或返回值中包含密钥。

## 结果回写

方向准确性和交易结果必须分开。输入 `perp_risk_outcome.v1`：

```json
{
  "schema_version": "perp_risk_outcome.v1",
  "snapshot_id": "perp_...",
  "target_id": "global-risk",
  "kind": "benchmark",
  "observed_at_utc": "2026-07-30T14:30:00Z",
  "horizon_minutes": 30,
  "return_pct": -0.35,
  "metadata": {"benchmark": "QQQ"}
}
```

`benchmark` 衡量市场方向和覆盖层贡献；`trade` 衡量实际/模拟交易结果。交易亏损
不能自动归因于该 Skill，因为个股选择、入场和风控由主系统负责。

## 配置晋级

`propose-config` 至少需要 100 条 benchmark 结果，只搜索有限阈值网格并记录尝试
数。输出永远是 challenger，`production_eligible=false`。

`approve-config` 要求用户明确提供候选哈希。Agent 必须先展示差异、样本量和
局限，不能代表用户确认或自动覆盖正式 YAML。
