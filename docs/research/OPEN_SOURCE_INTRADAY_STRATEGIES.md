# 开源美股日内 ORB 策略核验

核验日期：2026-08-18。仅使用原论文、官方开源仓库及 QuantConnect/Lean 官方页面。本笔记只界定可复现规则和证据边界，不构成实盘建议。

## 结论

- 最值得原样复现的美股策略是 Zarattini、Barbon、Aziz 的 **Stocks-in-Play 5 分钟 ORB**。论文给出完整选股、方向、入场、止损、退出和仓位规则，并使用无幸存者偏差的约 7,000 只美股样本测试 2016—2023 年。
- 论文原版 `ORB + Relative Volume` 公开样本命中率为 **48.4%**，不是超过 50%；优势来自较大的趋势盈利、组合构造和风险控制，不能把高收益或 Sharpe 误写成高胜率。
- `asdtroll3/ORB-Backtester` 是规则清晰的 30 分钟 ORB 开源回测器，但原作者以 NQ 期货 5 分钟数据开发。它可作为执行逻辑基准，不能直接视为已经验证过的美股个股策略。
- 两套策略的入场与退出定义不同，不能混成一个“原版”：30 分钟仓库是收盘确认、ORL 止损、固定 R 目标；论文是 5 分钟方向过滤、触价 stop order、0.1×ATR(14) 止损、收盘退出且无固定止盈。

## 1. 30 分钟 ORB 开源回测器

来源仓库：<https://github.com/asdtroll3/ORB-Backtester>  
规则实现：<https://github.com/asdtroll3/ORB-Backtester/blob/main/Backtest.py>  
本次核验 HEAD：`f72381144d6ec62d039f9a4d3dfc39999683194f`

### 主策略规则

- 数据周期：5 分钟。
- 开盘区间：纽约时间 09:30—10:00，ORH 为区间最高价，ORL 为区间最低价。
- 方向：只做多。
- 入场：10:00 后第一根**收盘价严格高于 ORH** 的 5 分钟 K 线；按该 K 线收盘价入场。
- 止损：ORL。
- 风险单位：`R = 入场价 - ORL`。
- 默认止盈：`入场价 + 1.0R`。
- 每日最多一笔：只取第一次突破。
- 同一根 K 线同时触及止损与止盈时，代码默认先算止损，属于保守成交假设。
- 成本与仓位均可配置；仓库默认示例按 NQ 合约设置，不是美股手续费或滑点模型。

### 原版默认值与公开敏感性测试

| 类别 | 参数 |
|---|---|
| 主回测默认 | `OR_MINUTES=30`、`TARGET_R_MULTIPLE=1.0`、`TRADE_EXIT_TIME=15:00`、`STOP_BEFORE_TARGET=True` |
| 仓库公开敏感性网格 | 开盘区间 `[15, 30, 45, 60]` 分钟；目标 `[0.5, 1.0, 1.5, 2.0, 3.0]R` |

注意：README 的策略表写硬退出 **14:00**，但同一 README 配置段及 `Backtest.py` 当前默认值写 **15:00**。复现时应固定 commit，并显式选择退出时间；不能把两者都称为唯一原版默认值。

### 适用边界

仓库说明其开发和测试对象为 E-mini Nasdaq-100（NQ）期货，虽称可通过修改合约参数适配其他品种，但没有提供美股个股样本结果或横截面选股规则。因此，它适合验证 ORB 撮合和风控逻辑，不足以单独证明美股个股有效性。

## 2. Stocks-in-Play 5 分钟 ORB 论文

原论文 PDF（University of St. Gallen/SFI）：<https://www.alexandria.unisg.ch/server/api/core/bitstreams/3c2989c4-688d-4d78-8a71-f02690990d51/content>  
SSRN/DOI：<https://doi.org/10.2139/ssrn.4729284>

### 样本与基础筛选

- 样本期：2016-01-01 至 2023-12-31。
- 股票池：美国交易所约 7,000 只股票，包含退市股票，避免只保留存续公司的幸存者偏差。
- 开盘价必须高于 5 美元。
- 前 14 个交易日日均成交量至少 1,000,000 股。
- 前 14 个交易日 ATR 必须高于 0.50 美元。

### Stocks-in-Play 筛选

- 当日首个 5 分钟成交量除以前 14 个交易日首个 5 分钟成交量均值，得到 Relative Volume。
- Relative Volume 至少为 100%（即不低于 1.0）。
- 每日只交易 Relative Volume 最高的 20 只。

### 方向、入场、止损、退出

- 首根 5 分钟 K 线收盘高于开盘：只允许做多，在该 K 线最高价放 buy stop。
- 首根 5 分钟 K 线收盘低于开盘：只允许做空，在该 K 线最低价放 sell stop。
- 首根 K 线为 doji（开盘价等于收盘价）：不下单。
- 订单触发后，止损距成交入场价为 `0.1 × ATR(14)`。
- 没有固定止盈；未触发止损的持仓在 16:00 ET 收盘退出。
- 仓位按止损触发时损失所分配资本的 1% 反算，并设 4 倍最大杠杆。
- 回测计入每股 0.0035 美元佣金；论文没有建立现代美股逐笔滑点、点差或市场冲击模型。

### 公开样本结果

论文 `ORB + Relative Volume` 组合：总收益 1,637%，年化收益 41.6%，波动率 14.8%，Sharpe 2.81，命中率 **48.4%**，最大回撤 12%，年化 alpha 35.8%，beta 约 0。以上为论文历史回测，不等于样本外或当前市场可复制结果。

基础 ORB 未加 Relative Volume 时，命中率 41.4%、Sharpe 0.48；论文的核心增益来自 Stocks-in-Play 过滤，不是单独的突破规则。

### 原版参数与论文公开扩展

| 类别 | 参数 |
|---|---|
| 论文主版本 | 5 分钟 ORB；价格 >5 美元；14 日均量 ≥100 万股；ATR(14) >0.50 美元；首 5 分钟 Relative Volume ≥1；每日前 20；0.1×ATR 止损；收盘退出 |
| 论文公开时间框架比较 | 15、30、60 分钟 ORB；各自用同长度开盘区间的 Relative Volume，并与前 14 日同时间段均值比较 |
| 论文时间框架结果 | 5/15/30/60 分钟命中率分别为 48.4%/44.7%/42.4%/42.3%；Sharpe 分别为 2.81/1.43/0.21/0.40 |

15、30、60 分钟属于论文公开的时间框架扩展，不是 5 分钟主版本参数。论文没有公开把固定止盈 R 倍数、VWAP、均线、回踩确认或 2% 固定止损纳入此主策略。

## 3. QuantConnect/Lean 官方复现

QuantConnect 官方研究页：<https://www.quantconnect.com/forum/discussion/18444>  
Lean 官方开源仓库：<https://github.com/QuantConnect/Lean>  
本次核验 Lean HEAD：`1cf2fb6134596b892c2639d12105cd5d31fa7d40`

QuantConnect 复现保留了 5 分钟开盘区间、首根 K 线方向、Relative Volume >1、ATR >0.50 美元、前 20、0.1×ATR 止损和收盘退出。主要差异是为运行效率先取美元成交额最高的 1,000 只，而论文约为 7,000 只；官方页面明确称这是唯一不匹配论文的主要默认参数。

QuantConnect 2016 年复现结果为 Sharpe 2.396、beta -0.042。公开敏感性测试包括：

- 开盘区间 5—25 分钟，每 5 分钟一步；
- 初始流动性股票池 500—1,500 只，每 250 只一步；
- ATR 门槛 0—2 美元，每 0.25 美元一步；
- 另测 ATR 高于股价 1% 的无量纲门槛。

这些是 QuantConnect 的公开敏感性测试，不是论文原版参数。页面还记录分钟级回测与实时/秒级执行在止损单提交时点上可能不同；正式复现必须单独验证成交顺序和滑点。

## 4. 本地回测应保持的证据边界

1. 先逐字复现论文 5 分钟版本，不能用盘前 RVOL 代替“当日首 5 分钟量 / 前 14 日首 5 分钟均量”。
2. 原版双向策略与当前只做多系统应分开报告；只做多结果不能冒充论文组合结果。
3. `2%` 固定止损、VWAP、均线、回踩确认、10:00 后入场均属于本地改版，必须另命名、另做样本外验证。
4. 30 分钟仓库基准应先复现当前 commit 的代码默认值，再单独运行其公开敏感性网格；不得从网格中只挑最好结果。
5. 验收至少同时报告交易数、命中率、平均盈亏比、期望值、最大回撤、成本后结果、时间切分样本外结果；论文的 48.4% 命中率不能被描述为“胜率超过 50%”。

## 5. 2026-08-18 增量核验：ORB 当前网格与高盈亏比候选

本节只记录相对上文的增量发现。核验对象均固定到当时公开 commit，避免之后仓库变更导致“原版规则”漂移。

### 5.1 `asdtroll3/ORB-Backtester` 完整 sensitivity grid

原始 URL：

- 仓库：<https://github.com/asdtroll3/ORB-Backtester>
- 本次核验 commit：<https://github.com/asdtroll3/ORB-Backtester/tree/f72381144d6ec62d039f9a4d3dfc39999683194f>
- 参数与回测实现：<https://github.com/asdtroll3/ORB-Backtester/blob/f72381144d6ec62d039f9a4d3dfc39999683194f/Backtest.py>

当前代码公开的完整网格只有 **20 组**：

| 维度 | 完整取值 |
|---|---|
| 开盘区间长度 | `15, 30, 45, 60` 分钟 |
| 止盈倍数 | `0.5, 1.0, 1.5, 2.0, 3.0R` |

`sensitivity()` 只遍历上述两维。`TRADE_EXIT_TIME` 虽可配置，但**不在 sensitivity grid 内**；全部 20 组共用同一退出时间、成本、仓位和同 K 线成交顺序。README 策略表写 14:00，但当前 `Backtest.py` 及 README 配置段的默认值都是 15:00；因此当前代码默认应按 15:00 复现。

#### 当前代码的实际成交行为

- 每天必须有完整开盘区间：从 09:30 开始，5 分钟 K 数必须等于 `OR_MINUTES // 5`；不完整日直接跳过。
- 只做多；区间结束后，第一根 `close > ORH` 的 5 分钟 K 线为唯一入场信号，理论入场价为该 K 线收盘价。
- 止损固定为 ORL，`R = 入场价 - ORL`，目标为 `入场价 + target_r × R`。每个交易日最多一笔。
- 入场买入价加一个滑点。止损和时间退出减一个滑点；目标单按限价单处理，不扣退出滑点。跳空越过止损时按 `open - slippage`，跳空越过目标时按当根 `open` 成交。
- 入场后某根 K 线同时覆盖止损和止盈时，默认 `STOP_BEFORE_TARGET=True`，先计止损。
- 时间退出使用 `mins < TRADE_EXIT_TIME` 的最后一根 K 线收盘价。默认 15:00 时，实际用 14:55—15:00 K 线收盘价。
- 默认开启按风险距离反算仓位，代码保留小数合约，没有取整；作者也明确要求实盘前改为整数合约。
- 时区转换使用手工配置的固定 UTC offset，不是 IANA 时区规则；跨美国夏令时切换的数据必须自行分段或先转换为纽约时间。

#### 本仓库现有 1 分钟 RTH 数据能否复现

**能复现规则，不能复现作者的 NQ 结果。** 现有 `alpaca.sip.orb5_1m` 数据含 OHLCV，可无损聚合成纽约时间 5 分钟 K 线，再按上述逻辑运行。但原仓库预期单品种 NQ CSV，本地是美股候选股快照；必须另行处理多标的、股票手续费/滑点和同日多股风险。

### 5.2 新候选：SPY Noise-Area Intraday Momentum

原始 URL：

- 作者论文（Concretum Research，2025-09-22 版）：<https://concretumgroup.com/wp-content/uploads/2026/02/Beat-the-Market.pdf>
- SSRN：<https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4824172>
- 开源独立复现：<https://github.com/giovannibrusco/zarattini-2024-momentum-spy>
- 本次核验的复现 commit：<https://github.com/giovannibrusco/zarattini-2024-momentum-spy/tree/ec10608398b86c1a48d83411ae3e0fc9ab4cbfd1>
- 信号/成交实现：<https://github.com/giovannibrusco/zarattini-2024-momentum-spy/blob/ec10608398b86c1a48d83411ae3e0fc9ab4cbfd1/src/backtest.py>
- Noise Area 实现：<https://github.com/giovannibrusco/zarattini-2024-momentum-spy/blob/ec10608398b86c1a48d83411ae3e0fc9ab4cbfd1/src/noise_area.py>

#### 为什么值得做高盈亏比候选

论文的“当前边界 + VWAP 跟踪退出”版本报告 **Profit/Loss Ratio 1.8**、交易命中率 **37%**；独立开源复现在其 SPY/ES 样本报告胜率 **41%**、payoff **1.69**。它的设计目标正是用动态跟踪退出截断反转，同时不用固定止盈封住趋势上限。这些是历史回测结果，不是对当前样本外收益的保证。

#### 原始规则

- 品种是 SPY，使用 09:30—16:00 ET 的 1 分钟 OHLCV。
- 对每个时刻 `t`，计算过去 14 个交易日同时刻从开盘到 `t` 的绝对收益均值 `sigma_t`。
- 上边界为 `max(当日开盘, 前收) × (1 + sigma_t)`；下边界为 `min(当日开盘, 前收) × (1 - sigma_t)`，用前收处理隔夜跳空。
- 从 10:00 开始每 30 分钟检查一次。价格高于上边界做多，低于下边界做空，区间内不建仓。
- 最终版多头跟踪退出线为 `max(当前上边界, 当日 RTH VWAP)`；检查时价格低于该线则退出。空头规则镜像。
- 未触发跟踪退出的持仓在 16:00 强制平仓，不留隔夜仓。
- 按过去 14 日日收益波动率做 2% 日波动目标仓位，最高 4 倍杠杆。
- 论文成本假设为每股 0.0035 美元佣金和每股 0.001 美元滑点。

#### long-only 边界

原论文和开源复现的主策略都是多空双向，并允许反向信号时翻仓。对本系统可定义一个规则完整的 **long-only 切片**：只接受高于上边界的信号，跌破 `max(上边界, VWAP)` 后平仓，忽略做空和翻空。但这是本地适配，**一手论文和当前复现仓库都没有单独报告 long-only 的 1.8/1.69 盈亏比**；不能把双向结果直接当作做多子策略的结果。

#### 数据要求与本地可复现性

严格复现至少需要：连续 SPY 1 分钟 RTH OHLCV、前一日收盘价、至少 14 个交易日的预热期，以及足够长的样本用于时间切分验证。Volume 是 RTH VWAP 的必需字段。

对本地 `alpaca.sip.orb5_1m` 快照的只读盘点结果：96 个数据集，去重后 87,744 条 `symbol + timestamp`，194 个标的、93 个交易日，时间范围 2025-07-16 至 2026-08-14；**SPY 记录数为 0**。因此：

- 现有字段和 1 分钟粒度足以实现该策略逻辑；
- 现有本地数据**不能直接复现 SPY 原策略**，需先回填连续 SPY SIP 1 分钟 RTH 历史；
- 若改用候选个股，则已变成横截面个股版，还需要每只股连续 14 日预热数据和单独的点差/滑点模型，不能称为原论文复现。

### 5.3 QQQ 5-minute Opening Bias 的当前样本核验

来源仓库：<https://github.com/giovannibrusco/qqq-opening-bias-5min>  
固定 commit：`12f5fb912b9828d94cd1ffaa02c5082867f99321`

原规则根据 QQQ 09:30—09:35 首根五分钟 K 线确定方向，09:35 开盘入场，首根 K 线低点为多头止损，目标为 10R，否则收盘退出。公开复现明确指出现实执行成本会显著压缩优势，并提出 NQ 09:25 同向确认版本。

本地只做多核验使用 Alpaca SIP 2024-01-02 至 2026-08-17 的 255,540 根完整 RTH 分钟线及 09:25—09:29 盘前分钟线。执行模型包含每股 0.02 美元入场滑点、止损额外 0.04 美元滑点、每股双边 0.007 美元佣金和收盘退出 0.005 美元滑点；结构止损连同成本超过入场价 2% 的交易直接拒绝。

- 原始长多版：318 笔，胜率 25.16%，平均盈亏比 2.85，利润因子 0.96，净亏 9,908.69 美元，拒绝。
- 单独使用 QQQ 09:25 同向确认：168 笔，胜率 23.21%，平均盈亏比 3.19，利润因子 0.96，净亏 4,558.40 美元，拒绝。
- 探索性候选 `09:25 同向确认 + 前收 > SMA20 > SMA50`：84 笔，胜率 29.76%，平均盈亏比 2.88，利润因子 1.22，净赚 13,095.32 美元，最大回撤 8,344.72 美元。2024、2025、2026 三段净值点估计均为正。

该候选满足“盈亏比大于 1.2、利润因子大于 1、分年净收益为正”的点估计门槛，但 95% bootstrap 单笔期望区间为 `[-275.97, 650.24]` 美元，仍包含零；且它来自六种过滤器的同样本探索，2026 已被用于筛选，不再是盲测。因此它只能冻结后进入新交易日前向模拟，不能接入 Paper 自动下单或生产。

## 6. Manus 候选清单核验 - 2026-08-18

输入报告：`C:/Users/frank/Downloads/open_source_us_intraday_strategies_report.md`。以下判断只依据原仓库和 QuantConnect 官方策略页。

| 项目 | 核验结论 | 本系统决定 |
|---|---|---|
| [Alpaca Momentum-Trading-Example](https://github.com/alpacahq/Momentum-Trading-Example) | 规则透明，但固定在 commit `be4352838eebdd2d124eedd341e31fbae8774f3d`，最后提交于 2019-09-13；仅支持 Python 3.6/旧 Polygon 接口，没有完整回测，也没有 LICENSE 文件。源码默认价格仅 2—13 美元、前日美元量仅 50 万美元，默认找不到结构低点时止损为入场价的 95%，不符合当前市值≥10亿美元和全包止损≤2%。 | 只提取“4%强势 + 15分钟突破 + 双MACD + 成交量”信号做现代化适配回测，不复用旧执行代码，不继承任何收益结论。优先级第一。 |
| [QuantConnect Strategy Library](https://www.quantconnect.com/docs/v2/writing-algorithms/strategy-library) | 是研究教程集合，不是统一盈利策略。其 [Dual Thrust 官方示例](https://www.quantconnect.com/research/15258/dual-thrust-trading-algorithm/) 在 SPY 2004—2017 的 Sharpe 为 -0.17、回撤 41.1%；[Intraday ETF Momentum](https://www.quantconnect.com/research/15348/intraday-etf-momentum/) 的 2015—2020 示例 Sharpe 为 -0.628。 | LEAN 可作回测实现参考；这两个官方示例不进入优先回测队列。 |
| [Hummingbird-Project](https://github.com/JittoJoseph/Hummingbird-Project) | 仓库实现面向 NSE/Upstox，不能把其中引用的美股论文数字当成本代码的美股结果。 | 排除。 |
| [opening-range-breakout](https://github.com/melkerliljegren/opening-range-breakout) | AAPL 首5分钟、10R、日内清仓的教学 notebook；与已完成的 QQQ 10R 和 ORB 网格高度重叠，且没有可信成本与样本外证据。 | 不重复回测。 |
| [quant-trading-lab](https://github.com/MrNabeel/quant-trading-lab) | 核心信号、参数和精确时序未公开，All Rights Reserved。 | 仅参考工程，不属于可复现策略。 |
| [parabolic-reversal-trading-engine](https://github.com/BColladoT/parabolic-reversal-trading-engine) | 微盘股做空反转；作者明确说明 79.4% 胜率、3.89 PF 是同样本调参结果，OOS 尚未完成，且未建模真实借券。 | 与永久只做多、市值≥10亿美元约束冲突，排除。 |

结论：该清单只新增一个值得独立验证的信号家族——Alpaca 的开盘15分钟动量突破。复现必须重新定义股票池、市值门槛、2%全包止损、现代 Alpaca SIP 成交成本和时间切分，不能运行或移植原仓库的旧交易脚本。
