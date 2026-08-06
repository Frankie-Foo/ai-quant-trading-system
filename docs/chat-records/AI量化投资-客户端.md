# AI量化投资（客户端）

- Thread ID：`019fc64a-671c-7ce0-856c-7e7fe0bd2f79`
- 迁移来源：`C:\Users\frank\Documents\Codex\2026-07-16\new-chat`

## 原聊天要点

- 以 `trading-system-windows-build` 为当前 Windows 主线，包含 Python sidecar、Electron 客户端、选股、盯盘、证据问答和收盘复盘。
- 研究面与人工实盘执行台隔离；本次迁移不启用实盘权限。
- 客户端只接收脱敏状态，密钥放 `.env` 或系统安全存储。
- 代码需覆盖飞书事件写回、订单恢复、Broker 对账、通知和测试。

## 重构落点

- Python 主仓：`data_plane/`、`kernel/`、`execution/`、`operations/`、`schedule/`
- Electron 客户端：`client/`
- 验证：`tests/`
- 配置模板：`.env.example`、`config.yaml`

## Git 迁移说明

原工作树是远程仓库的功能工作树，含未提交改动和 4.5 GB 以上的构建/运行产物。本项目只复制可审查源代码、测试和文档，在新的本地项目根初始化 Git；原远程历史不被伪造，敏感文件不被复制。
