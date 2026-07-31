import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Activity,
  AlertTriangle,
  ArrowDownRight,
  ArrowUpRight,
  BarChart3,
  BellRing,
  Bot,
  BrainCircuit,
  BriefcaseBusiness,
  CheckCircle2,
  Clock3,
  Database,
  Gauge,
  LayoutDashboard,
  Power,
  Radar,
  RefreshCw,
  Settings,
  ShieldCheck,
  Target,
  Waves,
} from 'lucide-react'

const PAGES = [
  { id: 'today', label: '今日', icon: LayoutDashboard },
  { id: 'opportunities', label: '机会', icon: Radar },
  { id: 'positions', label: '持仓', icon: BriefcaseBusiness },
  { id: 'review', label: '复盘', icon: BrainCircuit },
  { id: 'agents', label: 'Agent', icon: Bot },
  { id: 'system', label: '系统', icon: Settings },
]

const STATE_LABELS = {
  watching: '观察',
  armed: '信号武装',
  entry_ready: '允许侦察仓',
  holding: '持有',
  add_allowed: '允许加仓',
  reduce_required: '需要减仓',
  exit_required: '立即退出',
  abandoned: '今日放弃',
  closed: '已关闭',
}

const ACTION_LABELS = {
  no_action: '无新动作',
  arm_entry: '等待第二次确认',
  enter_probe: '可以买入侦察仓',
  allow_add: '允许加仓',
  reduce: '触发减仓',
  tighten_stop: '保护位上移',
  exit_now: '立即退出',
  abandon: '放弃买入',
}

const REASON_LABELS = {
  first_completed_minute_confirmation: '第一根完整分钟线确认',
  two_completed_minute_confirmations: '连续两根完整分钟线确认',
  entry_confluence_passed: '市场、趋势、量价与策略路线共振',
  force_exit_time_reached: '日内强制退出时间已到',
  protective_stop_reached: '实时买一价触及保护位',
  first_target_filled: '第一档止盈已完成',
  structural_stop_tightened: '结构保护位只向上调整',
  entry_window_expired: '允许开仓时间已经结束',
  vwap_lost: '跌破盘中 VWAP',
  five_minute_structure_failed: '五分钟结构转弱',
  holding_confluence_reconfirmed: '持仓后的多周期与订单流再次共振',
  risk_capacity_available: '不可变风险上限内仍有剩余容量',
}

const BLOCKER_LABELS = {
  market_data_incomplete: '实时数据或多周期指标不完整',
  quote_stale: '报价陈旧',
  spread_too_wide: '买卖价差过宽',
  market_risk_off: '市场处于风险规避状态',
  benchmark_below_vwap: '基准指数位于 VWAP 下方',
  sector_below_vwap: '行业弱于盘中均价',
  below_session_vwap: '个股位于盘中 VWAP 下方',
  fifteen_minute_trend_not_confirmed: '15 分钟趋势未确认',
  five_minute_structure_not_confirmed: '5 分钟结构未确认',
  one_minute_trigger_not_confirmed: '1 分钟触发未确认',
  green_volume_not_confirmed: '放量阳线未确认',
  relative_strength_not_confirmed: '相对强度不足',
  catalyst_unavailable: '催化剂证据不可用',
  catalyst_below_threshold: '催化剂强度不足',
  order_flow_unavailable: '订单流证据不可用',
  order_flow_below_threshold: '主动买盘不足',
  soft_revision_cooldown_active: '普通调整仍在冷却期',
  soft_revision_limit_reached: '今日普通调整次数已达上限',
  bar_time_regressed: '分钟线时间发生回退',
  risk_capacity_exhausted: '风险或名义仓位容量已经用尽',
  awaiting_position_increase_after_add_signal: '上一条加仓建议尚未在券商持仓中确认',
  first_target_already_filled: '完成第一档止盈后不再发出加仓建议',
}

const SELECTION_STATUS_LABELS = {
  ready: '今日锁池完成',
  waiting: '等待今日锁池',
  blocked: '今日锁池受阻',
  missing: '今日锁池缺失',
}

const JOB_LABELS = {
  premarket_catalyst_lock: '盘前催化剂锁池',
  premarket_final_selection: '盘前最终选股',
  premarket_multisignal_shadow: '多信号影子管线',
  postmarket_review: '盘后自动复盘',
}

const JOB_STATUS_LABELS = {
  pending: '待执行',
  running: '执行中',
  succeeded: '成功',
  failed: '失败',
}

const PIPELINE_STATUS_LABELS = {
  ready: '就绪',
  waiting: '等待数据',
  degraded: '降级',
}

const STAGE_LABELS = {
  research_only: '仅研究',
}

const REVIEW_STATUS_LABELS = {
  selected: '已入选',
  rejected: '硬闸拒绝',
  not_seen: '未进入候选池',
}

const REVIEW_CAUSE_LABELS = {
  selected: '已捕获机会',
  intentional_gate: '硬闸主动放弃',
  late_catalyst: '盘中新催化，盘前不可知',
  data_or_classifier_gap: '新闻抓取或分类缺口',
  factor_gap: '技术、订单流或行业因子缺口',
  incomplete_evidence: '证据不完整，暂不归因',
}

const AGENT_LABELS = {
  catalyst: '催化剂分析 Agent',
  red_team: '红队 Agent',
  supervisor: '确定性监督器',
}

const AGENT_STATUS_LABELS = {
  healthy: '证据当前且健康',
  blocked: '发现重大负面',
  unhealthy: '故障保护',
  stale_or_invalid: '证据陈旧或无效',
  unavailable: '尚未运行',
}

const percent = (value, digits = 2) => (
  value == null ? 'N/A' : `${value >= 0 ? '+' : ''}${(value * 100).toFixed(digits)}%`
)

const number = (value, digits = 2) => (
  value == null ? 'N/A' : Number(value).toFixed(digits)
)

const localTime = (value) => {
  if (!value) return 'N/A'
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(new Date(value))
}

function StatusDot({ ok }) {
  return <span className={`status-dot ${ok ? 'ok' : 'bad'}`} />
}

function Metric({ icon: Icon, label, value, detail, tone = '' }) {
  return (
    <article className={`metric ${tone}`}>
      <span className="metric-icon"><Icon size={18} /></span>
      <div>
        <small>{label}</small>
        <strong>{value}</strong>
        <span>{detail}</span>
      </div>
    </article>
  )
}

function Factor({ label, passed, value }) {
  return (
    <div className="factor">
      <span className={`factor-mark ${passed ? 'pass' : 'wait'}`}>
        {passed ? <CheckCircle2 size={14} /> : <Clock3 size={14} />}
      </span>
      <div><small>{label}</small><strong>{value}</strong></div>
    </div>
  )
}

function PlanCard({ plan, selected, onSelect }) {
  const baseline = plan.baseline
  const runtime = plan.runtime
  const evaluation = plan.latest_evaluation || plan.latest_decision
  const action = evaluation?.action || 'no_action'
  const danger = ['exit_required', 'reduce_required', 'abandoned'].includes(runtime.state)
  return (
    <button
      className={`plan-card ${selected ? 'selected' : ''}`}
      type="button"
      onClick={onSelect}
    >
      <div className="plan-symbol">
        <span>{plan.symbol.slice(0, 1)}</span>
        <div><strong>{plan.symbol}</strong><small>{baseline.mode === 'factor' ? '纯因子路线' : '催化剂路线'}</small></div>
      </div>
      <span className={`state-pill ${danger ? 'danger' : runtime.state === 'entry_ready' ? 'ready' : ''}`}>
        {STATE_LABELS[runtime.state] || runtime.state}
      </span>
      <div className="plan-action">{ACTION_LABELS[action] || action}</div>
      <div className="plan-meta">
        <span>修订 {runtime.revision}</span>
        <span>更新 {localTime(plan.updated_at_utc)}</span>
      </div>
    </button>
  )
}

function Detail({ plan }) {
  if (!plan) {
    return <section className="empty"><Target size={34} /><h2>暂无交易预案</h2><p>注册当日风险基线后，实时状态会自动显示在这里。</p></section>
  }
  const baseline = plan.baseline
  const runtime = plan.runtime
  const evaluation = plan.latest_evaluation || plan.latest_decision
  const facts = evaluation?.facts || {}
  const hasEvaluation = Boolean(evaluation?.facts)
  const reasons = evaluation?.reasons || []
  const blockers = evaluation?.blockers || []
  const priceVsVwap = facts.last_price != null && facts.session_vwap != null
    ? facts.last_price / facts.session_vwap - 1
    : null
  return (
    <div className="detail-stack">
      <section className="panel hero">
        <div>
          <span className="eyebrow">ADAPTIVE PLAN · {baseline.mode?.toUpperCase()}</span>
          <h2>{plan.symbol} · {STATE_LABELS[runtime.state] || runtime.state}</h2>
          <p>
            {ACTION_LABELS[evaluation?.action || 'no_action']}
            {evaluation?.suggested_shares ? ` · 风险约束建议 ${evaluation.suggested_shares} 股` : ''}
          </p>
        </div>
        <div className="hero-price">
          <small>实时中间价</small>
          <strong>{number(facts.last_price)}</strong>
          <span className={priceVsVwap >= 0 ? 'positive' : 'negative'}>
            VWAP {percent(priceVsVwap)}
          </span>
        </div>
      </section>

      <div className="metric-grid">
        <Metric icon={Gauge} label="相对市场强度" value={percent(facts.relative_strength)} detail="个股减基准日内收益" tone={facts.relative_strength > 0 ? 'green' : ''} />
        <Metric icon={BarChart3} label="放量阳线" value={facts.green_volume_ratio == null ? 'N/A' : `${number(facts.green_volume_ratio)}×`} detail="最近完整分钟线" tone={facts.green_volume_ratio >= 1.5 ? 'green' : ''} />
        <Metric icon={Waves} label="订单流失衡" value={percent(facts.order_flow_imbalance)} detail="五分钟 Tick Rule" tone={facts.order_flow_imbalance >= 0.2 ? 'green' : ''} />
        <Metric icon={Activity} label="催化剂强度" value={facts.catalyst_score == null ? 'N/A' : number(facts.catalyst_score)} detail={baseline.mode === 'factor' ? '因子路线不强制' : '冻结的入选证据'} />
      </div>

      <section className="panel">
        <header className="panel-header">
          <div><span className="eyebrow">SIGNAL CONFLUENCE</span><h3>信号共振与阻断项</h3></div>
          <span className="timestamp">完整分钟线 {localTime(facts.completed_one_minute_bar_utc)}</span>
        </header>
        <div className="factor-grid">
          <Factor label="1 分钟触发" passed={hasEvaluation && facts.one_minute_trigger} value={!hasEvaluation ? 'N/A' : facts.one_minute_trigger ? '已确认' : '等待'} />
          <Factor label="5 分钟结构" passed={hasEvaluation && facts.five_minute_confirmed} value={!hasEvaluation ? 'N/A' : facts.five_minute_confirmed ? '向上' : '未确认'} />
          <Factor label="15 分钟趋势" passed={hasEvaluation && facts.fifteen_minute_confirmed} value={!hasEvaluation ? 'N/A' : facts.fifteen_minute_confirmed ? '向上' : '未确认'} />
          <Factor label="市场环境" passed={hasEvaluation && !facts.market_risk_off} value={!hasEvaluation ? 'N/A' : facts.market_risk_off ? 'Risk-off' : '允许做多'} />
          <Factor label="基准 VWAP" passed={hasEvaluation && facts.benchmark_above_vwap} value={!hasEvaluation ? 'N/A' : facts.benchmark_above_vwap ? '上方' : '下方'} />
          <Factor label="行业 VWAP" passed={hasEvaluation && facts.sector_above_vwap} value={!hasEvaluation ? 'N/A' : facts.sector_above_vwap ? '上方' : '下方'} />
        </div>
        <div className="explain-grid">
          <div>
            <h4>本次依据</h4>
            {reasons.length ? reasons.map((item) => <p key={item} className="reason">✓ {REASON_LABELS[item] || item}</p>) : <p className="muted">没有新的实质性动作。</p>}
          </div>
          <div>
            <h4>当前阻断</h4>
            {!hasEvaluation ? <p className="muted">等待首个实时评估，当前不能视为条件通过。</p> : blockers.length ? blockers.map((item) => <p key={item} className="blocker">• {BLOCKER_LABELS[item] || item}</p>) : <p className="positive">全部必要条件通过</p>}
          </div>
        </div>
      </section>

      <section className="panel">
        <header className="panel-header">
          <div><span className="eyebrow">IMMUTABLE RISK ENVELOPE</span><h3>不可放宽的风险基线</h3></div>
          <ShieldCheck size={20} className="shield" />
        </header>
        <div className="risk-grid">
          <div><small>硬保护位</small><strong>{number(runtime.protective_stop)}</strong><span>初始 {number(baseline.hard_stop)}</span></div>
          <div><small>单笔最大风险</small><strong>{number(baseline.max_risk_dollars, 0)}</strong><span>运行中不可上调</span></div>
          <div><small>最大名义仓位</small><strong>{number(baseline.max_notional, 0)}</strong><span>运行中不可上调</span></div>
          <div><small>侦察仓比例</small><strong>{percent(baseline.probe_fraction, 0)}</strong><span>按剩余风险容量计算整股</span></div>
          <div><small>普通修订</small><strong>{runtime.soft_revision_count}/{baseline.max_soft_revisions}</strong><span>带冷却和次数限制</span></div>
          <div><small>停止开仓</small><strong>{localTime(baseline.entry_window_end_utc)}</strong><span>北京时间</span></div>
          <div><small>强制退出</small><strong>{localTime(baseline.force_exit_utc)}</strong><span>绝不过夜</span></div>
        </div>
      </section>
    </div>
  )
}

function PageHeader({ eyebrow, title, detail }) {
  return (
    <header className="page-header">
      <div><span className="eyebrow">{eyebrow}</span><h2>{title}</h2></div>
      <p>{detail}</p>
    </header>
  )
}

function CandidateTable({ candidates, compact = false }) {
  if (!candidates.length) {
    return <p className="table-empty">当前没有可验证的硬闸通过名单。</p>
  }
  return (
    <div className={`candidate-table ${compact ? 'compact' : ''}`}>
      <div className="candidate-row candidate-head">
        <span>排名 / 标的</span><span>盘前缺口</span><span>RVOL</span>
        <span>财报证据</span><span>盘前位置</span>
      </div>
      {candidates.map((candidate) => (
        <div className="candidate-row" key={candidate.symbol}>
          <span><b>{candidate.rank ?? '—'}</b><strong>{candidate.symbol}</strong></span>
          <span className={candidate.premarket_gap_return > 0 ? 'positive' : ''}>
            {percent(candidate.premarket_gap_return)}
          </span>
          <span>{number(candidate.rvol, 1)}×</span>
          <span>
            {candidate.earnings_strength_confirmed ? '强确认' : `${candidate.earnings_evidence_layers ?? 0} 层`}
            <small> 强度 {number(candidate.earnings_intensity_score, 0)}</small>
          </span>
          <span>
            {candidate.premarket_above_vwap ? 'VWAP 上方' : 'VWAP 下方'}
            <small> 收盘位 {percent(candidate.premarket_close_location, 0)}</small>
          </span>
        </div>
      ))}
    </div>
  )
}

function SelectionOverview({ desk }) {
  const selection = desk?.selection
  const candidates = selection?.candidates || []
  const top = candidates[0]
  const blocked = selection?.status === 'blocked' || selection?.status === 'missing'
  return (
    <div className="detail-stack">
      <section className={`panel selection-hero ${blocked ? 'danger-panel' : ''}`}>
        <div>
          <span className="eyebrow">POINT-IN-TIME SELECTION</span>
          <h2>{SELECTION_STATUS_LABELS[selection?.status] || '正在读取选股证据'}</h2>
          <p>
            目标交易日 {selection?.target_trade_date || 'N/A'}
            {selection?.session_date ? ` · 当前展示 ${selection.session_date}` : ''}
            {selection?.stale ? ' · 历史快照，仅供追溯' : ''}
          </p>
        </div>
        <div className="selection-badge">
          <strong>{selection?.pass_count ?? 0}</strong>
          <span>硬闸通过</span>
        </div>
      </section>
      {selection?.blocker && (
        <div className="evidence-warning">
          <AlertTriangle size={16} />
          今日流程失败：{selection.blocker}。客户端不会把昨日名单冒充成今日结果。
        </div>
      )}
      <div className="metric-grid">
        <Metric icon={Target} label="硬闸第一名" value={top?.symbol || 'N/A'} detail="官方排名仅由冻结规则产生" tone={top ? 'green' : ''} />
        <Metric icon={Gauge} label="第一名 RVOL" value={top ? `${number(top.rvol, 1)}×` : 'N/A'} detail="相对历史同窗口盘前量" />
        <Metric icon={ArrowUpRight} label="第一名盘前缺口" value={percent(top?.premarket_gap_return)} detail="相对前收，不代表买入许可" />
        <Metric icon={Database} label="证据时间" value={localTime(selection?.asof_utc)} detail={selection?.snapshot_id || '无可用快照'} />
      </div>
      <section className="panel">
        <header className="panel-header">
          <div><span className="eyebrow">DETERMINISTIC GATE PASSERS</span><h3>盘前硬闸通过名单</h3></div>
          <span className="timestamp">只读 · 不自动生成订单</span>
        </header>
        <CandidateTable candidates={candidates} />
      </section>
    </div>
  )
}

function OpportunitiesPage({ desk, plans, onOpen }) {
  const candidates = desk?.selection?.candidates || []
  const plansBySymbol = new Map(plans.map((plan) => [plan.symbol, plan]))
  return (
    <div className="page-stack">
      <PageHeader eyebrow="LIVE OPPORTUNITY QUEUE" title="候选机会" detail="这里先展示不可变选股快照；只有另行注册了风险基线并获得实时确认，才会进入动态预案。" />
      {desk?.selection?.stale && <div className="evidence-warning"><AlertTriangle size={16} />当前候选属于 {desk.selection.session_date}，不是 {desk.selection.target_trade_date} 的可执行名单。</div>}
      <div className="wide-card-grid">
        {candidates.length ? candidates.map((candidate) => {
          const plan = plansBySymbol.get(candidate.symbol)
          const evaluation = plan?.latest_evaluation || plan?.latest_decision
          return (
            <button className="opportunity-card" type="button" key={candidate.symbol} disabled={!plan} onClick={() => plan && onOpen(plan.plan_id)}>
              <div><strong>{candidate.symbol}</strong><span>催化剂硬闸 #{candidate.rank}</span></div>
              <b>{plan ? (STATE_LABELS[plan.runtime.state] || plan.runtime.state) : '尚未生成动态预案'}</b>
              <p>缺口 {percent(candidate.premarket_gap_return)} · RVOL {number(candidate.rvol, 1)}×</p>
              <small>
                {plan
                  ? `${(evaluation?.blockers || []).length} 个实时阻断项 · ${localTime(plan.updated_at_utc)}`
                  : '只有冻结选股证据；没有入场许可'}
              </small>
            </button>
          )
        }) : <section className="empty compact"><Radar size={28} /><h2>当前没有可验证候选</h2><p>等待锁池或处理数据故障；不会回退到猜测名单。</p></section>}
      </div>
    </div>
  )
}

function PositionsPage({ plans }) {
  const active = plans.filter((plan) => ['holding', 'add_allowed', 'reduce_required', 'exit_required'].includes(plan.runtime.state))
  return (
    <div className="page-stack">
      <PageHeader eyebrow="PAPER POSITION CONTROL" title="模拟盘持仓与保护" detail="客户端不提供手工买卖；止损、减仓、尾仓和 13:00 ET 清仓由确定性执行层管理。" />
      <section className="panel table-panel">
        <div className="table-row table-head"><span>标的</span><span>状态</span><span>保护位</span><span>尾仓规则</span><span>更新时间</span></div>
        {active.length ? active.map((plan) => (
          <div className="table-row" key={plan.plan_id}>
            <strong>{plan.symbol}</strong>
            <span>{STATE_LABELS[plan.runtime.state]}</span>
            <span>{number(plan.runtime.protective_stop)}</span>
            <span>标准 20% · 强右尾 25% · A++ 30%</span>
            <span>{localTime(plan.updated_at_utc)}</span>
          </div>
        )) : <p className="table-empty">当前无模拟盘持仓。系统不会为了保持活跃而强制交易。</p>}
      </section>
    </div>
  )
}

function ReviewPage({ desk, events }) {
  const review = desk?.review
  const postmarketJob = (desk?.jobs || []).find(
    (item) => item.job_name === 'postmarket_review',
  )
  return (
    <div className="page-stack">
      <PageHeader eyebrow="BIDIRECTIONAL POSTMORTEM" title="自动复盘" detail="同时回答“赚为什么赚、亏为什么亏”，并复查被拒绝标的、漏选强势股和尾仓反事实。" />
      {postmarketJob?.status === 'failed' && (
        <div className="evidence-warning">
          <AlertTriangle size={16} />
          最近盘后复盘失败：{postmarketJob.trade_date} · {postmarketJob.error_code || 'unknown'}。下方只展示最后一份已接受证据。
        </div>
      )}
      <div className="review-grid">
        <section className="panel review-card"><h3>最近复盘交易日</h3><p>{review?.session_date || '暂无已接受复盘'}</p><span>{review?.stale ? '证据不是最新交易日' : '证据已对齐目标交易日'}</span></section>
        <section className="panel review-card"><h3>覆盖机会数</h3><p>{review?.opportunity_count ?? 0} 个全市场强势机会进入归因。</p><span>生产参数修改始终为 false</span></section>
        <section className="panel review-card"><h3>复盘状态</h3><p>{review?.status === 'ready' ? '最后一份不可变快照可读。' : '尚无可验证复盘证据。'}</p><span>{review?.snapshot_id || 'N/A'}</span></section>
      </div>
      <section className="panel review-opportunities">
        <header className="panel-header"><div><span className="eyebrow">MISSED MOVERS ATTRIBUTION</span><h3>强势股与漏选归因</h3></div></header>
        {(review?.opportunities || []).length ? (
          <div className="review-table">
            <div className="review-row review-head"><span>排名 / 标的</span><span>收盘收益</span><span>盘中 MFE</span><span>是否入选</span><span>根因</span></div>
            {review.opportunities.map((item) => (
              <div className="review-row" key={`${item.rank}-${item.symbol}`}>
                <span><b>{item.rank ?? '—'}</b><strong>{item.symbol}</strong></span>
                <span className={item.close_return >= 0 ? 'positive' : 'negative'}>{percent(item.close_return)}</span>
                <span>{percent(item.mfe)}</span>
                <span>{REVIEW_STATUS_LABELS[item.selection_status] || item.selection_status || 'N/A'}</span>
                <span>{REVIEW_CAUSE_LABELS[item.root_cause] || '证据不足'}</span>
              </div>
            ))}
          </div>
        ) : <p className="table-empty">暂无可展示的复盘机会。</p>}
      </section>
      <section className="panel timeline">
        <h3>最近决策事实</h3>
        {events.slice().reverse().slice(0, 12).map((item) => (
          <div key={item.sequence}><time>{localTime(item.observed_at_utc)}</time><strong>{item.event.symbol}</strong><span>{ACTION_LABELS[item.event.action] || item.event.action}</span></div>
        ))}
        {!events.length && <p className="muted">尚无可复盘的实时事件。</p>}
      </section>
    </div>
  )
}

function AgentsPage({ desk }) {
  const descriptions = {
    catalyst: 'DeepSeek V4 Pro · 只读取冻结新闻事实并发布有时效的语义判断',
    red_team: '独立角色 · 专门查找重大负面、错误因果和过度乐观解释',
    supervisor: '确定性程序 · 核对券商、行情、配置和新闻输入是否完整',
  }
  return (
    <div className="page-stack">
      <PageHeader eyebrow="BOUNDED MULTI-AGENT" title="Agent 审计" detail="Agent 负责难以程序化的语义判断；风控、仓位、时钟和下单永远由确定性程序掌控。" />
      <div className="agent-grid">
        {(desk?.agents || []).map((agent) => (
          <section className="panel agent-card" key={agent.role}>
            <span className={`agent-status ${agent.status === 'healthy' ? 'online' : ''}`}>{AGENT_STATUS_LABELS[agent.status] || agent.status}</span>
            <Bot size={22} /><h3>{AGENT_LABELS[agent.role] || agent.role}</h3>
            <p>{descriptions[agent.role]}</p>
            <small>{agent.current_count}/{agent.symbol_count} 当前 · {localTime(agent.latest_generated_at_utc)} · 无直接下单权</small>
          </section>
        ))}
      </div>
    </div>
  )
}

function SystemPage({ desk, health, error }) {
  const maturity = desk?.maturity || {}
  const rows = [
    ['本地只读服务', error ? '异常' : '在线'],
    ['工程阶段', STAGE_LABELS[desk?.stage] || desk?.stage || 'N/A'],
    ['今日数据管线', PIPELINE_STATUS_LABELS[desk?.pipeline_status] || desk?.pipeline_status || 'N/A'],
    ['Paper 写入资格', desk?.paper_eligible ? '已批准' : '未批准'],
    ['实盘资格', desk?.live_eligible ? '已批准' : '未批准'],
    ['客户端手工下单', '禁止'],
    ['全局急停', health?.emergency_stop_active ? '已触发' : '待命'],
  ]
  return (
    <div className="page-stack">
      <PageHeader eyebrow="LOCAL-FIRST CONTROL PLANE" title="系统与安全边界" detail="Docker 本地运行、密钥不进 Git；后续迁移服务器时保持同一接口。" />
      <div className="system-grid">
        <section className="panel system-list">
          <header className="panel-header"><div><span className="eyebrow">CAPABILITY BOUNDARY</span><h3>真实能力边界</h3></div></header>
          {rows.map(([label, value]) => <div key={label}><span>{label}</span><strong>{value}</strong></div>)}
        </section>
        <section className="panel system-list">
          <header className="panel-header"><div><span className="eyebrow">MATURITY EVIDENCE</span><h3>成熟度证据</h3></div></header>
          <div><span>PIT 历史会话</span><strong>{maturity.point_in_time_history_sessions ?? 'N/A'}</strong></div>
          <div><span>Paper 运行会话</span><strong>{maturity.paper_trading_sessions ?? 'N/A'}</strong></div>
          <div><span>净成本标签</span><strong>{maturity.net_labeled_trade_count ?? 'N/A'}</strong></div>
          <div><span>报价成本覆盖率</span><strong>{percent(maturity.quote_cost_coverage)}</strong></div>
          <div><span>Purged OOS 折数</span><strong>{maturity.purged_oos_fold_count ?? 'N/A'}</strong></div>
        </section>
      </div>
      <section className="panel jobs-panel">
        <header className="panel-header"><div><span className="eyebrow">DURABLE JOB LEDGER</span><h3>任务账本</h3></div><span className="timestamp">失败不会被界面隐藏</span></header>
        <div className="job-table">
          <div className="job-row job-head"><span>任务</span><span>交易日</span><span>状态</span><span>尝试</span><span>错误</span></div>
          {(desk?.jobs || []).slice(0, 12).map((job) => (
            <div className="job-row" key={`${job.job_name}-${job.trade_date}`}>
              <span>{JOB_LABELS[job.job_name] || job.job_name}</span>
              <span>{job.trade_date}</span>
              <span className={job.status === 'succeeded' ? 'positive' : job.status === 'failed' ? 'negative' : ''}>{JOB_STATUS_LABELS[job.status] || job.status}</span>
              <span>{job.attempts}</span>
              <span>{job.error_code || '—'}</span>
            </div>
          ))}
        </div>
      </section>
    </div>
  )
}

export default function App() {
  const [dashboard, setDashboard] = useState(null)
  const [desk, setDesk] = useState(null)
  const [health, setHealth] = useState(null)
  const [events, setEvents] = useState([])
  const [selectedId, setSelectedId] = useState(null)
  const [page, setPage] = useState('today')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [stopping, setStopping] = useState(false)

  const refresh = useCallback(async () => {
    try {
      const [dashboardResponse, healthResponse, deskResponse] = await Promise.all([
        fetch('/v1/dashboard', { cache: 'no-store' }),
        fetch('/v1/health', { cache: 'no-store' }),
        fetch('/v1/desk', { cache: 'no-store' }),
      ])
      if (!dashboardResponse.ok || !healthResponse.ok || !deskResponse.ok) throw new Error('状态接口不可用')
      const [nextDashboard, nextHealth, nextDesk] = await Promise.all([
        dashboardResponse.json(),
        healthResponse.json(),
        deskResponse.json(),
      ])
      const after = Math.max(0, Number(nextDashboard.latest_sequence || 0) - 50)
      const eventsResponse = await fetch(
        `/v1/events?after=${after}&limit=50`,
        { cache: 'no-store' },
      )
      if (!eventsResponse.ok) throw new Error('事件接口不可用')
      const nextEvents = await eventsResponse.json()
      setDashboard(nextDashboard)
      setHealth(nextHealth)
      setDesk(nextDesk)
      setEvents(nextEvents.events || [])
      setSelectedId((current) => current || nextDashboard.plans?.[0]?.plan_id || null)
      setError('')
      return nextDashboard
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '无法连接本地决策引擎')
      return null
    } finally {
      setLoading(false)
    }
  }, [])

  const emergencyStop = useCallback(async () => {
    if (health?.emergency_stop_active) return
    if (!window.confirm('确认触发全局急停？触发后本日不可在客户端恢复。')) return
    setStopping(true)
    try {
      const response = await fetch('/v1/emergency-stop', { method: 'POST' })
      if (!response.ok) throw new Error('急停接口不可用')
      await refresh()
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '急停失败')
    } finally {
      setStopping(false)
    }
  }, [health, refresh])

  useEffect(() => {
    let cancelled = false
    let stream = null
    const start = async () => {
      const initial = await refresh()
      if (cancelled) return
      const after = Number(initial?.latest_sequence || 0)
      stream = new EventSource(`/v1/events/stream?after=${after}`)
      stream.addEventListener('plan-decision', refresh)
      stream.onerror = () => {}
    }
    start()
    const timer = window.setInterval(refresh, 15000)
    return () => {
      cancelled = true
      window.clearInterval(timer)
      if (stream) stream.close()
    }
  }, [refresh])

  const plans = dashboard?.plans || []
  const selected = useMemo(
    () => plans.find((item) => item.plan_id === selectedId) || plans[0] || null,
    [plans, selectedId],
  )
  const activePlanIds = new Set(plans.map((item) => item.plan_id))
  const latestEvents = events
    .filter((item) => activePlanIds.has(item.plan_id))
    .reverse()
    .slice(0, 8)

  const openPlan = (planId) => {
    setSelectedId(planId)
    setPage('today')
  }

  const pageContent = page === 'today'
    ? <section className="content">{selected ? <Detail plan={selected} /> : <SelectionOverview desk={desk} />}</section>
    : page === 'opportunities'
      ? <section className="content"><OpportunitiesPage desk={desk} plans={plans} onOpen={openPlan} /></section>
      : page === 'positions'
        ? <section className="content"><PositionsPage plans={plans} /></section>
        : page === 'review'
          ? <section className="content"><ReviewPage desk={desk} events={events} /></section>
          : page === 'agents'
            ? <section className="content"><AgentsPage desk={desk} /></section>
            : <section className="content"><SystemPage desk={desk} health={health} error={error} /></section>

  const pipelineHealthy = desk?.pipeline_status === 'ready'
  const systemLabel = health?.emergency_stop_active
    ? '全局急停已触发'
    : error
      ? '本地服务连接异常'
      : desk?.pipeline_status === 'degraded'
        ? '数据管线降级'
        : desk?.pipeline_status === 'waiting'
          ? '等待今日数据'
          : '只读证据服务在线'

  return (
    <main className="app">
      <header className="topbar">
        <div className="brand"><span><Activity size={21} /></span><div><strong>日内量化决策台</strong><small>ADAPTIVE SIGNAL DESK</small></div></div>
        <div className="system-state"><StatusDot ok={!error && pipelineHealthy && !health?.emergency_stop_active} /><span>{systemLabel}</span></div>
        <button type="button" className="refresh" onClick={refresh}><RefreshCw size={15} />刷新</button>
        <button type="button" className={`emergency ${health?.emergency_stop_active ? 'active' : ''}`} disabled={stopping || health?.emergency_stop_active} onClick={emergencyStop}><Power size={15} />{health?.emergency_stop_active ? '已急停' : '全局急停'}</button>
      </header>

      <section className="safety-banner">
        <ShieldCheck size={18} />
        <div><strong>研究只读控制台</strong><span>展示不可变选股、动态预案与复盘证据；Paper 写入和实盘均未获得批准。</span></div>
        <span className="readonly">RESEARCH ONLY</span>
      </section>

      {error && <div className="error-banner"><AlertTriangle size={17} />{error}。客户端保留最后一次已知状态，不生成新建议。</div>}
      {!error && desk?.pipeline_status === 'degraded' && <div className="error-banner"><AlertTriangle size={17} />今日数据管线存在失败任务。历史快照会明确标记为过期，不会冒充今日结果。</div>}

      <div className={`workspace ${page !== 'today' ? 'page-mode' : ''}`}>
        <nav className="nav-rail">
          {PAGES.map(({ id, label, icon: Icon }) => (
            <button key={id} type="button" className={page === id ? 'active' : ''} onClick={() => setPage(id)}><Icon size={17} /><span>{label}</span></button>
          ))}
        </nav>
        {page === 'today' && <aside>
          <div className="aside-title"><div><span className="eyebrow">TODAY</span><h3>{plans.length ? '动态交易预案' : '硬闸候选快照'}</h3></div><span>{plans.length || desk?.selection?.pass_count || 0}</span></div>
          <div className="plan-list">
            {loading
              ? <p className="muted">正在读取真实状态……</p>
              : plans.length
                ? plans.map((plan) => (
                    <PlanCard key={plan.plan_id} plan={plan} selected={plan.plan_id === selected?.plan_id} onSelect={() => setSelectedId(plan.plan_id)} />
                  ))
                : (desk?.selection?.candidates || []).slice(0, 8).map((candidate) => (
                    <div className="snapshot-card" key={candidate.symbol}>
                      <div><strong>{candidate.symbol}</strong><span>#{candidate.rank}</span></div>
                      <p>缺口 {percent(candidate.premarket_gap_return)} · RVOL {number(candidate.rvol, 1)}×</p>
                      <small>{desk?.selection?.stale ? `${desk.selection.session_date} 历史快照` : '尚未生成动态预案'}</small>
                    </div>
                  ))}
          </div>
          <section className="event-log">
            <header><BellRing size={15} /><strong>状态变化</strong></header>
            {latestEvents.length ? latestEvents.map((item) => (
              <div className="event" key={item.sequence}>
                <span>{item.event.symbol}</span>
                <p>{ACTION_LABELS[item.event.action] || item.event.action}</p>
                <small>{localTime(item.observed_at_utc)}</small>
              </div>
            )) : <p className="muted">尚无实质性状态变化。</p>}
          </section>
        </aside>}
        {pageContent}
      </div>

      <footer>
        <span><Database size={13} /> SQLite 可恢复状态与追加式事件记录</span>
        <span><Clock3 size={13} /> 15 秒感知 · 完整 K 线/风险事件驱动决策</span>
        <span>客户端下单：禁止 · Paper：未批准 · Live：未实现</span>
      </footer>
    </main>
  )
}
