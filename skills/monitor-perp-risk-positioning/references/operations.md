# 运行与故障处理

## 目录

1. 原生安装
2. Docker
3. 常用命令
4. 数据与备份
5. 故障语义

## 原生安装

要求 Python 3.11+：

```bash
python -m venv .venv
python -m pip install .
perp-risk doctor
perp-risk smoke-live
```

生成自定义配置：

```bash
perp-risk init --output config.yaml
perp-risk --config config.yaml doctor
```

环境和配置可以 Clone，但 SQLite、备份、`.env` 和真实密钥不得提交。

## Docker

```bash
docker compose up --build
docker compose logs -f perp-risk
```

默认数据卷保存 SQLite 和 `latest.json`。生产部署应固定镜像版本、限制出站目标、
使用容器 Secret/环境变量，并单独采集标准输出。

## 常用命令

```text
snapshot         单次采集、评分和持久化
watch            默认 60 秒持续运行
status           最新已持久化快照
recommend        合并调用方指定目标
record-outcome   记录 benchmark/trade 结果
review           双向复盘
propose-config   生成 challenger
approve-config   人工哈希确认后晋级
smoke-live       不持久化、不通知的真实接口烟测
doctor           离线诊断
schema           导出 JSON Schema
backup/restore   本地一致性备份和恢复
```

## 数据与备份

默认数据位置：

- Windows：`%LOCALAPPDATA%/monitor-perp-risk-positioning`
- macOS：`~/Library/Application Support/monitor-perp-risk-positioning`
- Linux：`${XDG_DATA_HOME:-~/.local/share}/monitor-perp-risk-positioning`

60 秒规范化观察默认保留 180 天；快照、状态变化、配置候选和结果长期保留。

普通备份：

```bash
perp-risk backup --output backup.sqlite3
```

加密备份先把至少 12 位口令保存到 OS keyring，或在容器设置
`PERP_RISK_BACKUP_PASSPHRASE`：

```bash
perp-risk backup --output backup.bin --encrypt
perp-risk restore --input backup.bin --encrypted --destination restored.sqlite3
```

恢复默认拒绝覆盖现有数据库；只有用户明确确认时使用 `--force`。

## 故障语义

- 单个合约失败：保留其他合约，供应商状态为 `partial`。
- 整个平台失败：平台状态为 `unavailable`，不伪造观察。
- 整体覆盖低于 35%：目标 `unavailable`，倍率上限 `0.5x`。
- 平台方向相反：禁止 `1.2x`；分歧度达到 50% 时 `conflicted`、上限 `0.5x`。
- 爆仓源未配置/失败：爆仓为 `null`，禁止 `1.2x`。
- 强 `risk_off`：单个独立窗口可降至 `0x`。
- 普通 `risk_off`：两个独立窗口降至 `0.5x`。
- 恢复或加仓：两个独立窗口确认；同一 60 秒窗口重复运行不增加计数。
- Skill 故障只阻止新增风险，不拥有账户清仓权；主系统整体故障规则另行执行。
