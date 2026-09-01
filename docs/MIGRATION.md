# 聊天与工作区迁移记录

迁移目标：把 Codex 中的 AI 量化、客户端和盯盘聊天，连同已有源代码，集中到本目录并由 Git 管理。

| 聊天 | Thread ID | 原工作目录 | 迁入位置 |
|---|---|---|---|
| AI量化（数据飞轮进化） | `019fd57d-85f6-7693-88fd-5aac0901a7b1` | `C:\Users\frank\Documents\Codex\2026-08-06\w` | 根仓、`operations/`、`schedule/`、`docs/chat-records/` |
| AI量化投资（客户端） | `019fc64a-671c-7ce0-856c-7e7fe0bd2f79` | `C:\Users\frank\Documents\Codex\2026-07-16\new-chat` | 根仓、`client/`、`tests/` |
| 盯盘 | `019fccd3-aa6c-7032-9f41-f7353b9589fa` | `C:\Users\frank\Documents\Codex\2026-08-04\vbot-pati-vcgdkijn1sw` | `scripts/`、`operations/`、`docs/chat-records/` |

## 已重构的共同接口

```text
行情/财报/公告
  -> 数据质量与点时快照
  -> 选股门禁
  -> 盯盘状态变化
  -> 纸上交易与风控
  -> 飞书事件账本
  -> 收盘复盘与规则迭代
```

关键决策已固定：只做多、默认纸上交易；每秒轮询留在本地；飞书只记录状态变化和可复盘事件；事件写入必须幂等；模型负责研究解释，确定性代码负责门禁、风控和撮合。

## 未迁入 Git 的内容

- `data/`、`runs/`、`reports/`：运行产物，可以重新生成。
- `client/release-windows-analyst/`：Windows 安装包和打包缓存。
- `.venv/`、各类测试/类型检查缓存。
- `.env`、机器人密钥、券商密钥和其他本机凭证。

这些规则既释放历史工作目录空间，也避免把不可审查或含敏感信息的文件带进仓库。
