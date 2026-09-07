# Paper 200,000 USD 发布与联调计划

> 面向 AI 工作者：使用现有分工逐任务实现并独立审查。此环境无 superpowers 执行子技能，使用现有子代理及主任务测试验证，不安装替代工具。

目标：发布已修复 Paper 执行链，把 100 美元验收上限提升为业主批准的 200,000 美元总名义金额上限，接通事实成交到 Loop。

架构：保留唯一 Modern H15 Paper 写入进程及持久化状态机。Loop 读取已验证计划与券商事实；研究收益和真实成交分别标记。发布来自 main 的明确 commit，保留旧目录与所有运行证据。

技术栈：Python、pytest、SQLite、PowerShell Task Scheduler、Alpaca Paper、Feishu、Livermore、Loop。

## 1. 额度发布

文件：scripts/monitor_modern_momentum_paper.py、scripts/run_modern_funnel_stage.py、PowerShell 安装与 tick 脚本、tests/test_modern_paper_release.py。

- [ ] 增加测试：200000 被允许；200000.01、NaN、Inf、缺失值拒绝；多标的/活动买单占用总额度；购买力不能替代账户净值。
- [ ] 运行 `python -m pytest tests/test_modern_paper_release.py -q`，确认测试先失败。
- [ ] 使用同一校验函数和发布常量，保留默认冻结、显式授权、风控与止损；先扣除现有持仓及活动买单，再计算新单额度。
- [ ] 同测试命令转绿；更新当前发布文档，不改写历史验收证据。

## 2. Loop 事实联调

文件：operations/loop_integration、scripts/sync_loop_daily_review.py、相关测试和独立 Loop worktree。

- [ ] 测试逐笔成交与累计快照不会重复计数；缺失源覆盖、费用或已批准计划不得伪造。
- [ ] 增加来自原生计划/完整观察池的上下文导出，以及券商事实适配和 scheduled review 接口。
- [ ] stage-only 验证真实历史证据，再对已授权 Loop 端点提交；记录响应 ID，不能以 HTTP 200 替代业务接受。

## 3. 合并、部署与验收

- [ ] 核对远程 main、生产未提交变更、任务参数、账户持仓/订单、专用 Base 与推送身份，秘密不进入输出。
- [ ] 两组修复提交后在干净发布 worktree 合并。运行完整 pytest、Ruff、Mypy、compileall 和 Paper acceptance drills；独立审查阻断项全部修正。
- [ ] 按仓库 main/CI 门禁合并；记录发布 SHA、配置哈希、回滚 SHA；保留原有 dirty worktree。
- [ ] 更新现有 Windows 调度到明确发布目录，后台隐藏窗口；共享历史证据不可丢弃，完整券商对账后才解除冻结。
- [ ] 验证外部只读连接、真实 Loop 接受记录、任务路径与额度。若休市，明确未验证真实成交，不为验收下单；保持下一交易日的已授权调度。

回滚：禁用唯一 Funnel、冻结新仓，保留保护/平仓路径；回切已记录旧 commit 与任务配置，不删除状态数据库，不放弃券商持仓。
