# AI量化（数据飞轮进化）

- Thread ID：`019fd57d-85f6-7693-88fd-5aac0901a7b1`
- 迁移来源：`C:\Users\frank\Documents\Codex\2026-08-06\w`

## 原聊天要点

- 复用原项目的“本地执行 + 飞书可视化台账 + 版本化复盘”闭环。
- 飞书只记录选股、盯盘触发、模拟交易、异常和收盘复盘，不记录每秒轮询。
- 日常链路不需要人工审核；机器硬门禁负责数据过期、信号缺失、风险超限和重复执行。
- 盯盘作为独立子任务接入主项目；纸上交易不调用真实下单入口。
- 事件需要幂等写入，并保留策略版本、信号快照和触发原因。

## 重构落点

- 飞书适配器：`operations/feishu_base.py`、`operations/feishu_investment_events.py`
- 无人值守运行：`operations/autonomous_paper_runtime.py`
- 选股与盯盘：`scripts/build_selection_gates.py`、`scripts/monitor_watch_plan.py`
- 调度与复盘：`schedule/`、`scripts/run_autonomous_paper_session.py`

## 当前边界

项目仍处于工程验证阶段；`NO_CANDIDATE`、数据阻塞和风控拦截都是合法结果，不能用旧名单或测试数据冒充当日信号。
