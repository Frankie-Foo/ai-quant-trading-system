# 首次 SIP 选股到 Paper 成交验收

目的：验证“总股本缓存 × 实时 Alpaca SIP 成交”可以完成三段选股并在**自然出现**的
H15 信号下提交一次受保护的 Alpaca Paper bracket。不是为了测试而强制买卖；全程禁止
真实交易。

## 前置条件

- 漏斗按美东交易时钟执行：08:30、09:00、09:30、09:35。Alpaca SIP 凭据仍必须满足其独立授权窗口；窗口外缺数据时阻断，不延迟波次、不伪造快照。
- 用户确认后先运行 `python -m scripts.cache_sec_shares_outstanding --max-ciks 100`。
  它按现有活跃普通股参考表中的 CIK 分批下载 SEC Company Facts，可随时中断和继续；默认
  缓存路径为 `<data-root>/cache/sec-shares-outstanding.parquet`。若改用自有路径，再设置
  `AI_QUANT_SHARES_CACHE_FILE=<绝对路径>`。缓存至少含 `symbol`、`shares_outstanding`、
  `available_at`、`source`、`provenance`，并附 CIK、申报日期和提取时间；不得用手工估值
  或未来数据补齐。
- `ALPACA_SIP_ENV_FILE` 指向用户提供的行情凭据；Paper 凭据、专用 Feishu、Livermore、
  kill-switch、账户对账都处于现有运行时要求的正确状态。
- 不创建或修改实盘账户、订单、持仓和密钥文件。

## 执行顺序

1. 离线：运行本分支的目标 pytest、Ruff、Mypy。确认没有 `fetch_ticker_details` 出现在
   `scripts.build_selection_gates` 或 `scripts.prepare_modern_momentum_forward` 的当前路径。
2. 只读市值：在授权的 Alpaca SIP 窗口运行
   `python -m scripts.refresh_event_sip_market_caps --trade-date <交易日> --data-root <数据根>`。
   只接受 `event.sip_market_cap` 的 accepted 快照；每条为缓存股本乘新鲜 SIP 成交价。
   覆盖不完整、报价超过 15 秒或来源异常均停止，不能进入下一步。
3. 第一、二、三波：按 `schedule.modern_funnel` / `scripts.run_modern_funnel_stage` 运行，
   检查第一波市值覆盖、中文推送、候选冻结、第二波与开盘完整 K 线。到第三波结束前，
   不带 `--arm-paper`，因此不能写 Paper 订单。
4. 运行 `python -m scripts.monitor_modern_momentum_paper --check ...` 完成账户、持仓、订单、
   计划哈希和通知配置的只读对账。
5. 只有第三波预案已写入、H15 自然触发、点差/时效/止损/风险门全部通过且现有保护订单可
   原子创建时，才在用户确认的 Paper 运行时加 `--arm-paper`。若当天无合格信号，结果应为
   “无成交”，不发送测试性市价单。
6. 成交后只核对一次买入推送、保护止损和 3R 目标；15:00 ET 后不再开仓，15:45 撤单，
   15:50 平掉系统仓位。最后生成复盘和 Loop 本地 outbox，远端回传仍遵守既有契约。

## 验收记录

记录交易日、缓存来源版本、SIP 快照 ID、三段候选、预案哈希、Paper 订单 ID（若有）、
成交与退出原因。没有信号或任何门槛失败都属于通过的安全结果；不得将其改写为成交。
