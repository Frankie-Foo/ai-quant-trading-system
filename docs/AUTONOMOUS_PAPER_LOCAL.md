# 本地自主模拟盘运行手册

## 产品边界

当前版本只允许美股普通股、只做多、只连接 Alpaca Paper。它不支持 ETF、期权、
期货、加密货币、做空或实盘。桌面客户端仍暂停；运行入口是三个彼此隔离的
本地进程：

```text
Alpaca SIP REST ──> 本地事件库 ──> 确定性策略内核 ──> Alpaca Paper
                         │               ▲
Alpaca 新闻 ──> 催化剂 Agent ─┐          │
                红队 Agent ──┼─> 安全信封 ┘
Paper/行情 ──> 确定性监督器 ──┘
                         │
                    利弗莫尔机器人
```

催化剂和红队目前都使用环境中已配置的 DeepSeek V4-Pro；监督器是确定性程序，
不调用大模型。深度学习训练按产品决定暂缓。任何 Agent、行情、Broker 读取、
安全信封或消息投递故障都禁止新开仓；已有仓位进入撤单并退出流程。系统永远
不会授权实盘。

## 第一次启动

1. 安装 Docker Desktop，并确认 Linux containers 正常运行。
2. 从示例生成本地文件：

   ```powershell
   Copy-Item .env.example .env
   Copy-Item config\autonomous_paper.example.json `
     config\autonomous_paper.local.json
   ```

3. 在 `.env` 中填写自己的 `ALPACA_PAPER_KEY_ID`、
   `ALPACA_PAPER_SECRET_KEY`、`DEEPSEEK_API_KEY`、
   `VPS_LIVERMORE_APP_SECRET` 和精确的 `VPS_LIVERMORE_CHANNEL_ID`。
   不要共享或提交 `.env`。
4. 用当日已接受的点时证据替换
   `config/autonomous_paper.local.json` 的全部示例字段。所有计划必须是同一个
   XNYS 交易日；`safety_envelope` 保持为
   `../runs/safety/<SYMBOL>.json`。
5. 保持以下安全默认值：

   ```text
   BROKER_WRITE_ENABLED=false
   TRADING_KILL_SWITCH=true
   ```

6. 构建并启动只读观察模式：

   ```powershell
   docker compose -f compose.autonomous-paper.yaml up -d --build
   docker compose -f compose.autonomous-paper.yaml ps
   docker compose -f compose.autonomous-paper.yaml logs -f --tail 100
   ```

只读模式即使 `.env` 被误改也缺少 `--arm-paper`，因此不能提交 Paper 订单。
它用于确认 SIP 更新、三角色证据、安全信封和中文机器人消息链路。

## 显式启用 Alpaca Paper

确认 Paper 账户是本系统独占、没有手工订单或未知仓位，并先停止只读容器。
然后同时满足三个开关：

1. `.env` 设置 `BROKER_WRITE_ENABLED=true`；
2. `.env` 设置 `TRADING_KILL_SWITCH=false`；
3. 使用带 `--arm-paper` 的受控覆盖文件启动。

```powershell
docker compose -f compose.autonomous-paper.yaml down
docker compose -f compose.autonomous-paper.yaml `
  -f compose.autonomous-paper.armed.yaml up -d --build
```

缺少任何一个条件都会保持只读或直接拒绝启动。该授权只影响 Alpaca Paper，
代码中没有实盘 Broker 路由。

## 紧急停止与恢复

立即禁止新增 Paper 写入：

```powershell
(Get-Content .env) `
  -replace '^TRADING_KILL_SWITCH=.*$', 'TRADING_KILL_SWITCH=true' |
  Set-Content -Encoding UTF8 .env
docker compose -f compose.autonomous-paper.yaml `
  -f compose.autonomous-paper.armed.yaml up -d --force-recreate paper-executor
```

若已有仓位，不要仅关闭进程：保持行情、Agent 和执行器运行，让故障策略撤单并
清仓。账户日内收益达到 -1.5% 后禁止新开仓；达到 -2.0% 后清仓并锁定当日。
所有仓位最迟在美东 13:00 清仓，不隔夜。

重启不会重复创建同一命令：订单意图、生命周期、尾仓状态和通知回执均存放在
`runs/*.sqlite3`。不要删除 `runs` 来“重试”订单。

## 日常核对

```powershell
docker compose -f compose.autonomous-paper.yaml ps
docker compose -f compose.autonomous-paper.yaml logs --since 30m
```

应看到：

- SIP 刷新持续成功且 `orders_submitted=0`；
- Agent 周期每 15 秒生成三角色证据；
- 健康安全信封的 `agents_healthy=true`、`push_healthy=true`；
- 执行器只记录物化动作，`live_trading_authorized=false`；
- 利弗莫尔消息为中文、`sender_type=bot`，盈亏仅使用百分比。

停机：

```powershell
docker compose -f compose.autonomous-paper.yaml `
  -f compose.autonomous-paper.armed.yaml down
```

迁移服务器时复制代码和完整 `runs` 状态目录，密钥通过服务器密钥管理重新注入；
不要复制开发机 `.env`。任何曾发到聊天、截图或终端记录中的密钥都应先轮换。
