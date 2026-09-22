# M1 事件账本、动态样本与新闻入口：交付及待确认测试流程

状态：代码已写入独立工作树，仅作静态阅读；未编写测试，未执行测试、导入、编译、格式化、类型检查、demo、smoke 或接口联调。本文件是流程文档，不是测试代码或通过证据。用户确认前到此停止。

工作树：`D:/cdoeX-worktrees/ai-quant-event-data`；分支：`codex/event-data-m1-20260914`。
公共接口依据主树只读文档 `docs/implementation/2026-09-14-m1-test-plan.md`，不改该文档或主树。

## 修改文件与 API

| 文件 | 职责/API |
| --- | --- |
| `data_plane/contracts.py` | 追加冻结的 `EventObservation`、`EventRevision`；不改变旧契约 |
| `data_plane/event_ledger.py`（新增） | `EventLedger(path, clock=...)`；`append(observation)`、`append_many(observations)`、`revisions()`、`as_of(at, forward_only=True)`、`close()`、上下文管理 |
| `data_plane/providers/catalyst_news.py` | 追加 `ingest_catalyst_events(frame, *, ledger, origin)`；旧 `fetch_alpaca_news_direct` 增加可选 keyword-only `client`，避免入口隐式读取环境凭据 |
| `research/event_cohort.py` | 追加 `build_dynamic_event_cohort(ledger, *, asof_utc)`；旧 `build_event_cohort` 不改行为 |
| `scripts/ingest_event_news.py`（新增） | 显式只读新闻生产调用入口，无自动执行、调度或部署 |
| 本文（新增） | 设计取舍、调用约束、后续测试流程及未验证项 |

`append` 返回 `EventRevision`；`append_many` 按输入顺序返回版本 tuple（可能包含幂等返回的既有版本）；`revisions` 和 `as_of` 返回稳定排序的版本 tuple。`ingest_catalyst_events` 返回整批版本 tuple。动态样本返回 Polars DataFrame，空表仍保留 schema。

## 版本、首见与来源规则

1. `event_id` 由 source/source_event_id 稳定生成。正文与来源证据保存在不可变 observation 中；保留 canonical 的 publisher、provenance、source_type、URL、tags、form_items 等字段。
2. 幂等指纹包含全部契约字段，但排除 `first_seen_at`。symbols 做去空格、大写、排序、去重；文本本身不改写。相同 source identity、文本、provider updated、origin 等实质证据重复采集，直接返回原版本，不更新首次首见、录入、可用时间或资格；首次首见缺失也不因后续重复采集而补造。
3. 文本或 provider updated 等实质字段变化才追加 revision。文本回到旧值且 provider updated 等字段也完全相同，视作旧版本重抓；若 provider updated 改变则仍追加。重抓旧版本不把它重新设为最新版本。
4. 同一 ledger 中事件的首次 origin 固定。historical/manual/forward 之间任意切换均拒绝，整批回滚。本轮没有来源转换或自动创建新数据集能力；不能把历史或人工记录洗成前向证据。
5. `recorded_at` 仅取 ledger clock；observation 不接受该字段。`updated_at < published_at` 拒绝；不强加首见与 provider updated 的猜测性因果顺序。新写入时钟早于数据库已有录入时间会拒绝，不钳制或伪造时钟。`available_at` 为 published/updated/first_seen/recorded 及该事件上一版 available 的最大值；首见空值保持为空，内容不可能在 updated 之前可用。
6. `forward_eligible` 仅在 origin=forward 且该版本具有首见证据时为真。`as_of` 先选当时已可用的最新 revision，再做 forward 过滤，不能因最新版受限而偷偷退回旧版。未来修订不覆盖上午回放。
7. 转载仅按完全相同的标准化 symbol 集合、headline/summary 的精确空白归一文本、UTC 发布日期共享 cluster。保留大小写、标点及标题/摘要边界，不做语义匹配；空文本不跨来源合并。不同日、不同文本或不同标的不合并。多标的集合部分重叠保守不合并，可能漏去重，不冒险错融合。
8. 当前 Alpaca provider 请求 `include_content=false`，只提供 headline/summary，不是完整新闻正文。账本完整保存的是传入 canonical 可得文本与来源字段，不恢复 provider 未提供或上游已去除的内容。`body_hash` 是上述标题/摘要精确归一文本的 hash。

## SQLite 与研究样本边界

所有批次先完成 Pydantic 结构校验，再进入显式 `BEGIN IMMEDIATE` / `COMMIT`，任何期间异常 `ROLLBACK`。唯一键、顺序插入与不可更新/删除触发器保护版本；值查询均参数化。连接内用锁串行，跨连接/进程由 SQLite 写锁串行，等待上限 30 秒；WAL、FULL 同步。重启读取同一数据库，不覆盖旧数据。直接使用 ledger 时父目录由调用者准备；CLI 在全部 fetch 成功后调用 `mkdir(parents=True, exist_ok=True)` 创建指定 ledger 父目录。本轮没有创建或运行数据库。

这些是应用级追加保护，不是抵抗持有数据库文件权限者删除文件、删除触发器或篡改 schema 的安全边界。没有外部备份或加密防篡改功能。按 ponytail 使用标准库和既有依赖，不新增存储框架。

动态表展开当时已知全部 event/symbol，包括历史、人工、无首见以及盘中新标的；不依赖盘前十只或盘后赢家。转载保留各来源行，共享 cluster；研究计数应使用唯一 `(event_cluster_id, symbol)`，也提供 `is_cluster_representative` 辅助去重。该标记针对完整研究表；再次按 origin/forward 过滤后应重新分组，不可直接沿用代表行作为前向样本选择。

每行包含 event/revision/cluster、文本/来源、所有证据时间、asof、origin、forward_eligible、coverage、missing_reason。`market_cap=null`、`market_cap_status=missing`、`tradable=false`、`outcome_label=null`；不声明已满足市值门槛或执行条件。coverage 为 partial 或 research_only，missing_reason 明示市值、未计算标签、未评估交易检查及来源/首见限制。

当前动态表是账本覆盖的累计 as-of 研究观察表，不是完整市场宇宙，也不做会话过期、收益标签、历史市值 join 或实际下单。`storage.py`、旧 snapshot、依赖、券商和调度配置均未改。

## 显式 API 入口约束

后续调用入口为 `scripts.ingest_event_news` 模块，本轮没有运行，包括没有运行帮助命令。

- 必填：`--env-file`、`--ledger`、`--symbols`（空格分隔）；无默认 Gary 路径、不自动发现 `.env`。symbols 来自用户或上层发现宇宙，不默认盘前十只；全市场扫描尚未接通。现有 provider 只保留请求范围内的关联 symbols。
- 默认 `--origin historical`，要求 `--start-utc` 和 `--end-utc` 为带时区时间，start < end <= 当前时间。回查得到的是 provider 当前可返回的版本，不是对历史所有修订的重建。
- `--origin forward` 必须显式给出；拒绝 start/end 覆盖，仅以当前采集时间结束、向前 `--lookback-minutes`（默认 15，允许 1–60）采集。首见来自实际请求后形成的 retrieved 时间，不能通过 CLI 注入。发布窗口轮询可能漏掉窗口外旧文章的新修订，不声明覆盖全部修订流。
- 凭据读取前以及每次 HTTP 请求（含分页、重试）检查北京时间跨日窗口：21:00（含）至次日 06:00（不含）。00:00–05:59 允许，06:00–20:59 拒绝；不继承 legacy `local_env` 午夜断流问题。没有新增日间授权或自动等待机制。
- 使用既有 `dotenv_values(..., interpolate=False)` 显式读取文件，仅使用其中成对 `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` 及可选 `ALPACA_DATA_URL`；不从其他别名/进程环境补一半凭据，不调用 `load_project_env`、不修改 `os.environ`、不提升 Paper 凭据，不使用 Finnhub 或 Alpha Vantage。
- 可选 `--data-url` 覆盖仅接受 `https://data.alpaca.markets`。HTTP 请求钩子只允许该主机的 GET `/v1beta1/news`；禁止交易主机、非 HTTPS、跳转和环境代理。
- `fetch_alpaca_news_direct(..., client=...)` 接受调用者拥有的既有 client，函数不关闭它；省略 client 的旧调用方式仍自建和关闭 client。分块内相同文章不同文本/updated 不混入同一个早期 retrieved 时间；合并 symbol 集合时使用该合并完成时的真实观测时间。
- 全部请求批次成功后才打开 ledger，由适配器原子写入。网络、契约、时窗、数据库错误输出 `status=failed`、异常类型且退出 1；不输出密钥或异常正文，不伪装为零消息。真实成功空响应单列 `empty_response`。`observations` 是返回条数，不宣称全是新增版本。

## 测试流程：待用户再次授权后才编写与执行

以下只规定输入场景和预期，不附测试实现，不执行任何命令。

1. **冻结契约与持久性。** 在隔离临时目录、固定 UTC 时钟下写一版，关闭重开后逐字段读取；尝试修改 revision、observation 与 tuple 失败。首次写入/空批/空表/重复 close/关闭后使用分别确认明确行为。
2. **幂等与来源锁。** 同源同内容同 updated 不同 retrieved 重复导入，版本数和首次时间不变；首见从空变有仍返回空首见旧版。文本变、updated 变分别新增。旧 payload 重抓不恢复为最新。historical/manual 转 forward 以及反向切换拒绝；批次已有候选写入时也全批回滚。
3. **时间与回放。** 09:00 发布、10:10 首见、10:12 实际录入，10:00 和 10:11 不可见、10:12 可见；14:00 修订不改变上午回放。future published/updated/first_seen 推迟 available；updated 早于 published 拒绝，first_seen 早于 updated 时仍不允许在 updated 前可用；时钟回退拒绝；naive 时间、空白来源/标的、伪造 recorded_at 拒绝。新受限修订可用后 forward 查询不得回退旧合格版本。
4. **保守聚类与来源保留。** 同标的、同 UTC 发布日、精确归一标题/摘要跨来源共享 cluster 且来源记录均保留；不同日、不同文字/标点/大小写、不同 symbol 集合及空文本不误合并。明确部分重叠多标的集合的漏去重限制。
5. **原子性及并发。** 批次最后一条结构错误、origin 冲突、时钟异常、插入异常分别无半批；两个连接/进程同时提交相同和不同 payload，检查唯一性、连续版本号及锁超时明确失败；重启继续追加。直接 UPDATE/DELETE/REPLACE 被拒绝，恶意字符串仅作数据。
6. **动态样本。** 上午事件、盘中新 symbol、历史/人工/无首见均按真实 available 入表；不输入任何收益排名，确保无赢家过滤和十只上限；市值、标签保持空值、tradable=false。转载唯一 cluster/symbol 计数正确，空/非空 schema 一致，重复 as-of 顺序稳定。
7. **适配器与旧接口回归。** canonical 所有来源字段保留，retrieved 仅映射首见，首见缺失不补造；全部转换成功才提交事务。旧 snapshot、旧 cohort、旧 provider 调用默认行为保留；自建 client 必须关闭，注入 client 不被函数关闭。跨 chunk 文本变化不得借用更早观测时刻。
8. **入口离线边界。** 仅在以后授权的离线测试中替换 HTTP transport/时钟，覆盖北京时间 20:59、21:00、23:59、00:00、05:59、06:00、跨午夜分页与 06:00 分页失败；时窗拒绝时不得读凭据。检查配对 key 缺失、Paper-only 文件、恶意 URL/重定向/代理、非法 symbols、historical 默认、forward 时间覆盖拒绝，且从不访问账户或订单接口。用虚构测试凭据，不读 Gary 文件。
9. **失败与输出。** 分页失败、provider 契约错误、真实空响应分开；请求失败不创建 ledger 或其父目录，成功 fetch 后允许创建此前不存在的父目录；持久失败不留下半批；stdout/stderr 无凭据或新闻正文；成功条数不可误作新增版本数。
10. **后续检查与联调分离。** 用户授权后才编写和运行新接口测试、原 event_cohort/catalyst/provider 回归及 Ruff/mypy。真实新闻 API 联调仍需单独授权，在 21:00–次日06:00 窗口使用显式文件、只读新闻端点与隔离 ledger；不因为离线测试获准就联网或启用调度。

未来执行者记录实际命令、退出码、真实红绿结果及证据位置。当前全部未验证：Python 运行/导入、Polars/Pydantic 兼容、SQLite 重启和并发、API权限/网络、回归与静态工具。没有通过声明、盈利证明、全市场覆盖或部署验收。
