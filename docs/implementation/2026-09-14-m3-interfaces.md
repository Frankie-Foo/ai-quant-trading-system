# M3 初版：显式 records 接口与实施流程

状态：初版代码与测试流程已写入，仅静态阅读，未经运行验证。不是 M3 完整验收或经济验收。本轮不编写测试、不执行测试或任何命令验证，不提交、不暂存。

## 范围与步骤

目标：确定性地分别统计盘后发现热度与全事件净收益，不授予交易或晋级权限。
架构：`research/event_edge.py` 接收调用方提供的 typed records、交易日历、冻结时间和成本批准证据；只返回不可变研究报告。使用标准库计算及仓库已有 Pydantic 校验，不读写业务文件、环境变量、网络或其他业务模块。ET 边界使用标准库 ZoneInfo 的 America/New_York 时区规则，依赖系统时区数据库或已有 tzdata；不自行安装、下载或回退成固定 UTC 偏移。

- [x] 新增 `research/event_edge.py`：输入校验、冻结边界、去重、热度、全事件统计与收缩。
- [x] 在本文记录实际 API、计算口径、M1/M2 适配责任及未实现部分。
- [x] 写未来测试流程（仅自然语言）。本轮不创建测试文件或 demo。

只新增本模块及本文。不修改 `research/registry.py`、`research/catalyst_scoring.py`、CLI、任务或生产配置。实验模块本轮不实现；既有 `ResearchSplit` / `ResearchRun` 契约原样保留。

## 实际 API

唯一计算入口是 `build_event_edge(*, records, top10_records, trading_sessions, decision_session, freeze_at, cost_approvals) -> EventEdgeReport`。所有参数显式必填；无数据时显式传空序列。没有默认时钟、隐式历史数据、自动批准或持久化。

公开数据类型为 `Provenance`、`TradingSession`、`CostApproval`、`EventRecord`、`Top10Record`、`EdgeStatistics`、`FactorHeat`、`EventEdgeReport`。采用 frozen Pydantic 模型，拒绝额外字段、非有限数及隐式字符串日期转换。调用入口要求对应类型实例，并重新校验其导出字段，避免 `model_construct` / `model_copy` 绕过输入验证。嵌套集合使用 tuple；调用方可用模型自带序列化接口取得 JSON，但模块不写文件。

### 输入记录

`EventRecord` 与 `Top10Record` 的共同字段：

| 字段 | 口径 |
| --- | --- |
| `event_cluster_id`, `symbol`, `session_date` | 样本身份；symbol 必须为规范大写，不自动归一化 |
| `factor` | 唯一主因；无法归因用明确的 `unknown`，不删除事件 |
| `observed_at` | 此记录身份、主因、来源等信息实际已知的时间；修订不能沿用早期时间冒充可见 |
| `matured_at` | 完整标签或缺失结论实际可用时刻，包含观测、发布和计算延迟；未成熟用 None |
| `coverage` | 调用方声明的该记录必需输入覆盖率，范围 0–1；不是全市场覆盖保证 |
| `provenance` | 明确来源类型、来源 ID、快照 ID、非空证据 ID tuple、质量状态 |

所有时间必须为 aware UTC；空白 ID、非法 symbol、未知来源、非 passed 质量状态均拒绝。`Provenance.source_kind` 只接受 `market_data`、`research_replay`、`broker_fill`、`manual`。结构校验不等于证据验真；模块不下载原文、不验证签名、不认证批准人。

`EventRecord` 另外必填：

- `source="all_events"`：调用方承诺提供被捕获全事件 cohort，不是赢家榜或只成交的成功记录。
- `label_definition_id`：固定标签及特征构造版本的身份；不同定义分开调用。
- `label_origin`：`event_response` / `decision_response` / `actual_fill`；禁止混账。
- `horizon_minutes`：15、30 或 60；每次调用只统计一个窗口。
- `label_start_at`：该窗口真实起点，必须在显式常规交易时段内；event_response / decision_response 另须在 10:00 ET 至 min(15:00 ET, 当日实际收盘) 的左闭右开区间内。不得回填新闻发布时刻来冒充真实决策起点。
- `cost_model_id`：net_return 已扣成本的模型版本；M3 不重新扣费。
- `net_return`、`mfe`、`mae`：小数收益，不是百分数点。MFE 非负，MAE 非正；MFE/MAE 为从同一起点归一化的价格路径极值，包含起点零值，不是另一次成本扣除。
- `missing_reason`：net_return 缺失时必填，有收益时必须为空。MFE/MAE 缺失各自独立计数，不推导为零。

`actual_fill` 仅接受 `broker_fill` 或 `manual` 来源，且两种来源仍需分开调用。它不受研究窗口 10:00–15:00 限制，只接受外部已经规范化、具有真实持仓证据的完整 15/30/60 分钟常规时段标签。本模块不生成实际执行标签，不验证持仓状态、真实退出、成交价或费用。真实持仓规则由外部标签提供方执行；提前退出、跨时段持仓等不适合本固定窗口接口的记录不得强行传入，不能把任意实际退出收益伪装成固定窗口收益。

`Top10Record` 另外要求 `source="postclose_top10"`、`rank`（1–10）、`interval_return`（10:00–15:00 区间简单收益）以及缺失时的 `missing_reason`。排名为整个当日 cohort 的排名，不是因子内排名；记录至少在该交易日实际收盘后可见。短市没有完整 10:00–15:00 窗口时，调用方应提供缺失值和原因，不能替换为另一时间窗口。M3 不从行情重建排名或核实此区间。

### 日历、冻结与成本批准

`TradingSession(session_date, open_at, close_at)` 明确提供美国股票常规开收盘 UTC 时刻，支持调用方给出的 DST 和半日市。日历严格升序、日期唯一且包含 `decision_session`，开收盘 UTC 日期应与该 session_date 一致。调用方必须提供完整交易日列表；模块不会识别或补回漏列的真实交易日。

`freeze_at` 不晚于决策日开盘；此前日历中的交易日必须全部已收盘。任何输入记录的日期必须在决策日前的显式日历中；`observed_at`、非空 `matured_at` 必须严格早于 freeze。等于边界也拒绝。即使记录在 60 日窗口外也要通过这些校验，不静默过滤不合法的未来数据。

未成熟记录只能以 `matured_at=None` 且所有结果字段为 None 传入，计入覆盖分母但不进入收益统计。event_response / decision_response 的非空结果必须满足 `label_start_at + horizon_minutes <= min(15:00 ET, session.close_at)`，且起点不早于 10:00 ET。14:40 的 15 分钟标签可完整结束，30/60 分钟标签不可进入成熟收益统计；起点等于或晚于研究终点直接拒绝，缺失记录也不例外。半日市使用实际收盘缩短研究终点。

actual_fill 的外部规范化固定窗口结果以 `session.close_at` 为时间上限，不混用研究 15:00 截止。所有非空结果的 `matured_at` 不早于相应窗口终点；起点有效但窗口被对应截止时间截断时，只能提供全空结果的缺失或未成熟记录，不能冒充完整标签。未来已成熟的归档记录不能直接用于过去 freeze；调用方须提供当时的真实记录版本，不得简单改时间戳。

成本批准单独使用 `CostApproval(cost_model_id, approved_at, approved_by, evidence_id)`，全部批准时刻严格早于 freeze，同成本 ID 多条批准直接拒绝。无批准传空 tuple；统计仍保留所有盈亏，输出 `cost_not_approved_at_freeze`。这不是成本模型验收，批准人与证据由调用方真实提供，不能自动填入用户姓名。

### 去重和来源隔离

每个账本独立以 `(event_cluster_id, symbol, session_date)` 去重。逐字段完全一致的重复记录只留一次并报告移除数；任何冲突，包括主因、收益、缺失状态、覆盖、证据或时间不同，整次调用拒绝。主因不在去重键内，不可借多因子归因重复增样。没有最新优先、最高收益优先或隐式 revision 选择。

同一次全事件调用要求 `label_definition_id / label_origin / horizon_minutes / cost_model_id / provenance.source_kind` 全部一致。不自动混合人工成交、研究回放和券商成交。不同来源 ID 可以属于同一来源类型，但必须遵守同一 cohort 与标签定义。Top10 另要求每个日期的 rank 与 symbol 各自唯一，不能把同一股票拆成多个事件占榜。

M1/M2 只需构造上述记录，不需调用对方未落地函数。M1 负责稳定事件簇、主因版本、真实首见/修订时间、完整正负 cohort、来源和覆盖；M2 负责固定标签定义、真实窗口起点、成熟时间、净成本、MFE/MAE、Top10 排名及缺失状态。适配器与输入接口确认后再讨论 CLI。

## 计算口径

### 全事件交易优势

基线取决策日前显式交易日列表最后 60 项；不足 60 项照实计算并附原因，不用自然日补足。报告保留全部合法输入记录，窗口外记录不参与基线。总体及各因子分别输出：

- `total_count`：去重后该组基线记录数；`count`：非缺失净收益数。
- `missing_count`：已成熟但净收益缺失；`unmatured_count`：未成熟。三者满足 total_count = count + missing_count + unmatured_count。
- `wins/losses/flat_count`：严格正、负、零收益数；零收益保留在胜率及期望分母中，缺失不算亏损。
- `independent_events`：有净收益的不同 event_cluster_id 数；`independent_days`：本组有净收益的不同 session_date 数，不借用全局天数。
- `win_rate`：wins / count；`mean` 与 `expectancy`：所有非缺失净收益算术均值；`median`：同一样本中位数。
- `avg_win`：严格正收益均值；`avg_loss`：严格负收益均值（负数，不取绝对值）；PF 为正收益总和 / 负收益绝对值总和。
- `coverage`：count / total_count；`source_coverage`：各去重记录输入 coverage 的算术均值。这两个数字不能替代未捕获事件的总体覆盖率。
- `mean_mfe/mean_mae` 及各自缺失数；`sample_keys` 指向实际贡献净收益的逐笔记录。完整记录在报告内，支持追溯标签和证据。

分母为零的指标用 None；无亏损时 PF 为 None，不返回无穷大。有限输入导致不可表示的比例也降为 None。空输入的统计量为 None，计数为零，不生成虚构收益。

因子 `shrunk_expectancy = w * factor.mean + (1-w) * overall.mean`，`w=n/(n+30)`。这里 n 使用本因子有净收益的独立事件簇数，不是重复新闻数；同簇跨股票或日期仍只计一个独立事件。均值仍是去重后的 event/symbol/day 等权描述统计，不能据此声称不同股票或日期独立。总体均值包括本因子，不做 leave-one-out；只有一个因子时收缩不改变均值。总体的 shrunk_expectancy 等于自身均值，shrinkage_weight 仅展示其 n/(n+30)，不再向未知上级收缩。因子没有收益时不把总体先验伪装成该因子收益。

30 是固定候选先验强度，60 是固定基线长度；本模块不调参、不读取验证集或盲测集。输入 freeze 控制可见性，但不是 train/validation/holdout 隔离器。

无亏损/全部赢家、独立事件少于 30、本组独立交易日少于 5、日历少于 60、成本未批准、缺失覆盖、人工来源及 unknown 主因均有明确原因。所有结果额外保留 `population_completeness_not_verified`：仅凭显式 records 不能证明调用方没有漏交失败事件。

`expectancy_ci95=None`，`ci_method="not_estimated"`。没有伪造 95% 精度，也没有把相关记录数当独立样本用于置信区间。`promotion_eligible` 和 `production_eligible` 始终为 False，正净期望也不授予交易权限。

### 近期热度

对最近 10 个显式交易日内出现的因子，分别输出 3/5/10 日窗口。每条有值 Top10 贡献 `(11-rank)/55 * min(max(interval_return, 0), 0.3)`；保留窗口总分 `score`，新增 `daily_mean_score = score / available_sessions`，用于比较 3 日与 10 日等不同窗口的已观察日均热度。分母是该窗口实际提供的全部显式交易日数，不是有样本天数，也不是固定填入名义窗口长度。负收益记录保留但热度贡献为零；此截断只影响热度，绝不截断全事件净收益。

`event_count` 是窗口内去重记录数，`count` 是有区间收益的记录数，缺失与未成熟各自计数。`available_sessions` 是日历长度，`represented_sessions` 是该因子实际出现日期数；不是全市场完整发现日数。无有值记录时 score 与 daily_mean_score 均为 None，不把没有上传记录解释成真实零热度。少于 10 个排名不补造样本，缺失排名的总体覆盖不能仅靠本接口判定。

元数据固定为 `score_scope="observed_heat_only"`、`normalization="explicit_window_session_count"`、`complete_market_estimate=False`。分子只包含已观察贡献；除以所有显式窗口交易日只是尺度归一，不证明空白日的真实热度为零，不是完整市场估计。始终给出 `population_completeness_not_verified`；某因子有无样本日期时另给 `no_sample_days_not_verified_zero`，记录 coverage 不足另给 `incomplete_source_coverage`。3 日与 10 日的均分可比较，但覆盖差异仍可能造成高低变化，不直接解释为真实市场热度升降。热度与 expectancy 从不同 record 类型计算，报告明确标记 `discovery_only_not_expectancy`。

输出按样本键、因子、窗口和成本 ID 固定排序；无随机种子、系统时间或外部状态参与结果。

## 未来测试流程（本轮不编写、不执行）

下列是后续取得测试授权后再实施的自然语言验收规格，不是已经通过的测试，不含可执行测试或命令。

1. 输入边界：分别送入 naive/non-UTC 时间、NaN/正负无穷、空 ID、非法 source、非 passed 质量、错误 symbol、额外字段和绕过构造校验的模型；预期拒绝。合规 aware UTC、明确 None 和缺失原因应保留。
2. 冻结边界：分别让 observed_at、matured_at、批准时刻等于或晚于 freeze，预期整次拒绝。此前真实已知记录保留；未成熟全空记录只增加未成熟数与覆盖分母。
3. 交易日历：使用含周末、休市、DST 和半日市的显式真实日历；检查 3/5/10/60 窗口按列表位置取值。乱序、重复日期、缺决策日、前日未收盘、开盘后 freeze 都应拒绝。日历不足不补天数。
4. 标签成熟：研究起点早于 10:00 ET、等于或晚于 min(15:00 ET, 实际收盘) 均拒绝；14:40 的 15 分钟有值标签可保留，30/60 分钟有值标签拒绝，完整窗口恰于研究终点结束可保留。半日市按实际收盘截断；起点合法但跨截止窗口的全空缺失记录保留，绝不成为成熟收益。分别覆盖 DST 前后相同 ET 时间的 UTC 转换。actual_fill 的外部规范化标签不受研究 15:00 限制，但必须完整落在常规交易时段，不能据此声称验证实际持仓。盘后 Top10 在收盘前可见应拒绝。
5. 幂等与冲突：完全相同记录多次输入只增加 duplicates_removed；同键改收益、因子、来源或时间均拒绝，不受顺序影响。Top10 同日重复 rank 或 symbol 拒绝。
6. 正负与缺失：含正、负、零及成熟缺失和未成熟记录的固定样本，逐项手算 count、wins、losses、flat_count、均值、中位数、平均盈亏、PF、期望和两种 coverage；缺失不得影响盈亏和收益均值分母。
7. 极端分母：空集合、只有零、全赢家、全亏损、缺 MFE/MAE 及极大有限收益；无定义统计应为 None，不得产生 infinity 或把缺失填零。全赢家仍需成本和样本量警告。
8. 收缩与独立性：同簇不同股票/日期只增加描述统计 count，不扩大 independent_events；同日多簇不得增加 independent_days。手算 n/(n+30) 向相同基线总体均值收缩；本组少于 5 天不能借用其他组的天数。
9. 热度分账：手算 rank 1 和 rank 10 的权重，超过 30% 截断，负收益只在热度贡献零。分别手算 3/5/10 日总分和按实际显式窗口交易日数计算的日均分；每日相同已观察贡献时各窗口日均分应一致，总分仍随长度变化。窗口内有无样本日时分母不得缩为有样本天数，并须保留未证实为零警告及 observed_heat_only 元数据。短于名义窗口时使用实际提供日数且报告日历不足；全空窗口两个分数均为 None。改变 Top10 记录不得改变总体及因子交易优势；改变全事件净收益不得改变热度。
10. 来源与成本：分别混入不同窗口、起点定义、成本 ID、manual/replay/broker 来源，预期拒绝混账。空批准列表保留收益且输出未批准原因；未来批准拒绝；合法批准不移除其他证据不足原因。
11. 研究边界：排列输入顺序结果一致；结果保留贡献样本、原始记录和去重数；任何收益与样本量下 CI 仍为 None、method 为 not_estimated、两项资格为 False。后续隔离审查应确认模块没有环境、文件、网络、broker、任务、active policy 或跨业务模块调用。

## 未实现与后续边界

- 未新增 `research/event_experiments.py`。没有 append-only SQLite/JSON 实验账、manifest 哈希登记、失败/取消尝试保存、候选落盘或 blind holdout 单次开启锁。本轮不能声称完成 M3 自进化。
- 后续实验存储必须复用原 `ResearchSplit` / `ResearchRun`，另保存 input/feature/model/cost/code hash 和每次尝试，不修改原 registry 契约。先冻结切分、清除跨边界事件簇、按显式交易日 purge/embargo；同一 holdout 身份的单次开封需要持久唯一约束及事务，失败开启也不可抹去。此处仅列约束，不提供实现或完整保障。
- 未实现聚类 bootstrap、置信下界、概率校准、局部状态分层、近期 expectancy 偏移、回落比例、现金/SPY/QQQ 基准、成本敏感性、多重比较控制、实际执行/持仓标签生成、前向 Paper 或三年真实回测。
- 输入记录不能自行证明完整样本、真实日历、历史首次可见性、成本批准真实性或训练/盲测隔离；这些责任明确留在调用方及后续实验协议。没有真实数据质量报告或经济通过结论。
- 不写 CLI，不修改 M1/M2、不调用它们未落地的接口。无 env/network/broker/task/commit/stage，无 active.json 写入。

## 本轮证据边界

指定工作树：`D:/cdoeX-worktrees/ai-quant-event-research`。静态读取的 HEAD 指向 `codex/event-research-m3-20260914`。已阅读设计第 8/13/14 节及 M3、仓库规则和现有 registry；仅新增约定的两个文件。

采用 ponytail 的最小实现原则：复用已有 Pydantic、标准库统计，实验登记延后；采用 writing-plans 的步骤与接口说明结构。用户本轮禁测与写入白名单优先于技能和旧计划的测试先行、额外计划文件及提交要求。只进行了文本读取、写入和静态阅读，没有编写测试，也没有运行 pytest、ruff、mypy、compile、demo、导入检查或其他命令验证。代码可运行性与行为正确性尚未验证；在此停止。
