# 跨资产永续合约情绪模块代码审查

审查日期：2026-07-30
审查范围：`kernel.cross_asset_sentiment`、`data_plane.providers.perpetual_sentiment`、`scripts.build_cross_asset_sentiment_snapshot`、`scripts.run_multisignal_shadow_pipeline` 及相关测试。

## 总结

该模块实现的是“24 小时跨资产风险偏好影子因子”，不是单股预测器，也不是交易策略。它读取 Hyperliquid 和 Aevo 的公开永续合约状态，归一化为不可变观察，对单个合约评分，再把多平台 BTC/ETH 证据聚合为 `global-risk` 市场情绪。

当前代码明确禁止用于生产和执行：

- `production_eligible=false`
- `execution_eligible=false`
- `orders_submitted=0`

仓库中没有选股、风控、仓位或 Broker 模块读取 `kernel.cross_asset.sentiment_shadow` 作为下单依据。它目前只是独立影子证据。

## 具体实现

1. 公开数据适配
   Hyperliquid 读取 `metaAndAssetCtxs`、`l2Book` 和 `recentTrades`；Aevo 读取 `markets`、`funding`、`orderbook` 和公开 trade history。适配器不接受交易凭据，也没有写操作。

2. 端到端已采集字段
   Hyperliquid 支持 Mark、Oracle、OI、资金费率、24 小时名义成交额、Bid/Ask 和 60 秒主动买卖不平衡。Aevo 支持 Mark、Index/Oracle、资金费率、活跃状态、Bid/Ask 和 60 秒主动买卖不平衡。

3. 显式缺失字段
   Aevo 当前公开 REST 不提供 OI 和 24 小时名义成交额，代码不会估算。两家默认公共接口也没有完整、可审计的全市场爆仓窗口，因此清算金额保持 `None`，不会用大额成交或价格跳变冒充爆仓。

4. 质量闸
   内核拒绝未来时间、超过新鲜度上限、不活跃合约、低成交额、Mark/Oracle 基差超限和 Bid/Ask 点差超限的数据。缺失字段保持 `None`，不被零值或估算值替代。

5. 评分模型
   分项包括价格趋势、价格 x OI、资金费率、主动订单流、爆仓不平衡、Mark/Oracle 基差。可用分项权重低于 35% 时，来源返回 `insufficient_evidence`。成交额只作为流动性质量证据，不产生买卖方向。

6. 聚合逻辑
   目标分数使用加权中位数，平台分歧用 `disagreement` 降低置信度。`coverage` 独立持久化；覆盖率低于 35% 时，即使单一强信号存在，也不能发布 `risk_on` 或 `risk_off`。

7. 审计快照
   每次运行写入 `raw.cross_asset.perp_observations` 和 `kernel.cross_asset.sentiment_shadow`。目标快照保存分项评分、质量原因、平台状态、聚合分数、覆盖率、置信度、分歧度和父快照标识。

## Standards 审查

1. 历史 `asof` 不能调用实时端点伪造历史数据。已处理：历史模式在没有官方历史归档适配器前返回 `historical_unavailable`。
2. 上一观察必须严格早于当前观察且足够新鲜。已处理：增加 `max_previous_gap_seconds`，并校验 `previous.observed_at_utc < current.observed_at_utc`。
3. 关键原始质量检查必须先于评分内核。已处理：重复、未来时间和关键原始检查在评分前完成，失败数据进入隔离区。
4. 不可变模型不能只是浅冻结。已处理：嵌套评分和来源证明使用深度不可变映射。
5. 配置字段必须有消费者。已处理：`collection_interval_seconds` 用于主动流窗口，`history_snapshot_limit` 用于历史快照裁剪。

## Spec 审查

1. 历史运行和快照复用必须满足点时语义。已处理：实时阶段强制刷新；历史复用要求数据截止时间与请求 `asof` 精确一致。
2. 上一快照选择必须按观察时间，而不是文件落盘时间。已处理：按原始观察时间选择 prior raw snapshot。
3. Hyperliquid 活跃状态不能固定为真。已处理：适配器读取并保留合约活跃/退市状态，异常状态会降级。
4. 文档中的 Bid/Ask、主动流、爆仓三类证据需要区分端到端能力和预留能力。已处理：Bid/Ask 与主动流已端到端采集，爆仓保持显式不可用。
5. coverage 不能只隐式进入 confidence。已处理：`TargetSentimentAssessment` 和持久化快照都输出 coverage。

## 剩余限制

1. 还没有完整的爆仓数据源，1.2x 仓位提升必须 fail-closed。
2. 还没有真实 VIX 或期货数据源，波动率目标仍是预留能力。
3. 还没有历史回填、purged walk-forward 或净成本标签证明该分数对次日美股、盘前方向或单股收益有增量预测价值。
4. 当前模块只能提供市场风险偏好和仓位约束证据，不能独立创造候选股、交易计划或下单授权。

## 验证

- 专项测试：通过
- Ruff：通过
- strict mypy：通过
- 公共 API live smoke：Hyperliquid 与 Aevo 观测健康
- 执行资格：保持 false
