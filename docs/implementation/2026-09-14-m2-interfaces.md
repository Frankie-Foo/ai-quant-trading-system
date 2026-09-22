# M2 特征、区间排名与响应标签

状态：实现初稿已汇入开发树，未经运行验证。测试和接口联调等待用户确认。

## 特征入口

`kernel.event_features.build_event_features(bars, *, symbol, session, decision_at, source, feed, price_basis, interval, sector_symbol=None, index_symbol=None, sector_mapping_available_at=None, include_ema=False)` 返回 `FeatureSnapshot`。

`ResearchSession(session_id, open_utc, close_utc)` 接收调用方提供的正式交易日历边界；模块本身不查询日历。`interval` 只接受 1 或 5 分钟。

行情表必需字段：`symbol/session_id/source/feed/price_basis`，`bar_start_utc/bar_end_utc/available_at`，`open/high/low/close/volume/vwap`。时间字段为带时区的 Polars Datetime。完成时间和实际可用时间均不晚于决策时刻的 K 线才进入特征；重复 K 键要求上游先按当时可见版本解决。

输出包含：H15/H30 的 high/low、全时段 VWAP、固定 15 分钟回看 VWAP 斜率、累计量和成交额、最近两个完成 K 的高低点抬高及量比、个股相对板块和板块相对指数的同窗口收益差。EMA20/50 及斜率为可选辅助。每个特征附可用状态、缺失原因、时间和输入哈希。

快照的 dataclass 固定属性，但内部 dict 仍可变；输入哈希是复现元数据，不是数字签名或防篡改认证。进入正式决策存储前仍需冻结、序列化及一致性校验。

## 排名与标签入口

- `build_top10_cohorts(trades, *, discovery_symbols, tradable_symbols, discovery_cohort_id, tradable_cohort_id, session, asof, source, feed, price_basis, validity_policy_id)`：返回发现池与可交易池两个 `Top10Cohort`。后者必须是前者子集，成员由调用方给出，本函数不认证全市场覆盖或历史市值门槛。
- `event_response(trades, *, event_id, symbol, recognized_at, session, asof, source, feed, price_basis, validity_policy_id, horizons=(15,30,60))`：计算价格响应毛收益。起点是有证据的识别时刻，不能用早期发布时间代替；不生成实际成交。
- `decision_response(quotes, *, event_id, decision_id, symbol, decision_at, session, asof, source, feed, price_basis, validity_policy_id, shares, costs, horizons=(15,30,60), max_entry_wait=1分钟, max_quote_age=30秒)`：寻找决策后可见的可执行 ask，再取固定期限 bid，输出假设回放成本。报价尺寸以股为单位，必须覆盖请求股数；此研究默认报价年龄不代表生产 2 秒门槛被修改。
- `quote_replay_costs(*, entry_ask, exit_bid, shares, costs)`：独立计算买卖各一单的研究成本。买卖价已体现 spread，不再额外扣 spread。

成交输入使用共同身份字段及 `trade_ts/available_at/trade_id/price/is_valid`；报价输入使用共同身份字段及 `ts_utc/available_at/bid_price/ask_price/bid_size/ask_size/is_valid`。`is_valid` 与 `validity_policy_id` 必须来自明确的成交条件/报价条件处理规则，不能批量填 true 冒充核验。

P10/P15 只取边界前 60 秒内且边界时已经可用的有效成交。短市没有完整 10—15 点窗口时不改用另一窗口。研究响应起点限定 10:00 至研究结束前；半日市研究结束为实际收盘与 15:00 较早者。超出窗口的完整期限标删失，观察时尚未到期标未成熟；缺行情保留空值。MFE/MAE 当前未计算，元数据明确缺少路径覆盖契约。

`CostAssumptions` 默认每股每边 0.0035 美元、每单每边最低 0.35 美元、每边附加滑点 10bp，支持更保守参数。状态固定 research/unapproved/cost_complete=False，不接受该入口自动批准费用。实际券商成交入口尚未实现，仍使用原系统真实成交证据链。

## 现有 API 字段映射

| 现有返回 | 新接口映射与限制 |
| --- | --- |
| bars 的 `ts_utc` | 明确声明为开始时刻后调用 `adapt_start_stamped_bars(bars, interval=...)`；不能猜供应商时间语义 |
| bars 的 `adjustment` | 按来源文件明确映射为 `price_basis`，不跨复权口径混用 |
| quotes 的 `ts_utc` | 保留为报价时刻；`bid_size/ask_size` 先核实 API 单位再按股输入 |
| trades 的 `ts_utc/trade_id` | 映射为 `trade_ts` 并保留唯一成交身份，适配时显式规范类型 |
| 历史数据没有 `available_at` | 不能直接赋值为市场时间；需实际采集证据或显式受限研究数据集，当前严格接口不会替用户补造 |
| 所有表的 `session_id` | 来自明确的正式日历会话；不能仅凭工作日推算 |

## 用户确认后的测试

固定手算样本验证完整 K、15 分钟 VWAP 回看、EMA 暖机和同窗口相对收益；加入晚到数据验证旧时点结果不变。验证 09:59/10:00/14:40/15:00、半日市、纳秒边界、重复或冲突行情、尺寸不足和报价失效；核对佣金最低值与不重复扣 spread。最后运行已有 quote_costs 及相关行情模块回归。当前没有执行这些步骤。
