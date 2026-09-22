# 事件驱动重构：实施状态

开始日期：2026-09-14。用户指定 GPT-6 Astra / medium。
开发分支：codex/event-driven-v1-20260914。
本文件记录事实，不表示线上切换或盈利验收通过。

## M0 基线与候选配置

- 已建立隔离工作树；没有修改计划任务、线上 .env、账户或券商订单。
- 已记录 main c2e5966、feature/loop d6ce9e7、活动发布 bd51b34 的差异。
- 活动发布内容哈希记录在同目录 `2026-09-14-baseline.json`；16 个文件复核均未变化。
- main 与 feature/loop 不能快进，已解决 5 处冲突并完成本地合并提交 `a2aa365`，保留两边意图。
- 候选配置及公共接口测试由 Astra medium 实施，仍仅 shadow。
- 当前 M0 尚未全部完成：消费方接入和活动发布特有修复需要分别验证。

## 已执行的保留性回归

Python：D:/cdoeX-work/runtime/ai-quant/python312/Scripts/python.exe。
工作目录：D:/cdoeX-worktrees/ai-quant-event-v1。

- `-m pytest tests/test_paper_runtime_policy.py tests/test_strategy_policy.py -q`
  ：24 passed，exit 0。
- `-m pytest tests/test_modern_current_signal.py tests/test_modern_paper_lifecycle.py tests/test_paper_state.py tests/test_paper_entry_rounding.py tests/test_alpaca_paper_broker.py -q`
  ：107 passed，exit 0。

新增及最终独立验证：

- `-m pytest tests/test_event_policy_config.py tests/test_loop_merge_regressions.py -q`：94 passed。
- 全部 `tests/test_loop*.py` 初轮独立验证：96 passed；其后新增 10 个审查回归场景。
- 飞书故障迁移：先补 3 条测试，3 failed；实现后包含开仓回执保护共 56 passed。
- 最终 `-m pytest tests -q`：**1219 passed，81.22 秒，exit 0**。
- 本次修改的 12 个 Python 文件 Ruff 通过；候选配置及修复相关共 4 个文件 mypy 通过。
- 两位 Astra medium 交叉审查；发现并修复诊断上传故障阻断 Outcome、合法长策略版本号被拒绝两项问题，复核关闭。

测试命令仅在子进程中将 `AI_QUANT_ACTIVE_POLICY_FILE` 设为空，隔离本地生产配置；没有改写实际环境文件。以上是离线测试，不等于实际行情、券商、远端 Loop 或盈利验收。

候选接口包含配置严格校验、不可变复制、策略哈希、风险预算、显式交易日历和夏令时/半日市处理。现有消费者尚未接入哈希校验，持久风险账本尚未实施，不代表生产风控已统一。

Loop 当前远端契约要求至少 10 只排名和恰好 10 只裁决；不足时本地阻断，不以盘后赢家补齐。新模型允许少于 10 只的兼容契约仍需后续实现。

## 活动发布差异的部署门槛

| 差异 | 处理要求 |
| --- | --- |
| bd51b34：飞书投影故障时保留选股 | 已迁入开发树并通过红绿回归；保留本地选股和通知，缺预案回执仍不能开仓；未部署 |
| SIP 跨午夜加载和 Paper 凭据回退 | 分别校验允许时窗与凭据配对；SIP 行情权限不能自动冒充新的交易授权 |
| bars 下载重试与 last_minute 推进顺序 | 保留成功下载后才消耗分钟的行为，补故障重试回归 |
| launcher 租约与解释器路径识别 | 检查实际子进程身份及单实例竞争；不能用模糊尾部匹配放弃身份校验 |
| 盘后飞书失败继续 Loop | 复盘、本地审计、通知和回传的故障域分离，缺证据仍阻断相应上传 |
| frozen pool 与 gate_asof 时间 | 保留不可变池和真实可用时刻；每行 gate 时间不自动证明整个池当时已冻结 |
| hidden PowerShell/VBS wrappers | 确认参数转义、退出码、日志和没有重复调度，之后才迁移 |
| owner-attested historical CLI | 保留来源标签、不可伪造历史时点；不并入自动成交证据通道 |

所有上表项目完成前，不允许用新开发树覆盖活动发布或宣称“部署完成”。
M1—M7 尚未完成验收。历史行情、新闻首见、成本审批和远端契约兼容仍按设计逐项验收。

## 本批实现交接：SIP 市值接入待实际 Paper 验收

本批源码已写入开发树。新增事件模块相关目标套件为 32 passed；实时市值刷新、选择门、
前向池、调度与桌面工作流目标套件为 38 passed。新增及修改 Python 文件 Ruff 通过，
实时市值路径 Mypy 通过。上文 1219 passed 仅属于 M0 基线（截至 44a4b11），不覆盖
本批全部源码。没有部署、没有提交订单、没有改动活动发布工作树。

- M1：追加式事件账本、版本与首见时间、动态事件样本；复用 Alpaca 新闻 API 的显式凭据入口。现代漏斗按美东交易时钟运行，凭据不可用时必须阻断，不得把波次延迟到下一阶段。
- M2：完整 K 线特征、双股票集合 10:00—15:00 榜单、事件响应及报价回放标签、显式未审批成本假设。研究起点限制为 10:00，不将早于窗口的信号平移入窗口。
- M3：全事件样本统计、3/5/10 日热度、60 日收缩基线、缺失与成本审批门槛；不据此自动晋升策略。

当前实时市值路径已接入：用户总股本缓存 × 当前 Alpaca SIP 成交；不再在当前第一波
路径逐只请求 Massive 市值。缓存尚未建立，必须在用户后续指令下单独生成；缓存缺失、
非 SIP、未来/过期报价都会阻断第一波。现代第一波固定为美东 08:30，后续为 09:00、
09:30 和 09:35；不得用北京时间门禁改变阶段。实际行情、Feishu/Livermore、Paper 订单和端到端 Loop 仍未验收；完整流程见
`2026-09-14-first-paper-sip-acceptance.md`。不得将本批源码交接表述为整个系统完成。

接口文档见本目录 m1-event-data-handoff、m2-interfaces、m3-interfaces；具体文件均以 2026-09-14 开头。测试流程见 2026-09-14-modules-awaiting-tests.md。用户确认后才启动测试，测试不使用 Astra。
