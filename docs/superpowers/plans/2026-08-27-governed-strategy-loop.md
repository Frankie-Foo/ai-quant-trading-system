# 受控策略进化闭环实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用当前任务内的分步实现与审查流程。步骤使用复选框（`- [ ]`）跟踪进度。

**目标：** 将现有“复盘写 Memory”流水线接成可运行的 Challenger、影子验证、人工晋级和回滚闭环，同时禁止 Agent 直接修改生产策略或交易安全规则。

**架构：** 复用现有 PDCA、月度提案、RVOL OOS sandbox、accepted snapshot 与 Windows Task Scheduler。生产选择只读取经过校验的 active policy；月度任务只能生成 shadow challenger。Challenger 每日读取同一冻结候选池但不下单，收盘结果形成 accepted shadow evidence；人工命令在 OOS 与最少影子样本均通过后原子切换 active policy，并保留上一版本用于回滚。

**技术栈：** Python 3.12、Pydantic、Polars、SQLite/JSON 原子文件、pytest、PowerShell Task Scheduler。

---

### 任务 1：版本化策略 Policy

**文件：**
- 创建：`kernel/strategy_policy.py`
- 修改：`kernel/config.py`
- 测试：`tests/test_strategy_policy.py`

- [ ] 编写失败测试：合法 policy 只允许覆盖 `universe.min_rvol`；篡改哈希、未知字段、非 active 状态必须失败。
- [ ] 运行 `python -m pytest tests/test_strategy_policy.py -q`，确认失败。
- [ ] 实现 `StrategyPolicy`、规范哈希、原子写入、读取验证和 `load_config()` 的显式环境变量覆盖。
- [ ] 运行测试确认通过。

### 任务 2：Challenger 生成、人工晋级和回滚

**文件：**
- 创建：`scripts/manage_strategy_policy.py`
- 测试：`tests/test_manage_strategy_policy.py`

- [ ] 编写失败测试：bootstrap 不覆盖现有 active；sandbox 未通过不能建 challenger；少于20个影子交易日不能晋级；晋级保留历史；rollback 只能指向已验证历史版本。
- [ ] 运行定向测试确认失败。
- [ ] 实现 `bootstrap`、`build-challenger`、`approve`、`rollback`、`status`；所有写入使用临时文件加 `Path.replace()`。
- [ ] 运行定向测试确认通过。

### 任务 3：每日双轨影子选择与收盘评价

**文件：**
- 修改：`scripts/run_modern_funnel_stage.py`
- 创建：`scripts/evaluate_strategy_shadow.py`
- 修改：`schedule/postmarket.py`
- 测试：`tests/test_modern_funnel_stage.py`
- 测试：`tests/test_strategy_shadow.py`

- [ ] 编写失败测试：第一波产物记录 active 版本和 challenger 子集；challenger 永远不触发订单；收盘评价只使用冻结名单与 accepted top-mover postmortem。
- [ ] 运行定向测试确认失败。
- [ ] 第一波写入版本和 shadow 名单；Feishu 使用 active 版本；postmarket 生成 `research.strategy_shadow_outcome` accepted snapshot并加入任务证据链。
- [ ] 运行定向测试确认通过。

### 任务 4：月度任务接通 Windows 调度

**文件：**
- 修改：`schedule/monthly_evolution.py`
- 创建：`scripts/run_monthly_evolution_tick.ps1`
- 修改：`scripts/install_local_observation_tasks.ps1`
- 测试：`tests/test_monthly_evolution_schedule.py`

- [ ] 编写失败测试：月度提案之后必须运行 OOS sandbox 并且只能生成 shadow challenger；任何步骤失败时任务失败且不改 active。
- [ ] 运行定向测试确认失败。
- [ ] 串联月度提案、RVOL sandbox 和 challenger 生成；安装每日一次、由 XNYS 首交易日门禁控制的 Windows 任务。
- [ ] 运行定向测试确认通过。

### 任务 5：运行文档、审查和发布验证

**文件：**
- 修改：`README.md`
- 修改：`docs/RUNBOOK.md`
- 修改：`docs/PRODUCTION_DEPLOYMENT.md`
- 修改：`PROGRESS.md`
- 修改：`CHANGELOG.md`

- [x] 记录闭环边界、人工晋级命令、回滚命令和禁止自动生产变更。
- [x] 审查完整 diff，修复 Critical/Important 问题。
- [x] 运行定向测试、完整 pytest、Ruff、Mypy 和 compileall。
- [x] 运行只读 dry-run/status，确认 active 未被自动改变、Broker订单数为0。
