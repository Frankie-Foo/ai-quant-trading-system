# AI-Quant 项目架构

本项目把三个历史聊天合并为一个可运行的 AI 投资代码库：

- `kernel/`：确定性快循环，负责特征、门禁和策略判断。
- `data_plane/`：行情、财报、公告和数据质量适配器。
- `execution/`：纸上交易、订单状态、恢复和对账。
- `operations/`：飞书事件账本、通知和无人值守运行时。
- `schedule/`：选股、盯盘、收盘复盘和纸上交易调度。
- `scripts/`：面向操作者的命令入口。
- `client/`：Windows/macOS 共用的 Electron 客户端和 Python sidecar。
- `tests/`：跨模块行为验证。
- `docs/chat-records/`：从 Codex 聊天重构出的需求、决策和验收记录。

外部接口的 seam 集中在 `data_plane/`、`operations/` 和 `execution/`；快循环不依赖 LLM SDK，不把秒级行情写入飞书。飞书只承载选股、触发、模拟交易、异常和复盘事件。

本次迁移只纳入源代码、测试、配置模板和文档。`data/`、`runs/`、报告、缓存、虚拟环境和桌面打包产物属于可再生文件，保留在 Git 忽略规则中。

## 基本验证

```powershell
python -m pytest -q
python -m ruff check .
python -m mypy data_plane kernel research execution schedule agent_gateway scripts tests
```

不要把 `.env`、API key、机器人 secret、交易凭证或真实客户数据提交到仓库。
