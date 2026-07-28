import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Activity,
  AlertTriangle,
  ArrowDownRight,
  ArrowUpRight,
  BarChart3,
  BellRing,
  CheckCircle2,
  Clock3,
  Database,
  Gauge,
  RefreshCw,
  ShieldCheck,
  Target,
  Waves,
} from 'lucide-react'

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

export default function App() {
  const [dashboard, setDashboard] = useState(null)
  const [events, setEvents] = useState([])
  const [selectedId, setSelectedId] = useState(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)

  const refresh = useCallback(async () => {
    try {
      const dashboardResponse = await fetch('/v1/dashboard', { cache: 'no-store' })
      if (!dashboardResponse.ok) throw new Error('状态接口不可用')
      const nextDashboard = await dashboardResponse.json()
      const after = Math.max(0, Number(nextDashboard.latest_sequence || 0) - 50)
      const eventsResponse = await fetch(
        `/v1/events?after=${after}&limit=50`,
        { cache: 'no-store' },
      )
      if (!eventsResponse.ok) throw new Error('事件接口不可用')
      const nextEvents = await eventsResponse.json()
      setDashboard(nextDashboard)
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

  return (
    <main className="app">
      <header className="topbar">
        <div className="brand"><span><Activity size={21} /></span><div><strong>日内量化决策台</strong><small>ADAPTIVE SIGNAL DESK</small></div></div>
        <div className="system-state"><StatusDot ok={!error} /><span>{error ? '引擎连接异常' : '确定性引擎在线'}</span></div>
        <button type="button" className="refresh" onClick={refresh}><RefreshCw size={15} />刷新</button>
      </header>

      <section className="safety-banner">
        <ShieldCheck size={18} />
        <div><strong>只读建议模式</strong><span>客户端没有订单接口；风险基线、状态机和券商持仓对账均在独立引擎执行。</span></div>
        <span className="readonly">ORDERS OFF</span>
      </section>

      {error && <div className="error-banner"><AlertTriangle size={17} />{error}。客户端保留最后一次已知状态，不生成新建议。</div>}

      <div className="workspace">
        <aside>
          <div className="aside-title"><div><span className="eyebrow">TODAY</span><h3>动态交易预案</h3></div><span>{plans.length}</span></div>
          <div className="plan-list">
            {loading ? <p className="muted">正在读取真实状态……</p> : plans.map((plan) => (
              <PlanCard key={plan.plan_id} plan={plan} selected={plan.plan_id === selected?.plan_id} onSelect={() => setSelectedId(plan.plan_id)} />
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
        </aside>
        <section className="content"><Detail plan={selected} /></section>
      </div>

      <footer>
        <span><Database size={13} /> SQLite 可恢复状态与追加式事件记录</span>
        <span><Clock3 size={13} /> 15 秒感知 · 完整 K 线/风险事件驱动决策</span>
        <span>{dashboard?.orders_authorized === false ? '订单权限：关闭' : '安全状态未知'}</span>
      </footer>
    </main>
  )
}
