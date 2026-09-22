# 事件量化的数据接口接入

用户提供的配置位置：`D:/桌面搬家/AI-Investgo/Gary-market-data.env`。
本轮只核对变量名称；未调用远端接口，未验证订阅权限或连通性。

## 接入顺序

| 数据 | 复用位置 | 本轮处理 |
| --- | --- | --- |
| 新闻 | `data_plane.providers.catalyst_news.fetch_alpaca_news_direct` | 新增显式采集入口，将接口结果导入事件账本 |
| 分钟行情 | `data_plane.providers.alpaca.fetch_bars` | 特征模块使用明确字段映射，不另写供应商客户端 |
| 买卖报价 | `data_plane.providers.alpaca.fetch_quotes` | 回放成本使用 ask/bid 和真实报价时刻 |
| 逐笔成交 | `data_plane.providers.alpaca.fetch_trades` | 10:00/15:00 边界价格使用成交记录，不拿当前报价替代 |
| 当前市值 | 用户确认的总股本缓存 × `Alpaca SIP` 最新逐笔成交 | 仅在缓存和新鲜 SIP 报价都可用时生成；不再逐只调用外部市值接口 |
| 历史股本与市值 | 需保留生效时间和系统可用时间的接口证据 | 当前公司信息不能倒填历史，缺失时保留阻断原因 |

配置还包含 Finnhub 和 Alpha Vantage 的键名。这只说明配置项存在，不能据此认定服务权限、历史覆盖或速率额度已验证。已有 Alpaca 能覆盖的能力直接复用，其他接口在确有缺口时再接。

## 时间与凭据

沿用已有美股晚间采集窗口：北京时间 21:00 至次日 06:00（不含 06:00），覆盖跨午夜交易。代码读取显式指定配置，按时间检查，成对加载 key/secret；不把行情配置当成交易账户授权。文档、Git、异常和审计只保存来源及状态，不保存密钥。

现代漏斗第一波固定为美东 08:30。总股本由
`scripts.cache_sec_shares_outstanding` 从既有活跃普通股参考表的 CIK 分批缓存，默认写入
`<data-root>/cache/sec-shares-outstanding.parquet`，状态和退避写入
`<data-root>/state/sec-shares-cache.sqlite3`；可用 `AI_QUANT_SHARES_CACHE_FILE` 改为用户
自有路径。每次最多处理显式 `--max-ciks` 个 CIK，成功项 14 天后才刷新，下载失败 1 小时后
重试，缺失股本 7 天后重试。这个后台缓存不生成市值，也不改变策略。

市值快照脚本 `scripts.refresh_event_sip_market_caps` 只读取该缓存并乘以当下 SIP 成交价；
它不会调用 Massive 的逐只 `ticker_details`，也不会把旧市值冒充实时市值。缓存必须至少
包含 `symbol`、`shares_outstanding`、`available_at`、`source`、`provenance`。报价超过 15 秒、
非 SIP、未来时间或总股本未知时都不产出市值，第一波因市值覆盖不完整而安全阻断。

总股本缓存任务默认不自动启动；待用户指定后再以低速批次运行或注册后台任务。当前不
伪造缓存内容。

接口原始返回缺少历史系统首见时刻时，导入保留实际采集和录入时间；历史研究必须标明这一限制。未来完成前向采集后，才能积累系统自己的首见证据。

## 待确认的接口检查

在用户确认测试后、允许时窗内，只读检查新闻、bars、quotes、trades；保存响应时间、feed、最新数据时间和缺失原因。遇到无权限、限流或超时，分别记录；不自动改用未经授权的凭据，也不把空响应当作全市场没有机会。
