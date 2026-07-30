# 跨资产永续合约情绪模块

## 当前状态

本模块已经实现为确定性的影子因子，读取 Hyperliquid 与 Aevo 的公开行情，
生成不可变原始快照和目标级情绪快照。它没有 Broker、OMS 或下单接口，
所有输出都固定为：

- `production_eligible=false`
- `execution_eligible=false`
- `orders_submitted=0`

默认配置只使用 BTC、ETH 永续合约作为 24 小时全球风险偏好代理。股票、
商品或未上市资产的 HIP-3 合约不会自动发现或自动启用。

## 深模块与接口

外部数据在 `data_plane.providers.perpetual_sentiment` 处被归一为
`PerpObservation`。确定性内核只暴露一个主要接口：

```python
CrossAssetSentimentEngine.evaluate(
    observations=...,
    previous_observations=...,
    asof_utc=...,
)
```

调用方无需知道 Hyperliquid 与 Aevo 的返回结构。将来接入只读 IBKR
ES/NQ/CL 时，只新增适配器和配置映射，不修改评分内核。

## 原始证据

统一观测支持：

- Mark、Oracle、参考价格；
- Open Interest；
- Funding Rate；
- 24 小时名义成交额；
- Bid/Ask；
- 主动买卖不平衡；
- 多头和空头清算金额；
- 活跃状态、UTC 时间与来源证明。

供应商没有提供的字段必须保留为 `None`。Aevo 当前公开 REST 观测没有
Open Interest 和 24 小时成交额时，系统不会估算这两个字段。

## 确定性评分

初始影子权重为：

| 因子 | 权重 |
|---|---:|
| 价格趋势 | 20% |
| 价格 × OI 四象限 | 25% |
| 资金费率与拥挤 | 15% |
| 有方向的主动订单流 | 20% |
| 清算不平衡 | 10% |
| Mark/Oracle 基差 | 10% |

缺失因子不补值；可用权重低于配置阈值时，该来源返回
`insufficient_evidence`。成交额只参与流动性质量闸，不产生买卖方向。

四象限方向为新增多头、空头回补、新增空头和多头去杠杆。四象限分值还会
同时乘以价格变化和 OI 变化的连续幅度，防止几十秒内极小变化被直接映射为
满分多头或满分空头。

资金费率在温和区间顺势计分，进入极端拥挤区后逐步衰减，超过极端阈值后
转为反身性风险提示。

## 质量闸与跨平台聚合

以下情况会让单个来源直接降级：

- 数据来自未来或超过新鲜度上限；
- 合约不活跃；
- 配置要求成交额但实际缺失或低于门槛；
- Mark/Oracle 基差超过上限；
- Bid/Ask 点差超过上限。

目标分数使用流动性/配置权重和证据置信度计算加权中位数。平台之间的分歧
不会被平均掩盖，而是记录 `disagreement` 并降低最终置信度。

## 不可变快照

每次运行生成：

1. `raw.cross_asset.perp_observations`
2. `kernel.cross_asset.sentiment_shadow`

第二份快照以当前原始快照和上一份原始快照为父证据，保存每个平台的分项
评分、质量原因、聚合结果和完整来源证明。关键质量检查失败会进入隔离区。
单个平台网络故障只记录为警告并降低覆盖率，不影响主选股链路。

## 运行方式

单独运行：

```powershell
.\.venv\Scripts\python.exe -m scripts.build_cross_asset_sentiment_snapshot `
  --trade-date 2026-07-30
```

正常盘前影子流水线也会先运行该阶段：

```powershell
.\.venv\Scripts\python.exe -m scripts.run_multisignal_shadow_pipeline `
  --trade-date 2026-07-30
```

配置文件是 `config/cross_asset_sentiment.yaml`。将 `shadow_only` 改为
`false` 会在启动阶段被拒绝。

## HIP-3 与未来 IBKR

代码已经支持通过 `market` 显式指定 Hyperliquid HIP-3 DEX，但在增加
SpaceX、股票指数、半导体或石油映射前，必须人工确认：

- DEX 和合约的精确身份；
- 预言机定义与更新时间；
- 部署者及结算机制；
- 最低流动性、点差和 OI 上限；
- 与目标美股或 CME 合约的历史相关性及增量预测价值。

接入 IBKR ES/NQ/CL 后，CME 数据应作为权威跨资产行情；永续合约继续作为
24 小时辅助情绪源。

在完成逐日历史积累、purged walk-forward 验证和人工批准前，本模块不会
获得评分调整权、市场硬闸权或仓位调整权。
