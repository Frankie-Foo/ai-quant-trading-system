# 实时期货数据方案调研（2026-07-28）

## 结论

当前只有一名个人用户且已经持有 IBKR 账户时，性价比最高的方案是：

1. 通过 IBKR Client Portal 为当前用户名订阅 CME、NYMEX 实时 L1 行情；
2. 在 VPS 上运行只读的 TWS 或 IB Gateway；
3. 系统通过 TWS API 订阅 ES、NQ、CL，将其只用于跨资产市场状态判断；
4. Alpaca Paper 继续负责模拟执行，IBKR 适配器禁止下单。

IBKR 的数据授权绑定用户名，不能把一个用户名的行情订阅共享给其他人。将来增加个人用户时，应让每个人使用自己的 IBKR 用户名和行情订阅；团队统一转发则需要商业授权。

## 方案比较

| 方案 | 实时 API | 个人成本/条件 | 多人使用 | 结论 |
|---|---|---|---|---|
| IBKR TWS/IB Gateway | 支持；Python、Java、C++、C# 等 | 已开户用户可按交易所订阅；账户须满足入金和期货权限条件 | 每个用户名单独订阅，不能共享 | 当前首选 |
| Massive Futures Advanced | REST、WebSocket | $199/月，个人使用 | 个人版不可向其他用户分发 | 当前没有必要 |
| Databento Standard | 官方客户端和 Live API | $199/月；个人使用最多两台设备 | 商业/分发需要更高套餐和交易所授权 | 数据能力强，但当前性价比不如 IBKR |
| Tradovate API | REST、WebSocket | 需要 Live 账户、超过 $1,000 权益、API Access，并可能涉及 CME non-display 要求 | 用户与授权分别管理 | 接入条件更复杂，不推荐作为第一选择 |
| Barchart/dxFeed | 支持 REST/流式方案 | 主要为定制报价或合作伙伴方案 | 可做正式团队授权 | 适合后期商业化询价 |
| TradingView | 图表端可购买实时交易所数据 | 面向图表显示 | 不是可供服务端复用的行情 API | 不适合作为后端数据源 |
| Hyperliquid/Aevo | 公共实时 API | 通常低成本 | 可多人读取公开数据 | 不是 CME 期货，只能作为辅助情绪因子 |

## IBKR 官方约束

- TWS API 可通过 TWS 或 IB Gateway 获取实时数据；官方要求已开立并入金的 IBKR Pro 账户、相应市场数据订阅和交易权限。
- 默认每个用户名至少有 100 条并发市场数据线，足够订阅 ES、NQ、CL。
- TWS API 的股票、期货等 Level 1 行情更新频率约为 250ms，远高于本系统 15 秒级状态评估所需频率。
- IBKR 当前公开价格页列出非专业用户的 CME Real-Time L1 和 NYMEX Real-Time L1 套餐；具体可见套餐和最终价格受开户地区、专业身份与账户权限影响，应以 Client Portal 实际页面为准。
- 市场数据费按用户名收取，同一账户下不同用户名也不能共享订阅。

官方来源：

- [IBKR Market Data Pricing](https://www.interactivebrokers.com/en/pricing/market-data-pricing.php)
- [IBKR TWS API Documentation](https://ibkrcampus.com/campus/ibkr-api-page/twsapi-doc/)
- [IBKR Web API Market Data Requirements](https://ibkrcampus.com/campus/ibkr-api-page/webapi-doc/)
- [IBKR TWS API Level I Data](https://interactivebrokers.github.io/tws-api/top_data.html)

## 其他官方来源

- [Massive Futures Pricing](https://massive.com/pricing?product=futures)
- [Massive Market Data Terms](https://massive.com/legal/market-data-terms-of-service)
- [Databento Pricing](https://databento.com/pricing)
- [Databento Live Licensing Guide](https://databento.com/docs/api-reference-live/basics/metered-pricing)
- [Tradovate API](https://api.tradovate.com/)
- [Barchart OnDemand APIs](https://www.barchart.com/ondemand/api)
- [TradingView Datafeed Documentation](https://www.tradingview.com/charting-library-docs/latest/connecting_data/)

## 推荐接入边界

- IBKR 连接只授予行情读取权限，API 端关闭交易能力。
- IBKR 和 Alpaca 使用不同进程、不同凭据及不同审计日志。
- IBKR 行情断流、时间戳过期或合约换月异常时，跨资产因子标记为不可用；不得降级为免费网页行情。
- 首版只接连续主力映射后的 ES、NQ、CL，并记录实际合约、换月规则、来源时间戳和数据新鲜度。
- 跨资产因子先影子运行；通过历史回测和完整交易日验证后，才按既定规则获得评分调整权。
