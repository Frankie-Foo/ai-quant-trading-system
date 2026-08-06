# 供应商与证据契约

## 目录

1. 默认来源
2. 证据语义
3. 代表性行业目标
4. 爆仓事件协议
5. 新来源验收

## 默认来源

Hyperliquid 只调用公开 `https://api.hyperliquid.xyz/info`：

- `metaAndAssetCtxs`：Mark、Oracle、OI、Funding、24 小时名义成交额、退市状态；
- `l2Book`：最佳 Bid/Ask；
- `recentTrades`：最近 60 秒带方向成交。

HIP-3 合约的 API 标识为 `dex:coin`。配置中使用未加前缀的合约名，例如
`market=xyz, instrument=CL`，适配器生成 `xyz:CL`，并同时兼容 metadata 中已经
带前缀的名称。

Aevo 只调用公开 `https://api.aevo.xyz`：

- `/markets`：Mark、Index、活跃状态；
- `/funding`：资金费率；
- `/orderbook`：最佳 Bid/Ask；
- `/instrument/{instrument}/trade-history`：最近窗口的带方向成交。

官方接口：

- Hyperliquid Info：
  <https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint>
- Hyperliquid HIP-3：
  <https://hyperliquid.gitbook.io/hyperliquid-docs/hyperliquid-improvement-proposals-hips/hip-3-builder-deployed-perpetuals>
- Aevo Orderbook：
  <https://api-docs.aevo.xyz/reference/getorderbook>
- Aevo Trade History：
  <https://api-docs.aevo.xyz/reference/getinstrumentinstrumentnametradehistory>

## 证据语义

`side=B/buy` 表示主动买入，`side=A/sell` 表示主动卖出。主动流使用名义金额：

`(主动买入金额 - 主动卖出金额) / 总主动成交金额`

默认窗口 60 秒且至少需要 3 笔去重成交。成交 1–2 笔时保留
`aggressor_trade_count`，但 `aggressor_imbalance` 必须为 `null`。

Bid/Ask 缺失、盘口交叉或超过绑定的最大点差时，该来源不能评分。未配置成交额
门槛的来源不虚构 24 小时成交额。

价格趋势和价格/OI 只比较同一来源、严格更早且不超过 180 秒的前序观察。冷启动
不使用供应商上一日价格替代。

## 代表性行业目标

默认只纳入：

- `xyz:CL` → `energy-risk`；
- `xyz:SMH` → `semiconductor-risk`。

两者只影响对应行业，不影响无关股票。单来源行业目标可以降低仓位，但不能触发
`1.2x`。

`xyz:VIX` 和 `xyz:VOL` 在 2026-07-30 核验时均已退市，Aevo 也没有可用对应
市场。因此 `volatility-risk` 默认禁用并返回 `no_active_vix_source`。接入新的
VIX 数据源后应使用反向极性：VIX 上升对应股票风险偏好下降。

## 爆仓事件协议

公开的一次性 Hyperliquid/Aevo 接口不能提供完整全市场爆仓窗口，不得用大单、
急跌或普通成交伪造。通用事件使用
`perp_risk_liquidation_event.v1`：

```json
{
  "schema_version": "perp_risk_liquidation_event.v1",
  "event_id": "provider-unique-id",
  "venue": "hyperliquid",
  "market": "main",
  "instrument": "BTC",
  "liquidated_side": "long",
  "notional_usd": 125000.0,
  "observed_at_utc": "2026-07-30T14:00:00Z",
  "provenance": "provider:event-id"
}
```

配置 `liquidation.provider=jsonl` 时逐行读取该 JSON。配置 `http` 时以 GET 请求
`start_utc`、`end_utc`，响应必须是事件数组或 `{"events": [...]}`。成功返回的
完整窗口没有事件时，金额可记为零；未配置或请求失败时必须保持 `null`。

## 新来源验收

新增合约或来源前必须核验：

1. 标的身份、预言机定义、部署者和活跃状态；
2. 24 小时成交额、持仓量和 Bid/Ask；
3. API 时间、成交方向和去重 ID 的语义；
4. 至少一次离线契约测试和一次显式 `smoke-live`；
5. 缺失/限流/退市场景保持 fail-closed；
6. 配置映射只影响明确目标，禁止自动扫描全部合约。
