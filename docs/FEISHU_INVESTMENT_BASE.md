# AI 投资飞书多维表接入

飞书只接收状态转换：选股信号、盯盘触发、模拟交易和收盘复盘。
每秒轮询、行情快照和本地状态不写入飞书。

## 安全边界

代码只接受下面的专用 `FEISHU_INVESTMENT_*` 配置，旧的通用变量会直接报错：

```dotenv
FEISHU_INVESTMENT_BASE_TOKEN=
FEISHU_INVESTMENT_BASE_TOKEN_SHA256=
FEISHU_INVESTMENT_SELECTION_TABLE_ID=tbl...
FEISHU_INVESTMENT_MONITOR_TABLE_ID=tbl...
FEISHU_INVESTMENT_TRADE_TABLE_ID=tbl...
FEISHU_INVESTMENT_REVIEW_TABLE_ID=tbl...
FEISHU_INVESTMENT_EVENT_ID_FIELD=事件ID
FEISHU_INVESTMENT_LOCK_DB=runs/feishu-investment-events.sqlite3
FEISHU_INVESTMENT_AUDIT_REQUIRED=true
```

四个表 ID 必须不同，并且每张表都需要一个可写文本字段 `事件ID`。
`信号ID`、`触发ID`、`交易ID`、`复盘ID`可以继续作为自动编号展示字段，不能承担幂等键职责。

生产环境设置 `FEISHU_INVESTMENT_AUDIT_REQUIRED=true`；本地离线测试可以关闭，代码仍会记录本地日志。
Base token 指纹用于阻止部署时误把配置换成别的 Base。代码不会读取、解析或访问旧 Base。

原项目使用官方 `lark-cli`，登录和查看自己的表结构由运行账户完成；本项目代码不会自动创建表、字段、视图或仪表盘。

## 四张表字段契约

除 `事件ID` 外，下面字段是代码投影使用的字段。字段名必须与自己的 Base 一致；如果你的表名不同，先修改自己的表字段或再调整投影映射。

### 选股信号

`事件ID`、`选股时间`、`交易日期`、`股票代码`、`选择排名`、`RVOL`、`参考价格`、`日均成交额`、`ATR比例`、`市值分层`、`选择快照ID`、`状态`、`触发原因`、`来源`、`消息正文`

### 盯盘触发

`事件ID`、`触发时间`、`交易日期`、`股票代码`、`触发类型`、`操作`、`触发原因`、`触发价格`、`持仓股数`、`订单ID`、`状态`、`来源`、`消息正文`

### 模拟交易

`事件ID`、`成交时间`、`交易日期`、`股票代码`、`操作`、`状态`、`计划ID`、`订单ID`、`请求股数`、`成交股数`、`触发原因`、`来源`、`消息正文`

### 复盘

`事件ID`、`复盘时间`、`交易日期`、`复盘类型`、`状态`、`复盘ID`、`程序复盘ID`、`选股复盘ID`、`关联证据`、`触发原因`、`来源`、`消息正文`

## 写入语义

每个状态转换先按 `事件ID` 精确查询，再在本机 SQLite 锁内写入并读回校验。
重复运行复用原记录；并发运行不会同时创建同一个事件。
飞书写入失败不会改变已经完成的模拟交易；若设置了 `AUDIT_REQUIRED=true`，调度任务会把审计失败报告为失败，自动盘中通知仍不会把飞书故障伪装成交易结果。
