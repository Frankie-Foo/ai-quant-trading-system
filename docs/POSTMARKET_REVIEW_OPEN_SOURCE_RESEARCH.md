# 自动选股与交易复盘：开源项目和 Agent Skills 调研

> 调研日期：2026-07-28
> 资料范围：项目官方仓库、官方文档和论文；不以博客清单作为结论依据。

## 结论

系统需要补的不是一份“交易总结”，而是一条每天自动运行、可以审计的学习闭环：

1. 固化盘前事实：当时看到了哪些股票、因子、催化剂、闸门结果和版本。
2. 收盘生成标签：全市场强势股、候选股未来不同期限的收益、MFE/MAE、成交成本和回撤。
3. 复盘两类问题：
   - **选股复盘**：为什么选到、为什么漏选、如果放宽某个闸门是否能选到。
   - **操作复盘**：计划、实际成交、滑点、仓位、止损止盈和规则遵守情况。
4. 把复盘结论变成“待验证假设”，而不是直接修改生产参数。
5. 通过滚动或 purged OOS、成本后收益和样本量门槛后，才允许 challenger 申请晋级。

调研没有发现一个成熟项目能够直接完成“盘前候选快照 → 收盘强势股对账 → 漏选逐只归因 → 反事实验证 → 安全晋级”的完整美股闭环。最佳方案不是替换现有系统，而是：

- 借鉴 **Qlib** 的特征、标签、实验记录和滚动 OOS；
- 借鉴 **vectorbt** 的高速反事实、阈值扫描和因子消融；
- 借鉴 **LEAN** 的订单/fill 重建和实盘—回测对账；
- 参考 **Jesse** 的 feature/label gather、规则显著性和 Monte Carlo；
- 参考 `tradermonty/claude-trading-skills` 的交易记忆与 postmortem 输出契约；
- 继续由现有 Polars/Python 内核实现每日复盘，不整体引入另一套交易引擎。

## 适配度总表

符号说明：`强` 表示原生能力可直接复用；`中` 表示需要适配；`弱` 表示不是该项目目标；`无` 表示没有发现对应成品能力。

| 项目 | 程序化导入 | 逐笔/分钟回放 | 漏选复盘 | 特征/标签/OOS | Python 嵌入 | 维护与许可（截至调研日） | 对本系统的适配度 |
|---|---|---:|---:|---:|---:|---|---:|
| [Microsoft Qlib](https://github.com/microsoft/qlib) | 行情、特征、预测：强；券商成交：弱 | 1 分钟/高频：中；逐笔撮合：弱 | 无成品，易扩展 | **强** | **强** | 活跃；MIT | **5/5** |
| [vectorbt](https://github.com/polakowo/vectorbt) | 数组化订单：强 | 分钟：强；事件式逐笔：弱 | 无成品，反事实很强 | 强 | **强** | 活跃；Apache-2.0 + Commons Clause | **4.5/5** |
| [QuantConnect LEAN](https://github.com/QuantConnect/Lean) | LEAN 回测/实盘订单：强；任意券商 CSV：中 | **Tick/分钟：强** | 无成品，可扩展 | 中—强 | 支持 Python，但核心为 .NET | 高度活跃；Apache-2.0 | **4/5** |
| [TradeNote](https://github.com/Eleven-Trading/TradeNote) | 券商 CSV/execution：强 | 无行情回放引擎 | 无 | 弱 | JS/MongoDB，需服务集成 | 更新较慢；GPL-3.0 | **3/5（仅日志界面）** |
| [hftbacktest](https://github.com/nkaz001/hftbacktest) | 结构化 tick/L2/L3：强 | **逐笔/盘口：强** | 无 | 弱 | Python + Numba/Rust | 活跃；MIT | **3/5（未来 L2）** |
| [FinRL](https://github.com/AI4Finance-Foundation/FinRL) | 行情和状态：强；成交日志：弱 | 分钟：中；逐笔：弱 | 无 | train/valid/test：中—强 | 强 | 活跃；MIT | **2.5/5** |
| [Jesse](https://github.com/jesse-ai/jesse) | 1 分钟 candle：强；外部成交：弱 | 逐根 candle | 无 | gather/ML/Monte Carlo：中 | 强 | 高度活跃；MIT；部分 Premium | **2.5/5** |
| [backtrader](https://github.com/mementum/backtrader) | CSV/Pandas：强 | tick/bar/replay：强 | 无 | 原生标签/OOS：弱 | 强 | 维护弱；GPL-3.0 | **1.5/5** |
| [TradeNote 类 Agent skills](https://github.com/tradermonty/claude-trading-skills) | JSON/YAML/人工记录：中 | 弱 | 5/20 日 missed opportunity：中 | 规则反馈：中；严格 OOS：无 | 脚本可复用 | 活跃；MIT | **3.5/5（输出契约）** |
| [TradesViz](https://www.tradesviz.com/) | 券商同步/文件导入：强 | 历史模拟/图表：强 | 无公开可嵌入实现 | 黑盒 | 无开源 Python 内核 | 商业闭源 | **仅作产品参考** |

## 重点项目评估

### 1. Qlib：最适合做“复盘研究底座”

Qlib 的 `qrun` 可以把 dataset、训练、回测和评价组成自动工作流；`QlibRecorder` 能记录参数、指标、模型、预测和分析产物，并可接 MLflow。[官方快速开始](https://qlib.readthedocs.io/en/stable/introduction/quick.html)；[Recorder 文档](https://qlib.readthedocs.io/en/stable/component/recorder.html)

它支持自定义 DataLoader/DataHandler、特征和标签，Dataset 支持 train/valid/test 切片，RollingStrategy/rolling task 适合做时间前推验证。[Online Serving](https://qlib.readthedocs.io/en/latest/component/online.html)

适合吸收的部分：

- `ReviewRecord`：每次盘前选股的候选、闸门、因子、配置和代码哈希；
- `OutcomeLabel`：5/15/30/60 分钟与收盘的收益、MFE/MAE、成本后收益；
- `Experiment/Recorder`：每个“为什么漏选”的修改建议成为独立实验；
- rolling OOS：只用过去标签训练，用之后区间评价。

不建议现在把主系统迁到 Qlib。Qlib 不是券商成交日志工具，也没有现成的漏选强势股归因；应先复用其数据契约和实验思想，等研究规模明显增大后再考虑把离线模型层接入 Qlib。

### 2. vectorbt：最适合反事实和因子消融

vectorbt 可以把大量参数、资产和时间窗口压进 NumPy 数组并行计算，`Portfolio` 提供 orders、trades、positions、drawdown 和绩效分析，适合快速回答：

- 如果 RVOL 门槛从 3 降到 2.5，当天赢家能否进入？
- 去掉某个硬闸后，召回率提高多少，假阳性和最大回撤增加多少？
- 催化剂、纯因子和订单流分别贡献多少？
- 同一规则在不同市场状态和 walk-forward fold 是否稳定？

相关能力见[官方仓库和 README](https://github.com/polakowo/vectorbt)及[Portfolio API](https://vectorbt.dev/api/portfolio/base/)。

限制：

- 它是向量化研究工具，不是严格的逐事件市场回放引擎；
- 许可证是 **Apache-2.0 with Commons Clause**，禁止出售价值主要来自该软件的产品或服务。内部研究可以评估使用，但若系统对外商业化，必须先做许可证审查。[官方许可证](https://github.com/polakowo/vectorbt/blob/master/LICENSE.md)

因此，近期可先用现有 Polars/NumPy 实现同样的反事实矩阵；不要在生产核心中形成未经审查的强依赖。

### 3. LEAN：适合成交重建和实盘—OOS 对账

LEAN 是事件驱动的回测和实盘引擎，支持 Python/C#、动态 Universe、历史数据和 Tick/Second/Minute 级别事件。[官方仓库](https://github.com/QuantConnect/Lean)；[数据格式](https://www.quantconnect.com/docs/v2/lean-engine/data-format/key-concepts)

它的研究环境可以分析回测和实盘订单，并支持 live reconciliation：把实盘结果与同区间 OOS 回测进行对照。[Backtest Analysis](https://www.quantconnect.com/docs/v2/research-environment/meta-analysis/backtest-analysis)；[Live Analysis](https://www.quantconnect.com/docs/v2/research-environment/meta-analysis/live-analysis)；[Live Reconciliation](https://www.quantconnect.com/docs/v2/research-environment/meta-analysis/live-reconciliation)

本系统最值得借鉴的是：

- 从订单事件重建完整 fill；
- 同一个交易日重放计划订单和实际订单；
- 把价格差拆成信号延迟、下单延迟、滑点和规则偏差；
- 用相同代码生成 live-vs-shadow 差异报告。

不建议整体迁移。LEAN 核心是 C#/.NET，通过 pythonnet 支持 Python，接入成本明显高于在当前 Python 架构内补齐 reconciliation。

### 4. hftbacktest：有完整 L2/L3 时才值得引入

hftbacktest 支持 tick-by-tick、L2/L3 订单簿重建、feed/order latency 和 queue position fill model，可通过 Python/Numba 使用。[官方文档](https://hftbacktest.readthedocs.io/en/py-v2.3.0/)；[数据格式](https://hftbacktest.readthedocs.io/en/latest/data.html)

但它明确是“市场数据回放”：模拟订单不能改变被回放的市场，也不考虑真实市场冲击；大额主动单的 fill 可能不现实。[Order Fill 假设](https://hftbacktest.readthedocs.io/en/latest/order_fill.html)

当前系统拥有 SIP trades/quotes，而不是完整美股 L2/L3 队列。现阶段用它会增加复杂度，却不能恢复缺失的队列信息。建议保留为未来升级项，不作为第一阶段依赖。

### 5. TradeNote 与 TradesViz：适合看“产品形态”，不解决选股学习

TradeNote 是自托管开源交易日志，支持 Docker/MongoDB 和券商 CSV 导入；其通用模板要求“一次 execution 一行”，能保留实际成交粒度。[官方仓库](https://github.com/Eleven-Trading/TradeNote)；[券商导入格式](https://github.com/Eleven-Trading/TradeNote/tree/main/brokers)

它可以作为人工查看交易、标签和截图的可选界面，但：

- 没有全市场候选池或漏选归因；
- 没有特征/标签/OOS 研究流水线；
- JavaScript/MongoDB 与当前 Polars/Facts 数据层不同；
- GPL-3.0 不宜直接把代码嵌入闭源生产系统。

TradesViz 官方产品提供文件导入、券商同步、历史模拟和大量统计，但它是商业闭源产品，不能作为可审计的本地学习内核。[官方功能页](https://www.tradesviz.com/)；[官方导入说明](https://www.tradesviz.com/blog/import-complete-guide/)

结论：可以模仿二者的时间轴、截图、标签和交易统计界面；“选股为什么漏掉”仍必须由本系统实现。

### 6. FinRL、Jesse 和 backtrader

**FinRL** 的价值在 train-validation-test-trade 分区和 DataOps。官方明确用时间分区减少信息泄漏，并允许用户导入数据和调整时间粒度。[官方架构](https://finrl.readthedocs.io/en/latest/finrl_meta/overview.html)

不过复盘初期样本稀疏、奖励函数不稳定，直接使用强化学习容易把噪声包装成“自动进化”。建议等监督式标签、反事实和 OOS 门槛成熟后再评估。

**Jesse** 是加密资产框架，支持多周期 candle 回测、feature/label gather、scikit-learn 模型、Monte Carlo 和规则显著性检查。[官方仓库](https://github.com/jesse-ai/jesse)

这些接口设计值得参考，但其基础数据粒度是 candle、目标市场是加密资产，部分 Benchmark 能力属于 Premium，不能直接替代美股全市场复盘。

**backtrader** 有 CSV/Pandas feeds、bar replay 和 Analyzer。[Data Replay](https://www.backtrader.com/docu/data-replay/data-replay/)；[Analyzer](https://www.backtrader.com/docu/analyzers/analyzers/)

它没有现代化 feature/label/experiment store 和严格 OOS 协议，且为 GPL-3.0、维护活跃度明显较低。不建议新引入。

## Agent Skills 调研

### `tradermonty/claude-trading-skills`

这是本次发现中最有参考意义的一套交易类 skills。仓库为 MIT，维护活跃；但它更偏人工研究、波段交易和报告编排，不是全自动美股日内选股复盘引擎。[官方仓库](https://github.com/tradermonty/claude-trading-skills)

可参考的三个模块：

1. [`signal-postmortem`](https://github.com/tradermonty/claude-trading-skills/blob/main/skills/signal-postmortem/SKILL.md)
   - 记录 signal 的 5 日/20 日实际收益；
   - 分类 `TRUE_POSITIVE`、`FALSE_POSITIVE`、`MISSED_OPPORTUNITY` 和 `REGIME_MISMATCH`；
   - 按来源 skill 汇总结果，并生成权重调整建议和改进 backlog；
   - 内置 20 个信号的最低建议样本量。

2. [`trader-memory-core`](https://github.com/tradermonty/claude-trading-skills/blob/main/skills/trader-memory-core/SKILL.md)
   - 维护 IDEA → ENTRY_READY → ACTIVE → CLOSED 的 thesis 生命周期；
   - 保留原始 screener provenance；
   - 支持部分平仓、P&L、持仓天数，以及可选的 MAE/MFE；
   - 能生成持久化 postmortem 和按 thesis type 的汇总。

3. [`edge-hint-extractor`](https://github.com/tradermonty/claude-trading-skills/blob/main/skills/edge-hint-extractor/SKILL.md)
   - 把 market summary、anomalies 和 news reactions 转换成结构化 `hints.yaml`；
   - 规则输出可选 LLM 补充；
   - 适合把漏选簇转换成“待研究提示”，而不是直接改生产策略。

不能直接照搬的原因：

- 默认持有期是 5/20 日，不覆盖日内 1/5/15/30/60 分钟标签；
- 信号记录和 thesis 状态需要显式录入，缺少全市场每日自动对账；
- `MISSED_OPPORTUNITY` 是已有信号未执行，不等于“从全市场根本没进入候选池”；
- 权重建议没有自带 purged/rolling OOS 晋级门；
- FMP 日线适配器不足以承担本系统的 SIP/Massive 点时数据要求。

建议只吸收其 JSON/YAML 输出契约、生命周期和归因词汇，不把 skill 的主观文字直接当成模型训练标签。

### 其他发现

- [`marian2js/trading-skills/post-trade-review`](https://github.com/marian2js/trading-skills/blob/main/skills/review-learning/post-trade-review/SKILL.md)：结构清楚，强调不能用盈亏倒推决策质量，但主要是提示词式人工复盘，没有数据管道和统计验证。
- [`tianfh080058-gif/financial-codex-workspace/trade-journal-shadow-review`](https://github.com/tianfh080058-gif/financial-codex-workspace/blob/main/.agents/skills/trade-journal-shadow-review/SKILL.md)：支持 CSV/XLSX、FIFO roundtrip 和行为诊断，但仓库未声明许可证，不应复制代码进入产品。
- [Webull Agent Skills](https://github.com/webull-inc/webull-agent-skills)：官方 skill 重点是行情、账户和交易 API，不是 postmarket 复盘或漏选归因。

结论：没有找到可直接安装后便能完成本系统闭环的成熟 Codex/Claude skill。Skill 可以负责编排、生成解释和提出实验假设；事实重建、标签、反事实、OOS 和晋级必须由确定性程序负责。

## 建议建设的生产级复盘模块

### A. 盘前决策账本

每日锁定后写入不可变快照：

- `trade_date`、`asof_utc`、数据/config/code hash；
- 全量可交易 universe；
- 催化剂池、纯因子池、统一仲裁结果；
- 每只股票所有原始因子、标准化分、闸门结果和 rejection reason；
- 数据缺失、停牌、新闻时点和 provenance；
- 实际推送给用户的候选和预案。

没有这份账本，收盘后所有“为什么没选到”都容易变成事后解释。

### B. 结果标签工厂

对盘前 universe 和候选统一计算：

- 开盘后 5/15/30/60 分钟收益；
- 日内最高收益、最大回撤、MFE/MAE、收盘收益；
- 达到 +3%/+5% 的首次时间；
- 成本后可实现收益、成交量/价差/停牌等可交易性；
- 新闻与催化剂首次可见时间，区分“盘前可知”和“盘中新信息”。

标签必须 point-in-time、时区明确、缺失为 `N/A`，且不能用盘后修订数据冒充盘前已知事实。

### C. 漏选强势股归因

定义当天“值得检讨的强势股”，然后沿真实链路归因：

1. 不在基础 universe；
2. 数据缺失或质量隔离；
3. 被硬闸拒绝；
4. 候选池内但分数不足；
5. 已进入候选但统一仲裁未选；
6. 盘中新催化，盘前不可预测；
7. 新闻抓取/实体映射/分类缺口；
8. 技术、订单流或资金流因子缺口；
9. 实际不可交易，属于合理放弃。

每只股票只能有一个主因和若干次因，并附证据字段；无法证明时写 `unknown`，不能让 LLM 编理由。

### D. 反事实与消融

对每个漏选簇自动运行：

- 单一阈值上下移动；
- 单因子删除或新增；
- 催化剂、纯因子、订单流分别关停；
- 硬闸保持不变，只测试软排序；
- 统计新增真阳性、假阳性、成本、回撤和市场状态稳定性。

反事实结果只产生 `research hypothesis`，不直接写入生产配置。

### E. 成交与计划复盘

把实际 fill 与预案和同期 SIP 行情对齐：

- 是否追涨、提前、迟到或违反确认条件；
- arrival price、实际均价、VWAP/盘口基准和滑点；
- 仓位、止损、止盈、部分退出是否符合计划；
- 实际 MFE/MAE 与可实现退出区间；
- 分离“股票选对但操作差”“选股错误但操作规范”。

### F. 安全学习门

建议保留以下硬约束：

- Agent/LLM 只能解释和提出候选修改，不能直接改生产权重；
- challenger 先在 shadow 中生成版本化结果；
- 使用 rolling 或 purged OOS，禁止随机打乱时间；
- 同时看召回率、精确率、成本后收益、回撤和覆盖率；
- 达到预设样本量和多折稳定性后才允许申请晋级；
- 每次尝试数量、失败实验和完整 hash 都必须记录，避免只展示最好结果；
- 生产晋级仍需要显式人工审批。

## 实施优先级

1. **先建不可变盘前账本和收盘标签**：这是所有自动复盘的事实基础。
2. **补全市场漏选归因**：优先解决“为什么昨天那么好行情却空仓”和“为什么没选到涨幅榜标的”。
3. **建立选股、操作两个独立评分**：不能让一笔盈利掩盖违规操作，也不能让一次止损否定正确选股。
4. **加入反事实和因子消融**：先用现有 Polars/NumPy；无需立即引入 vectorbt。
5. **接入 shadow challenger 和严格 OOS**：达到样本门槛前只积累证据。
6. **最后增加 Agent 文字复盘**：读取确定性产物，生成中文简报和实验 backlog；不参与事实计算。

一句话总结：**先让系统每天保存“当时为什么选/不选”的完整事实，再用收盘结果形成标签，最后让改进建议经过反事实和 OOS；这才是可复利的自动复盘，而不是让 Agent 每晚写一篇漂亮总结。**
