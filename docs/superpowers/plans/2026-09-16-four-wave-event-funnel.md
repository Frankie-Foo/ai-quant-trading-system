# 四段事件驱动日内漏斗实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 在 XNYS 交易日以美东时间 08:30、09:00、09:30 形成可审计的 Top20、Top20、Top10 事件池；09:30 后由 Paper 监控执行已验证的入场，15:50 前清仓，盘后复盘并同步 Loop。

**架构：** API 只采集、缓存并校验新闻、SIP 行情、NBBO、历史 RVOL 与市值；脚本完成确定性排序、跨轮重复加权、风控、下单、状态机和审计；LLM 仅做受约束新闻归因、盘后解释与候选研究建议，不能产生价格、排名、买卖或风险参数。每轮候选池以不可变 JSON 快照保存，下一轮只能读取此前快照，防止未来函数。

**技术栈：** Python 3.12、Polars、Alpaca SIP/Paper、SQLite、现有 Livermore/Feishu/Loop 客户端、pytest。

---

## 文件职责

- 新建：`research/intraday_wave_ranking.py`：合并当轮 API 特征和前轮快照，输出稳定 TopN 与重复权重。
- 新建：`scripts/run_event_funnel_wave.py`：运行一轮选股、保存不可变快照、调用中文推送。
- 新建：`scripts/report_event_funnel_status.py`：读取 Paper 状态，生成半小时事实摘要；不下单。
- 修改：`research/modern_momentum_forward.py`：允许调用者明确 TopN，不改变默认 10。
- 修改：`scripts/prepare_modern_momentum_forward.py`：接收 `--limit` 与 `--output-source`，输出可复用输入。
- 修改：`schedule/modern_funnel.py`：阶段改为 08:30、09:00、09:30 ET。
- 修改：`scripts/run_modern_funnel_stage.py`：第一、二轮保存 Top20，第三轮保存带重复权重 Top10。
- 修改：`schedule/postmarket.py`、`scripts/sync_loop_daily_review.py`、`operations/loop_integration/review_builder.py`：无交易日真实复盘可提交 Loop。
- 测试：`tests/test_intraday_wave_ranking.py`、`tests/test_schedule_modern_funnel.py`、`tests/test_modern_funnel_stage.py`、`tests/test_loop_integration.py`。

### 任务 1：跨轮 TopN 评分

**文件：**
- 创建：`research/intraday_wave_ranking.py`
- 测试：`tests/test_intraday_wave_ranking.py`

- [ ] **步骤 1：编写失败的测试**

```python
def test_repeated_symbols_receive_only_prior_wave_bonus() -> None:
    first = [{"symbol": "AAA", "base_score": 8.0}]
    current = [{"symbol": "AAA", "base_score": 7.0}, {"symbol": "BBB", "base_score": 7.5}]
    ranked = rank_wave(current, prior_waves=(first,), limit=20)
    assert [row["symbol"] for row in ranked] == ["AAA", "BBB"]
    assert ranked[0]["repeat_count"] == 1
```

- [ ] **步骤 2：运行测试验证失败**

运行：`python -m pytest tests/test_intraday_wave_ranking.py -q`

预期：FAIL，`rank_wave` 未定义。

- [ ] **步骤 3：编写最少实现**

```python
def rank_wave(rows, *, prior_waves, limit):
    prior = {row["symbol"] for wave in prior_waves for row in wave}
    ranked = [
        dict(row, repeat_count=int(row["symbol"] in prior),
             weighted_score=float(row["base_score"]) + int(row["symbol"] in prior))
        for row in rows
    ]
    return sorted(ranked, key=lambda row: (-row["weighted_score"], row["symbol"]))[:limit]
```

- [ ] **步骤 4：运行测试验证通过**

运行：`python -m pytest tests/test_intraday_wave_ranking.py -q`

预期：PASS。

- [ ] **步骤 5：Commit**

```bash
git add research/intraday_wave_ranking.py tests/test_intraday_wave_ranking.py
git commit -m "feat: rank event candidates across intraday waves"
```

### 任务 2：三次选股快照

**文件：**
- 修改：`research/modern_momentum_forward.py`
- 修改：`scripts/prepare_modern_momentum_forward.py`
- 创建：`scripts/run_event_funnel_wave.py`
- 测试：`tests/test_modern_momentum_forward.py`、`tests/test_modern_funnel_stage.py`

- [ ] **步骤 1：编写失败测试**

```python
def test_forward_pool_default_and_explicit_limit() -> None:
    assert select_forward_pool(frame, market_caps=caps).height <= 10
    assert select_forward_pool(frame, market_caps=caps, limit=20).height <= 20
```

- [ ] **步骤 2：运行失败测试**

运行：`python -m pytest tests/test_modern_momentum_forward.py -q`

预期：新 CLI/快照断言失败。

- [ ] **步骤 3：最小实现**

08:30、09:00 分别重新取同一时点可用 API 数据并输出 `wave_0830_top20.json`、`wave_0900_top20.json`；09:30 输出 `wave_0930_top10.json`。每个文件包含 `observed_at_utc`、输入快照 ID、前轮文件 SHA256、`base_score`、`repeat_count`、`weighted_score`。09:00 只能引用 08:30 快照；09:30 只能引用前两轮快照。

- [ ] **步骤 4：运行测试**

运行：`python -m pytest tests/test_modern_momentum_forward.py tests/test_modern_funnel_stage.py -q`

预期：PASS。

- [ ] **步骤 5：Commit**

```bash
git add research/modern_momentum_forward.py scripts/prepare_modern_momentum_forward.py scripts/run_event_funnel_wave.py tests
git commit -m "feat: persist weighted intraday Top20 and Top10 waves"
```

### 任务 3：单一日程与执行边界

**文件：**
- 修改：`schedule/modern_funnel.py`
- 修改：`scripts/run_modern_funnel_stage.py`
- 创建：`scripts/report_event_funnel_status.py`
- 测试：`tests/test_schedule_modern_funnel.py`

- [ ] **步骤 1：编写失败测试**

```python
assert _stage_for(time(8, 30)) is FunnelStage.FIRST_WAVE
assert _stage_for(time(9, 0)) is FunnelStage.SECOND_WAVE
assert _stage_for(time(9, 30)) is FunnelStage.OPEN_CONFIRMATION
```

- [ ] **步骤 2：运行失败测试**

运行：`python -m pytest tests/test_schedule_modern_funnel.py -q`

预期：旧时刻断言失败。

- [ ] **步骤 3：最小实现**

09:30 启动 Paper 监控，但首笔仍由已回测的 H15/回踩承接完整 K 触发；不把“09:30 到了”当买入信号。每 30 分钟运行只读状态摘要。15:00 后拒绝新仓，15:50 前由已有保护逻辑确认平仓。

- [ ] **步骤 4：运行测试**

运行：`python -m pytest tests/test_schedule_modern_funnel.py tests/test_modern_paper_lifecycle.py -q`

预期：PASS。

- [ ] **步骤 5：Commit**

```bash
git add schedule/modern_funnel.py scripts/run_modern_funnel_stage.py scripts/report_event_funnel_status.py tests
git commit -m "feat: schedule weighted event funnel and status summaries"
```

### 任务 4：盘后复盘与 Loop

**文件：**
- 修改：`operations/loop_integration/review_builder.py`
- 修改：`scripts/sync_loop_daily_review.py`
- 修改：`schedule/postmarket.py`
- 测试：`tests/test_loop_integration.py`

- [ ] **步骤 1：编写失败测试**

```python
def test_no_trade_review_is_submittable_without_claiming_a_plan() -> None:
    envelope = build_review_envelope(...)
    assert envelope.execution_summary["orders_authorized"] is False
    assert envelope.risk_policy["status"] == "no_trade"
```

- [ ] **步骤 2：运行失败测试**

运行：`python -m pytest tests/test_loop_integration.py -q`

预期：旧实现阻断无交易日。

- [ ] **步骤 3：最小实现**

无交易日同步真实三轮候选、触发检查、无成交原因与盘后结果；`no_trade` 明确不可授权订单。Loop 只接收学习数据，绝不反向开仓。复盘推送只用事实字段，不显示“人工干预”，也不伪造自动信号。

- [ ] **步骤 4：运行测试与真实暂存**

运行：`python -m pytest tests/test_loop_integration.py tests/test_loop_daily_provider.py -q`

运行：`python -m scripts.sync_loop_daily_review --trade-date <已结束交易日> --stage-only ...`

预期：测试通过，暂存返回唯一 `event_id`。

- [ ] **步骤 5：Commit**

```bash
git add operations/loop_integration/review_builder.py scripts/sync_loop_daily_review.py schedule/postmarket.py tests
git commit -m "fix: deliver factual no-trade daily reviews to Loop"
```

### 任务 5：上线验收

**文件：**
- 修改：`docs/implementation/2026-09-14-event-v1-status.md`

- [ ] **步骤 1：执行全量相关测试**

运行：`python -m pytest tests/test_intraday_wave_ranking.py tests/test_modern_momentum_forward.py tests/test_modern_funnel_stage.py tests/test_schedule_modern_funnel.py tests/test_modern_paper_lifecycle.py tests/test_loop_integration.py -q`

预期：全通过。

- [ ] **步骤 2：Paper 干跑**

运行：`python -m scripts.run_event_funnel_wave --trade-date <下一交易日> --stage first --check`

预期：只读 API 校验、不会访问 broker 写接口。

- [ ] **步骤 3：部署单一自动化**

使用已有单一 AI 量化自动化更新日程；禁用旧 21:00/21:25/21:35/04:50 重复任务，防止重复对话和重复下单。

- [ ] **步骤 4：Commit**

```bash
git add docs/implementation/2026-09-14-event-v1-status.md
git commit -m "docs: record event funnel rollout checks"
```
