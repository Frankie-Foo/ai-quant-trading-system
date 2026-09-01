# AI Quant 企业级加固实现计划

> **面向 AI 代理的工作者：** 使用 TDD 按任务逐项实现；每个任务完成后执行定向测试并提交。最终使用独立代码审查和完整验证门禁。

**目标：** 将三段选股漏斗、Modern H15 Alpaca Paper、飞书审计、利弗莫尔通知和巴菲特实盘只读盯盘收敛为可恢复、可审计、默认冻结的企业级本地系统。

**架构：** 唯一自动下单链由纽约交易所日历驱动，08:00、09:25、09:35 三阶段通过持久化状态机串联；只有第三阶段计划、飞书和通知回执齐全后才允许 Modern H15 Paper。Paper 执行使用独立安全策略、原子保护单、实时 NBBO 门禁、SQLite 状态与 Outbox；巴菲特链保持只读并从类型和依赖层禁止订单 API。

**技术栈：** Python 3.12/3.13、Pydantic、Polars、SQLite、httpx、pytest、Ruff、Mypy、PowerShell Task Scheduler。

---

## 文件职责

- 创建 `operations/paper_runtime_policy.py`：统一 Paper 时间、风险、开关和故障策略。
- 创建 `operations/paper_state.py`：持久化订单生命周期、恢复状态、通知 Outbox 和幂等键。
- 创建 `schedule/modern_funnel.py`：三阶段漏斗状态机和阶段门禁。
- 创建 `scripts/run_modern_funnel_tick.ps1`：Windows 隐藏、幂等、一分钟调度入口。
- 修改 `scripts/install_local_observation_tasks.ps1`：只注册唯一生产漏斗、收盘复盘和必要只读链。
- 修改 `scripts/monitor_modern_momentum_paper.py`：使用安全策略、实时 NBBO、原子保护单、恢复和强制清仓。
- 修改 `execution/alpaca_paper.py`：支持受保护的可成交限价单和按系统订单 ID 恢复。
- 修改 `operations/autonomous_selection_handoff.py`、`operations/autonomous_plan_compiler.py`：缺失事实使用 `N/A`，计划回执成为开仓门禁。
- 修改 `operations/livermore_push.py`、`operations/feishu_base.py`：故障报警、UTF-8 校验、Outbox 重放和专用 Base 限定。
- 保留 `scripts/monitor_trade_plan.py`：巴菲特实盘只读；修复 Python 3.12、类型和格式。
- 删除无调度、无引用的一次性盯盘脚本及对应实现细节测试；通用研究算法保留在 `research/`。
- 修改 `pyproject.toml`：最低 Python 3.12；静态检查排除运行产物、第三方 vendor、报告和数据。
- 更新 `README.md`、`docs/ARCHITECTURE.md`、`docs/AUTONOMOUS_PAPER_LOCAL.md`、`docs/PRODUCTION_DEPLOYMENT.md`、`PROGRESS.md`。

### 任务 1：冻结和统一运行策略

**文件：**
- 创建：`operations/paper_runtime_policy.py`
- 创建：`tests/test_paper_runtime_policy.py`
- 修改：`pyproject.toml`

- [ ] 编写失败测试：缺少 `BROKER_WRITE_ENABLED=true`、kill switch 未关闭、缺少第三阶段回执、非 Paper host、15:00 后开仓分别失败。
- [ ] 运行：`.venv/Scripts/python -m pytest tests/test_paper_runtime_policy.py -q`；预期新测试失败。
- [ ] 实现 `PaperRuntimePolicy`：09:56 最早开仓、15:00 截止、15:45 撤买单、15:50 清仓；单票风险 0.5%、板块 0.75%、组合 1.5%、日亏 1.5%/2%；所有开关 fail-closed。
- [ ] 运行定向测试并提交 `feat(safety): centralize paper runtime policy`。

### 任务 2：三阶段漏斗持久状态机

**文件：**
- 创建：`schedule/modern_funnel.py`
- 创建：`tests/test_schedule_modern_funnel.py`
- 修改：`scripts/install_local_observation_tasks.ps1`
- 创建：`scripts/run_modern_funnel_tick.ps1`
- 修改：`tests/test_deployment_contracts.py`

- [ ] 编写失败测试：XNYS 休市无动作；ET08:00 仅第一波；09:25 只读第一波；09:35 只读第二波；窗口内重试；窗口外禁止补跑；同阶段重复 tick 不重复推送或启动。
- [ ] 编写失败部署测试：安装器必须注册启用的 `Trading System V2 - AI Quant Funnel` 一分钟任务，且不得注册或启用旧 Paper 执行入口。
- [ ] 实现 SQLite 阶段状态、租约和回执门禁；所有时间从 `America/New_York` 和 XNYS 日历计算。
- [ ] 运行定向测试并提交 `feat(schedule): add durable three-stage funnel`。

### 任务 3：计划真实性和第三阶段门禁

**文件：**
- 修改：`operations/autonomous_plan_compiler.py`
- 修改：`operations/autonomous_selection_handoff.py`
- 修改：`scripts/prepare_autonomous_selection_handoff.py`
- 修改：`tests/test_autonomous_plan_compiler.py`
- 修改：`tests/test_autonomous_selection_handoff.py`

- [ ] 编写失败测试：催化评分、板块或盘前涨幅缺失时不得使用固定数字或 SPY 冒充；消息显示 `N/A`；缺少飞书/利弗莫尔成功回执不能生成可执行授权。
- [ ] 将计划目标与观察事实分离；目标 3R 可保留为规则，事实值必须携带来源或 `N/A`。
- [ ] 生成不可变 `open_confirmation.json` 和哈希回执，Paper 只接受同交易日、同候选池、同策略版本的完整回执。
- [ ] 运行定向测试并提交 `fix(plan): require truthful confirmed execution handoff`。

### 任务 4：原子保护订单和实时点差

**文件：**
- 修改：`execution/alpaca_paper.py`
- 修改：`scripts/monitor_modern_momentum_paper.py`
- 修改：`tests/test_alpaca_paper_broker.py`
- 修改：`tests/test_modern_momentum_paper.py`

- [ ] 编写失败测试：下单前读取最新 NBBO；点差或价格滑移超过 0.10% 撤销；买入 payload 为限价 bracket/OTO 并原子带止损与 3R；真实 host 永远拒绝。
- [ ] 实现可成交限价上限、实时 quote 新鲜度校验和原子保护单；退出优先于通知依赖。
- [ ] 运行定向测试并提交 `fix(execution): protect every paper entry atomically`。

### 任务 5：崩溃恢复、Outbox 和强制清仓

**文件：**
- 创建：`operations/paper_state.py`
- 创建：`tests/test_paper_state.py`
- 修改：`scripts/monitor_modern_momentum_paper.py`
- 修改：`tests/test_modern_momentum_paper.py`

- [ ] 编写失败测试：买单成交后崩溃、止损成交后崩溃、退出提交后崩溃、通知发送前后崩溃、重复进程、15:45 撤单、15:50 清仓均可恢复且不重复订单/消息。
- [ ] 使用 SQLite 事务记录 intent、broker ID、状态和 Outbox；启动时按 client order ID 对账，只管理本系统订单；未知持仓冻结并报警。
- [ ] 实现首次/二次 60%/40% 风险预算，最多两次；同票、板块、组合和日损保险丝统一走 `PaperRuntimePolicy`。
- [ ] 运行定向测试并提交 `feat(recovery): make paper lifecycle restart safe`。

### 任务 6：故障报警和自动恢复

**文件：**
- 修改：`operations/livermore_push.py`
- 修改：`operations/feishu_base.py`
- 创建：`operations/runtime_alerts.py`
- 创建：`tests/test_runtime_alerts.py`

- [ ] 编写失败测试：外部故障首次、第三次升级和恢复各通知一次；开仓前故障阻断；持仓后仍允许退出；补写审计不重复。
- [ ] 实现最多三次有界退避重连/重试；失败后冻结；禁止代码或策略自修改；备用行情只标记观察用途。
- [ ] 运行定向测试并提交 `feat(ops): add deduplicated fault recovery alerts`。

### 任务 7：收敛旧入口和巴菲特只读边界

**文件：**
- 修改：`scripts/monitor_trade_plan.py`
- 修改：`tests/test_trade_plan_monitor.py`
- 删除：经 `rg` 确认无调度和无引用的一次性 `scripts/monitor_*.py` 及对应测试。
- 修改：`tests/test_architecture_contracts.py`

- [ ] 编写失败架构测试：巴菲特模块不得导入 `execution` 或 Broker；旧脚本不得出现在调度器、安装器和生产文档。
- [ ] 修复 Python 3.12 语法、类型和 UTF-8 消息；删除无价值入口，保留通用研究计算。
- [ ] 运行定向测试并提交 `refactor(runtime): keep one paper path and readonly buffett`。

### 任务 8：清零静态检查和测试失败

**文件：**
- 修改：`pyproject.toml`
- 修改：Mypy/Ruff 报告涉及的自有源码和测试。
- 修改：`tests/test_cloud_market_coverage.py`、`tests/test_execution_settings.py`

- [ ] 配置 Ruff 排除 `runtime/`、第三方 vendor、报告、数据和构建产物。
- [ ] 逐个修复 Ruff 错误；不得用全局 `noqa` 或关闭规则隐藏问题。
- [ ] 逐个修复 Mypy 错误；测试替身必须满足公开 Protocol，不用无界 `Any` 掩盖。
- [ ] 修复环境污染测试，确保测试显式清理交易开关；更新 Alpaca 测试替身签名。
- [ ] 运行 `ruff check .`、Mypy 全命令、全量 pytest；提交 `chore(quality): make repository checks green`。

### 任务 9：文档、演练和发布门禁

**文件：**
- 修改：`README.md`
- 修改：`docs/ARCHITECTURE.md`
- 修改：`docs/AUTONOMOUS_PAPER_LOCAL.md`
- 修改：`docs/PRODUCTION_DEPLOYMENT.md`
- 修改：`PROGRESS.md`
- 创建：`scripts/run_paper_acceptance_drills.py`
- 创建：`tests/test_paper_acceptance_drills.py`

- [ ] 文档只描述唯一生产链、ET/DST 时间、风险规则、报警、恢复、冻结和一次性解冻流程。
- [ ] 实现无真实订单的时钟、崩溃、幂等、未知持仓、故障、强制清仓演练，并生成哈希审计收据。
- [ ] 运行 Python 3.12 与 3.13 验证；若本机缺少 3.12，记录阻断并使用 CI/独立环境补齐，不伪造结果。
- [ ] 执行外部只读 Alpaca、专用飞书 Base 配置和利弗莫尔身份检查；不访问断开的旧 Base。
- [ ] 提交 `docs(ops): document paper hardening evidence`。

### 任务 10：独立复审与受控烟测准备

**文件：**
- 创建：`runs` 外的版本化烟测说明不需要；烟测回执仅写 `runs/` 和专用飞书表。

- [ ] 独立审查 `f1fc268...HEAD` 的标准与规格两轴。
- [ ] 修复全部 P0/P1；重新执行全量验证并记录原始退出码。
- [ ] 保持 Windows Paper 任务禁用，提交验收报告；等待用户一次性解冻确认。
- [ ] 下一 XNYS 交易日获确认后执行最多 100 美元、`smoke` 标记的 Alpaca Paper 闭环；不计策略绩效和 Memory，15:50 前清仓。

## 最终验收命令

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\mypy.exe data_plane kernel research execution schedule agent_gateway scripts tests
.\.venv\Scripts\python.exe -m scripts.run_paper_acceptance_drills
```

验收标准：四条命令退出码均为 0；没有真实交易入口；Paper 调度保持冻结；外部只读检查成功；所有证据含版本、UTC 时间、配置哈希和代码提交。
