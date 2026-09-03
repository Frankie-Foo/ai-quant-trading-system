# AI 量化交易系统本地联跑手册

## 1. 目的与安全边界

本手册用于研发在本地运行 `ai-quant-trading-system`，完成以下受治理闭环：

1. 生成本地每日盘后复盘；
2. 将 accepted 复盘提交为 Loop Platform `quant_daily_review` Task；
3. 回填 1d、5d、20d 延迟 Outcome；
4. 从 Loop 读取 `strategy_policy_candidate`；
5. 将合法候选安装为本地 Shadow Challenger。

本流程始终保持：

- `PAPER_ONLY`；
- `production_eligible=false`；
- `allow_order_execution=false`；
- Loop 不得调用 Broker；
- Loop 不得修改 `runs/strategy/active.json`；
- Loop 不得绕过本地 P0/P1/P2 风控；
- 首期唯一允许下行的策略参数是 `universe.min_rvol`；
- Challenger 进入 Active 必须经过 Shadow、Paper Canary、明确 policy hash 确认和人工批准。

## 2. 系统关系

```text
行情/选股/执行事实
        │
        ▼
本地 postmarket review
        │ accepted snapshot + provenance
        ▼
本地 Loop Outbox
        │
        ▼
Loop quant_daily_review v6
        │
        ├── Signal Contract 校验
        ├── Top10 强制裁决
        ├── FSM review_completed
        ├── Golden Replay
        └── 复盘/经验/策略候选
                    │
                    ▼
        本地 Shadow challenger.json
                    │
                    ▼
        人工治理流程，禁止自动晋级
```

## 3. 当前约定

| 项目 | 值 |
|---|---|
| Loop Workflow | `quant_daily_review` |
| Workflow Version | `workflow-version-quant-daily-review-v6` |
| Market Scope | `US-equity` |
| Signal Contract | `signal-contract-ai-quant-us-equity-v1` |
| FSM Contract | `fsm-contract-ai-quant-us-equity-v1` |
| FSM Event | `review_completed` |
| Golden Suite | `golden-suite-ai-quant-us-equity-paper-v1` |
| 本地 Outbox | `runs/loop-integration.sqlite3` |
| Active Policy | `runs/strategy/active.json` |
| Shadow Challenger | `runs/strategy/challenger.json` |

生产 Loop 的 API Key 必须通过 Kubernetes Secret 和本地 Secret 文件注入。不得写入本手册、源码、命令历史、工单或 Git。

## 4. 前置条件

研发开始前逐项确认：

- 本机能解析并访问 `aisales-loop-platform-backend`；
- Python 3.12 可用；
- 本地时间和 UTC 时间同步；
- 目标交易日是有效 XNYS 交易日；
- 目标日期已有 accepted 选股复盘快照；
- Loop Runtime Key 已在服务端生效；
- 无 Key 和错误 Key 返回 401，正确 Key 返回 200；
- 三个真实合同已经登记；
- `workflow-version-quant-daily-review-v6` 已在 Loop 激活；
- 所有验证仅使用 Paper 或只读接口。

## 5. 初始化本地环境

### 5.1 进入项目

```bash
cd /Users/leo/Documents/vertu/code_space/ai-quant-trading-system
```

### 5.2 创建 Python 环境

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
```

验证：

```bash
.venv/bin/python --version
.venv/bin/python -m pytest tests/test_loop_integration.py -q
```

### 5.3 本地环境变量

本地 `.env` 必须被 `.gitignore` 忽略，最小配置如下：

```dotenv
AI_QUANT_DATA_ROOT=/absolute/path/to/ai-quant-trading-system/data
AI_QUANT_ACTIVE_POLICY_FILE=/absolute/path/to/ai-quant-trading-system/runs/strategy/active.json
AI_QUANT_LOOP_BINDING_FILE=/absolute/path/to/ai-quant-trading-system/runs/loop-quant-binding.json
LOOP_BASE_URL=https://aisales-v2.vertu.cn/api/v1/loop/machine
LOOP_RUNTIME_API_KEY=<从安全渠道注入>
AI_QUANT_LOOP_SYNC_ENABLED=false
```

三个合同属于控制面配置，不在公网机器路由白名单内。仅在一次性登记合同时，通过受控的
Kubernetes 网络设置：

```bash
LOOP_CONTROL_BASE_URL=http://aisales-loop-platform-backend
```

加载环境：

```bash
set -a
source .env
set +a
```

禁止执行 `cat .env` 或在日志中打印全部环境变量。

### 5.4 创建运行目录

```bash
mkdir -p data/accepted runs/strategy/history runs/autonomous runs/logs
```

### 5.5 Binding 文件

`runs/loop-quant-binding.json`：

```json
{
  "schema_version": "loop_quant_binding.v2",
  "workflow_id": "quant_daily_review",
  "workflow_version_id": "workflow-version-quant-daily-review-v6",
  "market_scope": "US-equity",
  "signal_contract_id": "signal-contract-ai-quant-us-equity-v1",
  "signal_contract_sha256": "<64-character-config-sha256>",
  "fsm_contract_id": "fsm-contract-ai-quant-us-equity-v1",
  "fsm_contract_sha256": "<64-character-config-sha256>",
  "fsm_review_event_type": "review_completed",
  "golden_suite_id": "golden-suite-ai-quant-us-equity-paper-v1",
  "golden_suite_sha256": "<64-character-config-sha256>",
  "golden_actual_results": {
    "paper-only": {
      "verdict": "PAPER_ONLY"
    }
  }
}
```

## 6. 服务端认证验收

```bash
NO_KEY=$(curl -sS -o /dev/null -w '%{http_code}' \
  "$LOOP_BASE_URL/api/v1/tasks?limit=1")

WRONG_KEY=$(curl -sS -o /dev/null -w '%{http_code}' \
  -H 'X-Loop-API-Key: invalid-auth-probe' \
  "$LOOP_BASE_URL/api/v1/tasks?limit=1")

VALID_KEY=$(curl -sS -o /dev/null -w '%{http_code}' \
  -H "X-Loop-API-Key: $LOOP_RUNTIME_API_KEY" \
  "$LOOP_BASE_URL/api/v1/tasks?limit=1")

printf 'no_key=%s wrong_key=%s valid_key=%s\n' \
  "$NO_KEY" "$WRONG_KEY" "$VALID_KEY"
```

唯一允许的结果：

```text
no_key=401 wrong_key=401 valid_key=200
```

不满足时停止联跑，不得临时关闭认证。

## 7. 一次性登记三个合同

合同登记是一次性平台初始化操作。若 ID 已存在，先核对已有合同，不要重复创建随机 ID。

```bash
NOW_UTC=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
```

### 7.1 Signal Contract

```bash
curl --fail-with-body -sS -X POST \
  -H "X-Loop-API-Key: $LOOP_RUNTIME_API_KEY" \
  -H 'Content-Type: application/json' \
  "$LOOP_CONTROL_BASE_URL/api/v1/knowledge/quant/signal-contracts" \
  -d "{
    \"id\": \"signal-contract-ai-quant-us-equity-v1\",
    \"market_scope\": \"US-equity\",
    \"version\": \"v1\",
    \"required_features\": [
      \"close_return\",
      \"mfe_from_previous_close\",
      \"mae_from_previous_close\",
      \"dollar_volume\",
      \"atr_pct\"
    ],
    \"allowed_signal_types\": [\"long\", \"watch\"],
    \"max_signal_age_seconds\": 86400,
    \"top_n\": 10,
    \"status\": \"active\",
    \"mode\": \"PAPER_ONLY\",
    \"effective_at\": \"$NOW_UTC\",
    \"available_at\": \"$NOW_UTC\",
    \"metadata\": {
      \"source_system\": \"ai-quant-trading-system\",
      \"production_eligible\": false,
      \"allow_order_execution\": false
    }
  }" | jq
```

### 7.2 FSM Contract

```bash
curl --fail-with-body -sS -X POST \
  -H "X-Loop-API-Key: $LOOP_RUNTIME_API_KEY" \
  -H 'Content-Type: application/json' \
  "$LOOP_CONTROL_BASE_URL/api/v1/knowledge/quant/fsm-contracts" \
  -d "{
    \"id\": \"fsm-contract-ai-quant-us-equity-v1\",
    \"market_scope\": \"US-equity\",
    \"version\": \"v1\",
    \"initial_state\": \"OBSERVING\",
    \"states\": [\"OBSERVING\"],
    \"transitions\": {
      \"OBSERVING\": {
        \"review_completed\": \"OBSERVING\"
      }
    },
    \"status\": \"active\",
    \"mode\": \"PAPER_ONLY\",
    \"effective_at\": \"$NOW_UTC\",
    \"available_at\": \"$NOW_UTC\",
    \"metadata\": {
      \"source_system\": \"ai-quant-trading-system\",
      \"purpose\": \"daily postmarket review only\",
      \"production_eligible\": false,
      \"allow_order_execution\": false
    }
  }" | jq
```

### 7.3 Golden Suite

```bash
curl --fail-with-body -sS -X POST \
  -H "X-Loop-API-Key: $LOOP_RUNTIME_API_KEY" \
  -H 'Content-Type: application/json' \
  "$LOOP_CONTROL_BASE_URL/api/v1/knowledge/quant/golden-suites" \
  -d "{
    \"id\": \"golden-suite-ai-quant-us-equity-paper-v1\",
    \"market_scope\": \"US-equity\",
    \"name\": \"AI Quant US Equity PAPER-only governance\",
    \"version\": \"v1\",
    \"cases\": [
      {
        \"id\": \"paper-only\",
        \"expected\": {\"verdict\": \"PAPER_ONLY\"}
      }
    ],
    \"status\": \"active\",
    \"mode\": \"PAPER_ONLY\",
    \"effective_at\": \"$NOW_UTC\",
    \"available_at\": \"$NOW_UTC\",
    \"metadata\": {
      \"source_system\": \"ai-quant-trading-system\",
      \"production_eligible\": false,
      \"allow_order_execution\": false
    }
  }" | jq
```

## 8. 初始化本地 Active Policy

仅在 `runs/strategy/active.json` 不存在时执行：

```bash
.venv/bin/python -m scripts.manage_strategy_policy bootstrap \
  --active runs/strategy/active.json \
  --version selection-baseline-v1 \
  --min-rvol 3.0 \
  --approved-by <研发负责人ID>
```

查看状态：

```bash
.venv/bin/python -m scripts.manage_strategy_policy status \
  --active runs/strategy/active.json
```

记录初始 hash：

```bash
ACTIVE_HASH_BEFORE=$(jq -r '.policy_hash' runs/strategy/active.json)
```

## 9. 准备真实复盘数据

检查 accepted 复盘：

```bash
find "$AI_QUANT_DATA_ROOT/accepted" \
  -path '*/research.intraday_selection_postmortem-*/data.parquet' \
  -print
```

选择确实存在数据的交易日：

```bash
TRADE_DATE=YYYY-MM-DD
```

禁止通过复制其他日期、修改 `session_date` 或生成伪数据绕过检查。

## 10. 第一次联跑

### 10.1 Stage-only

```bash
.venv/bin/python -m scripts.sync_loop_daily_review \
  --trade-date "$TRADE_DATE" \
  --data-root "$AI_QUANT_DATA_ROOT" \
  --binding "$AI_QUANT_LOOP_BINDING_FILE" \
  --active-policy "$AI_QUANT_ACTIVE_POLICY_FILE" \
  --outbox runs/loop-integration.sqlite3 \
  --stage-only
```

检查 Outbox：

```bash
sqlite3 runs/loop-integration.sqlite3 \
  'select event_id,event_type,status,attempts,error_code from loop_outbox;'
```

### 10.2 真实提交

```bash
.venv/bin/python -m scripts.sync_loop_daily_review \
  --trade-date "$TRADE_DATE" \
  --data-root "$AI_QUANT_DATA_ROOT" \
  --binding "$AI_QUANT_LOOP_BINDING_FILE" \
  --active-policy "$AI_QUANT_ACTIVE_POLICY_FILE" \
  --outbox runs/loop-integration.sqlite3
```

预期输出包含非空 `task_id` 和 `run_id`。

```bash
sqlite3 runs/loop-integration.sqlite3 \
  'select event_id,status,remote_task_id,remote_run_id,error_code from loop_outbox;'
```

### 10.3 查询 Loop Task 和 Run

```bash
TASK_ID=<上一步返回的task_id>

curl --fail-with-body -sS \
  -H "X-Loop-API-Key: $LOOP_RUNTIME_API_KEY" \
  "$LOOP_BASE_URL/api/v1/tasks/$TASK_ID" | jq
```

验收以下事实：

- Workflow 为 `quant_daily_review` v6；
- 来源为 `ai-quant-trading-system`；
- `synthetic=false`；
- `not_real_market_data=false`；
- Top10 正好十只且排名连续；
- Signal Contract 通过；
- FSM 合同显式执行 `OBSERVING.review_completed -> OBSERVING`，Run Trace 显示
  `state_changed=false`、`transition_kind=self_transition`；这表示盘后事件已被审计接收，
  不表示进入新的交易状态或取得下单授权；
- Golden Replay 通过；
- `orders_submitted=0`；
- Run 失败时没有控制产物和状态副作用。

### 10.4 验证 Active 未被修改

```bash
ACTIVE_HASH_AFTER=$(jq -r '.policy_hash' runs/strategy/active.json)
test "$ACTIVE_HASH_BEFORE" = "$ACTIVE_HASH_AFTER"
```

## 11. 启用每日自动同步

只有第 10 节全部通过后才可启用：

```bash
sed -i.bak \
  's/^AI_QUANT_LOOP_SYNC_ENABLED=false$/AI_QUANT_LOOP_SYNC_ENABLED=true/' \
  .env

grep '^AI_QUANT_LOOP_SYNC_ENABLED=' .env
```

不要打印完整 `.env`。

日终调度入口：

```bash
set -a
source .env
set +a

.venv/bin/python -m schedule.postmarket
```

成功日志必须包含 `loop_review_sync_completed` 和 `orders_submitted=0`。Loop 暂时不可用时，本地复盘仍应完成，同步事件进入 pending/failed 状态。

## 12. 延迟 Outcome 回填

Outcome 必须来自真实可用时间点，并包含：

- `decision_event_id`；
- `source_run_id`；
- `strategy_revision_id`；
- `evaluation_role` 为 `holdout`、`walk_forward` 或 `forward`；
- `point_in_time_guard_passed=true`；
- horizon 为 `1d`、`5d` 或 `20d`。

先暂存：

```bash
.venv/bin/python -m scripts.sync_loop_outcomes \
  --file /absolute/path/to/outcomes.json \
  --outbox runs/loop-integration.sqlite3 \
  --stage-only
```

核对后提交：

```bash
.venv/bin/python -m scripts.sync_loop_outcomes \
  --file /absolute/path/to/outcomes.json \
  --outbox runs/loop-integration.sqlite3
```

当天复盘不得提前填充未来的 1d、5d、20d Outcome。

## 13. 拉取和安装策略候选

### 13.1 查询候选

```bash
curl --fail-with-body -sS \
  -H "X-Loop-API-Key: $LOOP_RUNTIME_API_KEY" \
  "$LOOP_BASE_URL/api/v1/knowledge/quant/control-artifacts?artifact_type=strategy_policy_candidate&market_scope=US-equity&status=candidate&limit=20" \
  | jq
```

研发必须核对：

- `schema_version=quant-strategy-policy-v3`；
- `mode=PAPER_ONLY`；
- `production_eligible=false`；
- `allow_order_execution=false`；
- `trading_policy` 为空；
- 唯一参数为 `universe.min_rvol`；
- 值在本地允许范围 2.0 至 8.0；
- 存在 `strategy_revision_id` 和 `strategy_fingerprint`。

### 13.2 安装 Shadow

```bash
ARTIFACT_ID=<人工确认的候选ID>

.venv/bin/python -m scripts.sync_loop_policy_candidates \
  --market-scope US-equity \
  --artifact-id "$ARTIFACT_ID" \
  --active-policy runs/strategy/active.json \
  --challenger-policy runs/strategy/challenger.json
```

检查：

```bash
.venv/bin/python -m scripts.manage_strategy_policy status \
  --active runs/strategy/active.json \
  --challenger runs/strategy/challenger.json
```

此步骤只能产生 `status=shadow` 的 Challenger，不能自动批准。

## 14. 完整验收清单

### 本地

- [ ] Python 3.12 依赖完整；
- [ ] Loop 集成测试通过；
- [ ] `.env` 和 `runs/` 均未被 Git 跟踪；
- [ ] Active Policy 有批准人、批准时间和有效 hash；
- [ ] accepted 数据对应真实交易日；
- [ ] Outbox WAL/FULL 正常；
- [ ] 重复提交保持幂等；
- [ ] Active hash 在联跑前后不变。

### Loop

- [ ] 认证结果严格为 401/401/200；
- [ ] 三个合同 ID 与 Binding 一致；
- [ ] Workflow v6 已激活；
- [ ] Task/Run ID 已记录；
- [ ] Signal、Top10、FSM、Golden 全部通过；
- [ ] provenance 与实际来源一致；
- [ ] 失败 Run 的领域数据零副作用；
- [ ] 所有记录为 PAPER_ONLY；
- [ ] `orders_submitted=0`。

### 候选反哺

- [ ] 仅接收 `strategy_policy_candidate`；
- [ ] 仅修改 `universe.min_rvol`；
- [ ] 只生成 Shadow Challenger；
- [ ] 不修改 trading policy；
- [ ] 不修改 Active；
- [ ] 不调用 Broker；
- [ ] 后续晋级保留人工批准和回滚证据。

## 15. 停用与回滚

### 15.1 立即停止自动上传

```bash
sed -i.bak \
  's/^AI_QUANT_LOOP_SYNC_ENABLED=true$/AI_QUANT_LOOP_SYNC_ENABLED=false/' \
  .env
```

本地复盘和交易逻辑不受影响。

### 15.2 移除未批准 Challenger

不要直接删除审计证据。先记录候选 ID、policy hash 和原因，再将文件移动到隔离目录：

```bash
mkdir -p runs/strategy/rejected
mv runs/strategy/challenger.json \
  "runs/strategy/rejected/challenger-$(date -u '+%Y%m%dT%H%M%SZ').json"
```

### 15.3 Outbox 重试原则

- 网络故障：使用同一事件重新运行同步命令；
- 401：检查 Secret 和环境加载，不得跳过认证；
- 409/身份冲突：比较 event identity 与 payload hash，禁止生成新身份掩盖冲突；
- 422：修复合同或数据，不得降低合同门槛；
- 5xx：保留 pending/failed 记录，待 Loop 恢复后重试；
- 已成功的事件不得用不同内容重复提交。

## 16. 常见故障

| 现象 | 检查 | 处理 |
|---|---|---|
| `No module named pydantic/polars` | Python/venv | 用 Python 3.12 重新安装 `requirements.txt` |
| `401` | Key、Secret、环境加载 | 验证 Secret 引用和 401/401/200，不关闭认证 |
| 找不到 accepted snapshot | 日期、数据根目录 | 先完成真实盘后复盘，不伪造文件 |
| Top10 校验失败 | 候选数量和排名 | 确保十只唯一标的、排名 1..10 |
| Signal 缺字段 | Signal Contract | 修复上游复盘字段，不降低合同 |
| FSM transition invalid | Binding/FSM | 确认绑定事件为 `review_completed`，且冻结合同在当前状态显式声明该事件；不要为通过校验而虚构新状态 |
| Golden Replay 失败 | actual/expected | 保持 PAPER_ONLY，不修改预期绕过失败 |
| 候选被拒绝 | 参数白名单 | 只允许 `universe.min_rvol` |
| 已有 Challenger 冲突 | 本地文件 | 完成现有候选评审后再安装新候选 |
| Loop 不可用 | Outbox | 保留本地复盘成功和待重试事件 |

## 17. 研发交付证据

联跑完成后提交以下脱敏证据：

1. Git commit、配置 hash、active policy hash；
2. 目标交易日和 accepted snapshot IDs；
3. 认证 401/401/200 状态码；
4. Signal/FSM/Golden 合同 ID 与版本；
5. 本地 event ID、Loop task ID、run ID；
6. Run 各步骤状态与 Golden Replay 结果；
7. Outbox 状态；
8. `orders_submitted=0`；
9. 联跑前后 Active hash 一致；
10. 如有 Challenger，记录 artifact ID、revision、fingerprint、min_rvol 和 Shadow hash；
11. 失败场景的零副作用或回滚证据；
12. 未包含 API Key、券商凭证、数据库密码和完整连接串的脱敏日志。
