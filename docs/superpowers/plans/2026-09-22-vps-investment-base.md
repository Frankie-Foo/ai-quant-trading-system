# VPS 多维表记录与发布计划

> 面向 AI 代理：按以下步骤内联执行；当前环境没有 superpowers 执行子技能，使用现有测试工具与独立代码审查。

**目标：** 将生产记录切换到用户指定的 vps-work 多维表，保留 Paper 安全门、真实回执和历史账本，发布至固定 main 提交。

**架构：** 复用现有四表事件投影和客户端入口。VPS 客户端独立校验配置、只追加不可变事件，远端回读成功才返回带提供方的回执；SQLite 保存先于网络写入的意图，未知结果不盲目重发。飞书代码仅供历史兼容，VPS 模式不调用飞书。

**技术栈：** Python 3.12、SQLite、已安装 vps-work 2.1.3、Windows 隐藏任务、pytest。

## 文件与步骤

### 1. 记录后端

- 创建 `operations/vps_investment_base.py`：固定服务地址、四表配置、字段校验、隐藏 CLI、意图去重、完整回读。
- 修改 `operations/feishu_base.py`：唯一工厂增加显式 provider 分流；未知 provider 报错，VPS 故障不得回落飞书。
- 创建 `tests/test_vps_investment_base.py`，先测试失败再实现：
  ```python
  assert client.record_event(table, event_id, fields) == client.record_event(table, event_id, fields)
  assert runner.writes == 1
  ```
- [x] 测试正常写入、重放、正文冲突、重复远端行、分页/截断、写超时已成功、写超时未知后重启、缺字段、错误脱敏、隐藏进程。
- [x] 执行聚焦测试及完整测试；全部通过后提交有意修改。

### 2. 真实联调

- [x] 仅在新建专用文档创建选股、盯盘、成交、复盘四表，字段使用真实 schema ID。
- [x] 外置非敏感 binding JSON；环境选择 `AI_QUANT_INVESTMENT_PROVIDER=vps-work`，配置文件 SHA-256 固定绑定。
- [x] 四表只读检查；写一条明确标记的部署审计（不是交易样本），重复调用，核实同一远端记录及正文。
- [x] 更新健康检查、提示文案与 `AGENTS.md`，保留历史回执字段兼容性，不改写旧凭证。

### 3. 发布

- [x] 全量 pytest、Ruff、Mypy；独立审查确认无剩余重要缺陷。
- [x] 停用旧调度与 supervisor 入口；不动巴菲特或券商保护单。
- [ ] 只读核对仓位和订单，备份 SQLite，保留旧路径及证据哈希，绑定持久账本到新发布目录。
- [ ] 合并 main，固定 release SHA，安装隐藏任务；再次外部检查通过才启用唯一现代漏斗。
- [ ] 检查任务指向、实际运行日志、VPS/Livermore回执；记录未通过项，不凭测试结果声称实际成交。
