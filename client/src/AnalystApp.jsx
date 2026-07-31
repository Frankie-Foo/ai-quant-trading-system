import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Activity,
  AlertTriangle,
  Bot,
  BrainCircuit,
  CheckCircle2,
  Clock3,
  Database,
  KeyRound,
  LoaderCircle,
  MessageSquareText,
  Radar,
  RefreshCw,
  Save,
  Settings,
  ShieldCheck,
  Sparkles,
} from 'lucide-react'
import {
  booleanLabel,
  evidenceReferenceFromDesk,
  evidenceReferenceKey,
} from './client-state'

const bridge = window.analystDesktop

const PAGES = [
  { id: 'today', label: '选股', icon: Radar },
  { id: 'data', label: '数据', icon: Database },
  { id: 'assistant', label: '答疑', icon: MessageSquareText },
  { id: 'review', label: '复盘', icon: BrainCircuit },
  { id: 'agents', label: 'Agent', icon: Bot },
  { id: 'settings', label: '设置', icon: Settings },
]

const MODEL_LABELS = {
  question: '答疑模型',
  catalyst: '催化剂 Agent',
  red_team: '红队 Agent',
  supervisor: '证据审计 Agent',
}

const AGENT_META = {
  catalyst: {
    title: '催化剂 Agent',
    detail: '解释冻结催化剂强度、时效、盘前缺口和可能的延续条件。',
  },
  red_team: {
    title: '红队 Agent',
    detail: '寻找反例、过期证据、错误因果、拥挤与流动性风险。',
  },
  supervisor: {
    title: '证据审计 Agent',
    detail: '核对选股、复盘、任务与成熟度证据是否当前、完整且一致。',
  },
}

const REVIEW_CAUSES = {
  selected: '已捕获机会',
  intentional_gate: '硬闸主动放弃',
  late_catalyst: '盘中新催化，盘前不可知',
  data_or_classifier_gap: '新闻抓取或分类缺口',
  factor_gap: '技术、订单流或行业因子缺口',
  incomplete_evidence: '证据不完整，暂不归因',
}

const SELECTION_STATUS = {
  ready: '今日锁池完成',
  waiting: '等待今日锁池',
  blocked: '今日锁池受阻',
  missing: '今日锁池缺失',
}

const percent = (value, digits = 2) => (
  value == null ? 'N/A' : `${value >= 0 ? '+' : ''}${(value * 100).toFixed(digits)}%`
)

const number = (value, digits = 1) => (
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
    hour12: false,
  }).format(new Date(value))
}

function LoadingScreen() {
  return (
    <main className="analyst-loading">
      <span><Activity size={26} /></span>
      <LoaderCircle className="spin" size={24} />
      <p>正在读取安全配置……</p>
    </main>
  )
}

function EvidenceStamp({ evidence, currentKey }) {
  if (!evidence) return <small>证据引用缺失</small>
  const changed = evidenceReferenceKey(evidence) !== currentKey
  return (
    <small className={changed ? 'evidence-stale' : ''}>
      证据 {evidence.targetTradeDate || 'N/A'} · {evidence.selectionSnapshotId || '无选股快照'} · {changed ? '当前证据已变化' : evidence.selectionStale ? '历史快照' : '当前快照'}
    </small>
  )
}

function ConnectionSetup({ busy, error, runtimeStatus, onValidated }) {
  const [openRouterApiKey, setOpenRouterApiKey] = useState('')
  const [massiveApiKey, setMassiveApiKey] = useState('')
  const [marketDataKey, setMarketDataKey] = useState('')
  const [marketDataSecret, setMarketDataSecret] = useState('')
  const [secUserAgent, setSecUserAgent] = useState('')

  const submit = async (event) => {
    event.preventDefault()
    await onValidated({
      openRouterApiKey,
      massiveApiKey,
      marketDataKey,
      marketDataSecret,
      secUserAgent,
    })
  }

  return (
    <main className="onboarding-shell">
      <section className="onboarding-card">
        <div className="onboarding-brand"><span><Activity size={24} /></span><div><strong>AI 量化研究台</strong><small>DESKTOP RESEARCH EDITION</small></div></div>
        <span className="step-label">首次启动 · 1 / 2</span>
        <h1>启用本地研究内核与模型</h1>
        <p className="onboarding-lead">选股、因子和复盘都在这台电脑本机运行；实时行情使用固定 Alpaca SIP 代理，不包含模拟盘或订单入口。</p>
        <div className="local-runtime-card">
          <span><Database size={17} /></span>
          <div><strong>trading-system-v2 本地内核</strong><small>{runtimeStatus?.local_execution ? '已启动 · 本地执行' : '正在启动'} · 行情源：Alpaca SIP 代理</small></div>
        </div>
        <form className="setup-form" onSubmit={submit}>
          <label>
            <span>OpenRouter API Key</span>
            <input type="password" value={openRouterApiKey} onChange={(event) => setOpenRouterApiKey(event.target.value)} placeholder="sk-or-…" autoComplete="off" required />
            <small>只在 Electron 主进程使用，并由操作系统安全存储加密保存；答疑与 Agent 会把最小化研究证据发送至 OpenRouter 及所选模型提供商。</small>
          </label>
          <label>
            <span>Massive API Key</span>
            <input type="password" value={massiveApiKey} onChange={(event) => setMassiveApiKey(event.target.value)} autoComplete="new-password" required />
            <small>用于历史日线、分钟线、新闻和参考数据。</small>
          </label>
          <label>
            <span>Alpaca 代理行情 Key</span>
            <input type="password" value={marketDataKey} onChange={(event) => setMarketDataKey(event.target.value)} autoComplete="off" required />
          </label>
          <label>
            <span>SEC 联系信息</span>
            <input type="text" value={secUserAgent} onChange={(event) => setSecUserAgent(event.target.value)} placeholder="姓名 email@example.com" autoComplete="off" required />
            <small>SEC 要求请求携带可联系的 User-Agent。</small>
          </label>
          <label>
            <span>Alpaca 代理行情 Secret</span>
            <input type="password" value={marketDataSecret} onChange={(event) => setMarketDataSecret(event.target.value)} autoComplete="off" required />
            <small>代理地址固定写入程序；凭据只进入操作系统安全存储，不进入安装包或 Git。</small>
          </label>
          {error && <div className="setup-error"><AlertTriangle size={16} />{error}</div>}
          <button className="primary-action" type="submit" disabled={busy}>
            {busy ? <LoaderCircle className="spin" size={16} /> : <ShieldCheck size={16} />}
            验证连接
          </button>
        </form>
        <div className="boundary-note"><ShieldCheck size={15} /><span>实时 SIP 只提供报价、成交和分钟线；历史、新闻或财务数据不足时仍会阻断完整选股。使用答疑或 Agent 时，最小化证据会发送至 OpenRouter。</span></div>
      </section>
    </main>
  )
}

function ModelSetup({ models, initialValues, busy, error, onSave, embedded = false }) {
  const defaultModel = models[0]?.id || ''
  const [values, setValues] = useState(() => ({
    question: initialValues?.question || defaultModel,
    catalyst: initialValues?.catalyst || defaultModel,
    red_team: initialValues?.red_team || defaultModel,
    supervisor: initialValues?.supervisor || defaultModel,
  }))

  useEffect(() => {
    if (!defaultModel) return
    setValues((current) => {
      const next = { ...current }
      for (const key of Object.keys(MODEL_LABELS)) {
        if (!next[key]) next[key] = initialValues?.[key] || defaultModel
      }
      return next
    })
  }, [defaultModel, initialValues])

  const content = (
    <>
      {!embedded && <div className="onboarding-brand"><span><Sparkles size={24} /></span><div><strong>模型路由</strong><small>OPENROUTER CATALOG</small></div></div>}
      {!embedded && <span className="step-label">首次启动 · 2 / 2</span>}
      <h1>{embedded ? '四个角色分别选择模型' : '选择答疑和 Agent 模型'}</h1>
      <p className="onboarding-lead">四个角色互相独立，后续可随时修改。模型只负责解释与审计，不能改变选股硬闸。</p>
      <div className="model-form">
        {Object.entries(MODEL_LABELS).map(([key, label]) => (
          <label key={key}>
            <span>{label}</span>
            <select value={values[key]} onChange={(event) => setValues((current) => ({ ...current, [key]: event.target.value }))}>
              {models.map((model) => (
                <option key={model.id} value={model.id}>{model.name} · {model.id}</option>
              ))}
            </select>
          </label>
        ))}
      </div>
      {error && <div className="setup-error"><AlertTriangle size={16} />{error}</div>}
      <button className="primary-action" type="button" disabled={busy || !models.length} onClick={() => onSave(values)}>
        {busy ? <LoaderCircle className="spin" size={16} /> : <Save size={16} />}
        保存模型配置
      </button>
    </>
  )

  if (embedded) return <section className="panel settings-models">{content}</section>
  return <main className="onboarding-shell"><section className="onboarding-card model-card">{content}</section></main>
}

function CandidateTable({ candidates = [] }) {
  if (!candidates.length) return <p className="table-empty">当前没有可验证的硬闸候选。</p>
  return (
    <div className="analyst-table">
      <div className="analyst-row analyst-head"><span>排名 / 标的</span><span>盘前缺口</span><span>RVOL</span><span>财报证据</span><span>盘前位置</span></div>
      {candidates.map((candidate) => (
        <div className="analyst-row" key={candidate.symbol}>
          <span><b>{candidate.rank ?? '—'}</b><strong>{candidate.symbol}</strong></span>
          <span className={candidate.premarket_gap_return > 0 ? 'positive' : ''}>{percent(candidate.premarket_gap_return)}</span>
          <span>{number(candidate.rvol)}×</span>
          <span>{candidate.earnings_strength_confirmed == null ? 'N/A' : candidate.earnings_strength_confirmed ? '强确认' : `${candidate.earnings_evidence_layers ?? 0} 层`}<small>强度 {number(candidate.earnings_intensity_score, 0)}</small></span>
          <span>{booleanLabel(candidate.premarket_above_vwap, 'VWAP 上方', 'VWAP 下方')}<small>收盘位 {percent(candidate.premarket_close_location, 0)}</small></span>
        </div>
      ))}
    </div>
  )
}

function TodayPage({
  desk,
  workflowStatus,
  runBusy,
  runResult,
  onSyncData,
  onRunSelection,
  onRunReview,
  onToggleMonitor,
}) {
  const selection = desk?.selection || {}
  const candidates = selection.candidates || []
  const top = candidates[0]
  const activeJob = workflowStatus?.active_job
  const latestJob = workflowStatus?.latest_job
  const displayedRunResult = activeJob
    ? { status: 'running' }
    : latestJob?.action === 'run_today' ? latestJob : runResult
  const monitor = workflowStatus?.monitor || {}
  const progressPercent = activeJob?.overall_progress_percent
    ?? (activeJob?.total_steps
      ? activeJob.completed_steps / activeJob.total_steps * 100
      : 0)
  return (
    <div className="analyst-page-stack">
      <section className={`panel analyst-selection ${selection.stale || selection.status === 'blocked' ? 'warn' : ''}`}>
        <div><span className="eyebrow">POINT-IN-TIME SELECTION</span><h2>{SELECTION_STATUS[selection.status] || '正在读取'}</h2><p>目标 {selection.target_trade_date || desk?.target_trade_date || 'N/A'} · 证据 {selection.session_date || 'N/A'}{selection.stale ? ' · 历史快照，不构成今日建议' : ''}</p></div>
        <div className="selection-actions">
          <div className="selection-count"><strong>{selection.pass_count ?? 0}</strong><span>硬闸通过</span></div>
          <button type="button" disabled={runBusy} onClick={onRunSelection}>
            {runBusy ? <LoaderCircle className="spin" size={15} /> : <Radar size={15} />}
            {runBusy ? '正在运行' : '开始今日流程'}
          </button>
        </div>
      </section>
      <section className="panel today-flow-card"><div><strong>今日流程</strong><span>增量同步 → 今日选股 → 成功后自动启动盘中监控</span></div><div><span>监控：{monitor.status || 'stopped'} · {monitor.events_stored || 0} 条</span>{monitor.status === 'running' && <button type="button" onClick={onToggleMonitor}>停止监控</button>}</div></section>
      {activeJob && <section className="panel workflow-progress-panel">
        <header><div><span className="eyebrow">BACKGROUND WORKFLOW</span><h3>{activeJob.action === 'sync_data' ? '正在同步数据' : activeJob.action === 'run_selection' ? '正在运行今日选股' : '正在生成收盘复盘'}</h3></div><strong>{progressPercent.toFixed(1)}%</strong></header>
        <div className="workflow-progress-track"><i style={{ width: `${Math.max(0, Math.min(100, progressPercent))}%` }} /></div>
        <div className="workflow-progress-meta"><span>总步骤 {activeJob.completed_steps}/{activeJob.total_steps}</span><span>当前：{activeJob.current_step || '准备中'}</span><span>{activeJob.step_progress_total ? `本步骤 ${activeJob.step_progress_current}/${activeJob.step_progress_total}` : '等待子任务进度'}</span><span>{activeJob.progress_detail || ''}</span></div>
      </section>}
      {!activeJob && latestJob?.status === 'failed' && <div className="analyst-warning"><AlertTriangle size={16} />最近任务失败：{latestJob.error || '请查看任务日志'}</div>}
      {displayedRunResult && <div className={`analyst-warning ${displayedRunResult.status === 'complete' || displayedRunResult.status === 'running' ? 'success' : ''}`}><Activity size={16} />本次运行：{displayedRunResult.status === 'running' ? '正在后台运行，可在进度中查看' : displayedRunResult.status === 'complete' ? '今日流程已完成，候选已刷新并自动启动监控' : displayedRunResult.reason === 'initial_data_sync_required' ? '请先点击“同步数据”完成首次数据准备' : displayedRunResult.reason === 'historical_research_inputs_missing' ? '实时行情正常，但仍缺历史、新闻和财务研究输入' : displayedRunResult.error || displayedRunResult.reason || displayedRunResult.status}</div>}
      {selection.blocker && <div className="analyst-warning"><AlertTriangle size={16} />流程阻断：{selection.blocker === 'market_data_provider_unconfigured' ? '行情源尚未配置' : selection.blocker === 'historical_research_inputs_missing' ? '实时行情已连接，但历史、新闻和财务研究输入尚未齐全' : selection.blocker}</div>}
      <div className="analyst-metrics">
        <article><span>第一名</span><strong>{top?.symbol || 'N/A'}</strong><small>冻结排名</small></article>
        <article><span>盘前缺口</span><strong>{percent(top?.premarket_gap_return)}</strong><small>不等于入场许可</small></article>
        <article><span>RVOL</span><strong>{top ? `${number(top.rvol)}×` : 'N/A'}</strong><small>同窗口相对成交量</small></article>
        <article><span>证据时间</span><strong>{localTime(selection.asof_utc)}</strong><small>{selection.snapshot_id || '无快照'}</small></article>
      </div>
      <section className="panel">
        <header className="analyst-panel-header"><div><span className="eyebrow">DETERMINISTIC GATE PASSERS</span><h3>硬闸候选名单</h3></div><span>只读 · 不含交易动作</span></header>
        <CandidateTable candidates={candidates} />
      </section>
    </div>
  )
}

function DataPage({ workflowStatus, onSyncData }) {
  const inventory = workflowStatus?.data_inventory || {}
  const active = workflowStatus?.active_job
  return <div className="analyst-page-stack"><header className="analyst-page-header"><div><span className="eyebrow">LOCAL DATA PLANE</span><h2>数据与同步</h2></div><button type="button" disabled={Boolean(active)} onClick={onSyncData}><RefreshCw size={15} />同步缺失增量</button></header><div className="review-summary"><article><span>日线快照</span><strong>{inventory.grouped_daily || 0}</strong><small>Massive</small></article><article><span>参考数据</span><strong>{inventory.reference || 0}</strong><small>证券身份</small></article><article><span>新闻分区</span><strong>{inventory.news || 0}</strong><small>催化剂历史</small></article><article><span>选股准备</span><strong>{inventory.ready_for_selection ? '已就绪' : '未就绪'}</strong><small>仅补缺失日期</small></article></div>{active?.action === 'sync_data' && <section className="panel workflow-progress-panel"><header><div><span className="eyebrow">SYNC PROGRESS</span><h3>正在同步数据</h3></div><strong>{(active.overall_progress_percent || 0).toFixed(1)}%</strong></header><div className="workflow-progress-track"><i style={{ width: `${active.overall_progress_percent || 0}%` }} /></div><div className="workflow-progress-meta"><span>{active.completed_steps}/{active.total_steps}</span><span>{active.current_step}</span><span>{active.step_progress_current || 0}/{active.step_progress_total || 0}</span><span>{active.progress_detail || ''}</span></div></section>}</div>
}

function AssistantPage({ desk, model }) {
  const currentEvidenceKey = evidenceReferenceKey(evidenceReferenceFromDesk(desk))
  const [question, setQuestion] = useState('')
  const [messages, setMessages] = useState([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const ask = async (event) => {
    event.preventDefault()
    const text = question.trim()
    if (!text || busy) return
    setMessages((current) => [...current, { role: 'user', content: text }])
    setQuestion('')
    setBusy(true)
    setError('')
    try {
      const result = await bridge.assistant.ask(text)
      setMessages((current) => [...current, {
        role: 'assistant',
        content: result.content,
        model: result.model,
        usage: result.usage,
        truncated: result.truncated,
        evidence: result.evidence,
      }])
    } catch (caught) {
      setError(caught.message || '答疑请求失败')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="analyst-page-stack assistant-layout">
      <header className="analyst-page-header"><div><span className="eyebrow">EVIDENCE-BOUND Q&A</span><h2>研究答疑</h2></div><p>模型：{model || 'N/A'} · 自动附带当前选股与复盘证据</p></header>
      <section className="panel chat-panel">
        <div className="chat-context"><Database size={15} /><span>当前上下文：目标交易日 {desk?.target_trade_date || 'N/A'} · {desk?.selection?.stale ? '历史证据' : '当前证据'} · 最小化证据将发送至 OpenRouter · 无交易权限</span></div>
        <div className="chat-thread">
          {!messages.length && <div className="chat-empty"><MessageSquareText size={28} /><h3>可以问什么？</h3><p>为什么选它、今天为何空仓、漏选因子是什么、证据是否过期、复盘能得到什么假设。</p></div>}
          {messages.map((message, index) => (
            <article className={`chat-message ${message.role}`} key={`${message.role}-${index}`}>
              <span>{message.role === 'user' ? '你' : 'AI'}</span>
              <div><p>{message.content}</p>{message.truncated && <em className="answer-truncated">回答达到长度上限，请继续追问或缩小问题范围。</em>}{message.model && <small>{message.model} · {message.usage?.totalTokens || 0} tokens</small>}{message.role === 'assistant' && <EvidenceStamp evidence={message.evidence} currentKey={currentEvidenceKey} />}</div>
            </article>
          ))}
          {busy && <div className="chat-thinking"><LoaderCircle className="spin" size={16} />正在根据证据回答……</div>}
        </div>
        <form className="chat-input" onSubmit={ask}>
          <textarea value={question} onChange={(event) => setQuestion(event.target.value)} placeholder="输入关于今日选股或复盘的问题…" maxLength={4000} />
          <button type="submit" disabled={busy || !question.trim()}><Sparkles size={16} />发送</button>
        </form>
        {error && <div className="inline-error"><AlertTriangle size={15} />{error}</div>}
      </section>
    </div>
  )
}

function ReviewPage({ desk, workflowStatus, onRunReview }) {
  const review = desk?.review || {}
  const opportunities = review.opportunities || []
  const reviewJobs = (workflowStatus?.jobs || [])
    .filter((job) => job.action === 'run_review')
  const reviewSnapshotCount = workflowStatus?.data_inventory?.reviews || 0
  return (
    <div className="analyst-page-stack">
      <header className="analyst-page-header"><div><span className="eyebrow">POST-CLOSE ATTRIBUTION</span><h2>自动复盘</h2></div><button type="button" disabled={Boolean(workflowStatus?.active_job)} onClick={onRunReview}><BrainCircuit size={15} />运行收盘复盘</button></header>
      <div className="review-summary">
        <article><span>覆盖机会</span><strong>{review.opportunity_count ?? 0}</strong><small>全市场强势股</small></article>
        <article><span>证据状态</span><strong>{review.status === 'ready' ? '可追溯' : '不可用'}</strong><small>不可变快照</small></article>
        <article><span>生产变更</span><strong>禁止</strong><small>只生成沙盒假设</small></article>
      </div>
      <section className="panel">
        <header className="analyst-panel-header"><div><span className="eyebrow">MISSED MOVERS</span><h3>强势股与漏选归因</h3></div></header>
        <div className="review-analyst-table">
          <div className="review-analyst-row analyst-head"><span>排名 / 标的</span><span>收盘收益</span><span>盘中 MFE</span><span>入选状态</span><span>根因</span></div>
          {opportunities.map((item) => (
            <div className="review-analyst-row" key={`${item.rank}-${item.symbol}`}>
              <span><b>{item.rank}</b><strong>{item.symbol}</strong></span>
              <span className="positive">{percent(item.close_return)}</span>
              <span>{percent(item.mfe)}</span>
              <span>{item.selection_status === 'selected' ? '已入选' : item.selection_status === 'rejected' ? '硬闸拒绝' : '未见'}</span>
              <span>{REVIEW_CAUSES[item.root_cause] || '证据不足'}</span>
            </div>
          ))}
          {!opportunities.length && <p className="table-empty">暂无可验证复盘。</p>}
        </div>
      </section>
      <section className="panel review-history">
        <header className="analyst-panel-header"><div><span className="eyebrow">REVIEW HISTORY</span><h3>历史复盘记录</h3></div><span>{reviewSnapshotCount} 个快照 · {reviewJobs.length} 次手动任务</span></header>
        {reviewJobs.map((job) => <div key={job.job_id}><span>{job.trade_date}</span><strong>{job.status}</strong><small>{job.error || `完成 ${job.completed_steps}/${job.total_steps}`}</small></div>)}
        {!reviewJobs.length && <p className="table-empty">已导入 {reviewSnapshotCount} 个历史复盘快照；当前表格展示最近一期，尚无手动复盘任务记录。</p>}
      </section>
    </div>
  )
}

function AgentsPage({ settings, desk }) {
  const currentEvidenceKey = evidenceReferenceKey(evidenceReferenceFromDesk(desk))
  const [results, setResults] = useState({})
  const [busyRole, setBusyRole] = useState('')
  const [error, setError] = useState('')

  const run = async (role) => {
    setBusyRole(role)
    setError('')
    try {
      const result = await bridge.agents.run(role)
      setResults((current) => ({ ...current, [role]: result }))
    } catch (caught) {
      setError(caught.message || 'Agent 运行失败')
    } finally {
      setBusyRole('')
    }
  }

  return (
    <div className="analyst-page-stack">
      <header className="analyst-page-header"><div><span className="eyebrow">BOUNDED MULTI-AGENT REVIEW</span><h2>三个只读 Agent</h2></div><p>各自选择模型 · 只解释和审计 · 不改变选股、不下单</p></header>
      {error && <div className="analyst-warning"><AlertTriangle size={16} />{error}</div>}
      <div className="analyst-agent-grid">
        {Object.entries(AGENT_META).map(([role, meta]) => {
          const result = results[role]
          return (
            <section className="panel analyst-agent" key={role}>
              <div className="agent-title"><span><Bot size={20} /></span><div><h3>{meta.title}</h3><small>{settings.models?.[role] || '未配置'}</small></div></div>
              <p>{meta.detail}</p>
              <button type="button" disabled={Boolean(busyRole)} onClick={() => run(role)}>
                {busyRole === role ? <LoaderCircle className="spin" size={15} /> : <Sparkles size={15} />}运行今日复核
              </button>
              {result && <div className="agent-result"><p>{result.content}</p><small>{result.model} · {result.usage?.totalTokens || 0} tokens</small><EvidenceStamp evidence={result.evidence} currentKey={currentEvidenceKey} /></div>}
            </section>
          )
        })}
      </div>
    </div>
  )
}

function SettingsPage({ settings, models, runtimeStatus, workflowStatus, ibkrStatus, busy, error, onLoadModels, onSaveModels, onReset }) {
  const [editingModels, setEditingModels] = useState(false)
  return (
    <div className="analyst-page-stack">
      <header className="analyst-page-header"><div><span className="eyebrow">SECURITY & PROVIDERS</span><h2>设置</h2></div><p>凭据不显示、不导出、不进入 Git；AI 请求会发送最小化研究证据至 OpenRouter</p></header>
      <div className="settings-grid">
        <section className="panel settings-card"><KeyRound size={20} /><h3>安全凭据</h3><div><span>OpenRouter</span><strong>{settings.openRouterKeyConfigured ? '已安全保存' : '未配置'}</strong></div><div><span>Massive</span><strong>{settings.massiveConfigured ? '已安全保存' : '未配置'}</strong></div><div><span>Alpaca SIP</span><strong>{settings.marketDataConfigured ? '已安全保存' : '未配置'}</strong></div><div><span>SEC</span><strong>{settings.secConfigured ? '已安全保存' : '未配置'}</strong></div></section>
        <section className="panel settings-card"><ShieldCheck size={20} /><h3>数据源状态</h3><div><span>执行位置</span><strong>{runtimeStatus?.local_execution ? '本机' : '不可用'}</strong></div><div><span>实时行情</span><strong>{runtimeStatus?.market_data?.realtime_ready ? 'Alpaca SIP 已连接' : '不可用'}</strong></div><div><span>Massive 日线</span><strong>{workflowStatus?.data_inventory?.grouped_daily || 0} 个快照</strong></div><div><span>选股准备</span><strong>{workflowStatus?.data_inventory?.ready_for_selection ? '已就绪' : '等待首次同步'}</strong></div></section>
        <section className="panel settings-card"><Clock3 size={20} /><h3>IBKR Paper 预留</h3><div><span>适配器</span><strong>{ibkrStatus?.adapter || 'N/A'}</strong></div><div><span>连接</span><strong>{ibkrStatus?.connected ? '已连接' : '未配置'}</strong></div><div><span>下单</span><strong>{ibkrStatus?.orderSubmissionEnabled ? '启用' : '强制关闭'}</strong></div></section>
      </div>
      {!editingModels ? (
        <section className="panel current-models">
          <header className="analyst-panel-header"><div><span className="eyebrow">MODEL ROUTING</span><h3>当前模型</h3></div><button type="button" onClick={async () => { await onLoadModels(); setEditingModels(true) }}>更改模型</button></header>
          {Object.entries(MODEL_LABELS).map(([key, label]) => <div key={key}><span>{label}</span><strong>{settings.models?.[key] || '未配置'}</strong></div>)}
        </section>
      ) : (
        <ModelSetup models={models} initialValues={settings.models} busy={busy} error={error} embedded onSave={async (values) => { if (await onSaveModels(values)) setEditingModels(false) }} />
      )}
      <section className="panel reset-zone"><div><h3>重新配置连接</h3><p>清除本机安全存储中的 OpenRouter、行情凭据和模型选择。</p></div><button type="button" onClick={onReset}>清除并重新配置</button></section>
    </div>
  )
}

export default function AnalystApp() {
  const [settings, setSettings] = useState(null)
  const [models, setModels] = useState([])
  const [desk, setDesk] = useState(null)
  const [ibkrStatus, setIbkrStatus] = useState(null)
  const [runtimeStatus, setRuntimeStatus] = useState(null)
  const [workflowStatus, setWorkflowStatus] = useState(null)
  const [page, setPage] = useState('today')
  const [onboarding, setOnboarding] = useState('')
  const [busy, setBusy] = useState(false)
  const [deskError, setDeskError] = useState('')
  const [runtimeError, setRuntimeError] = useState('')
  const [workflowError, setWorkflowError] = useState('')
  const [actionError, setActionError] = useState('')
  const [loading, setLoading] = useState(true)
  const [selectionBusy, setSelectionBusy] = useState(false)
  const [selectionRun, setSelectionRun] = useState(null)
  const connectionError = [runtimeError, deskError, workflowError].filter(Boolean).join('；')
  const error = [connectionError, actionError].filter(Boolean).join('；')

  const refreshDesk = useCallback(async () => {
    try {
      const next = await bridge.desk.get()
      setDesk(next)
      setDeskError('')
      return next
    } catch (caught) {
      setDeskError(caught.message || '研究数据服务不可用')
      return null
    }
  }, [])

  const refreshRuntime = useCallback(async () => {
    try {
      const status = await bridge.runtime.status()
      setRuntimeStatus(status)
      setRuntimeError('')
      return status
    } catch (caught) {
      setRuntimeError(caught.message || '本地研究内核不可用')
      return null
    }
  }, [])

  const refreshWorkflows = useCallback(async () => {
    try {
      const status = await bridge.workflows.status()
      setWorkflowStatus(status)
      setWorkflowError('')
      return status
    } catch (caught) {
      setWorkflowError(caught.message || '后台任务状态不可用')
      return null
    }
  }, [])

  const loadModels = useCallback(async () => {
    setBusy(true)
    setActionError('')
    try {
      const catalog = await bridge.models.list()
      setModels(catalog)
      return catalog
    } catch (caught) {
      setActionError(caught.message || '无法读取 OpenRouter 模型')
      return []
    } finally {
      setBusy(false)
    }
  }, [])

  useEffect(() => {
    let cancelled = false
    document.title = 'AI 量化研究台'
    const initialize = async () => {
      try {
        if (!bridge) throw new Error('桌面安全桥接不可用，请重新启动客户端')
        const current = await bridge.settings.get()
        if (cancelled) return
        setSettings(current)

        try {
          const localStatus = await bridge.runtime.status()
          if (!cancelled) {
            setRuntimeStatus(localStatus)
            setRuntimeError('')
          }
        } catch (caught) {
          if (!cancelled) {
            setRuntimeError(caught.message || '本地研究内核不可用')
          }
        }

        if (!current.openRouterKeyConfigured
          || !current.massiveConfigured
          || !current.marketDataConfigured
          || !current.secConfigured) {
          setOnboarding('connection')
        } else if (!current.configured) {
          try {
            const catalog = await bridge.models.list()
            if (!cancelled) setModels(catalog)
          } catch (caught) {
            if (!cancelled) {
              setActionError(caught.message || '无法读取 OpenRouter 模型')
            }
          }
          if (!cancelled) setOnboarding('models')
        } else {
          await Promise.all([refreshDesk(), refreshWorkflows()])
          try {
            const status = await bridge.ibkrPaper.status()
            if (!cancelled) setIbkrStatus(status)
          } catch (caught) {
            if (!cancelled) {
              setActionError(caught.message || 'IBKR Paper 状态不可用')
            }
          }
        }
      } catch (caught) {
        if (!cancelled) setActionError(caught.message || '客户端初始化失败')
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    initialize()
    return () => { cancelled = true }
  }, [refreshDesk, refreshWorkflows])

  useEffect(() => {
    if (!settings?.configured || onboarding) return undefined
    const timer = window.setInterval(() => {
      refreshDesk()
      refreshRuntime()
      refreshWorkflows()
    }, 5_000)
    return () => window.clearInterval(timer)
  }, [onboarding, refreshDesk, refreshRuntime, refreshWorkflows, settings?.configured])

  const validateConnection = async (connection) => {
    setBusy(true)
    setActionError('')
    try {
      const result = await bridge.settings.validateConnection(connection)
      setSettings(result.settings)
      setModels(result.models)
      setRuntimeStatus(result.runtime)
      setOnboarding('models')
    } catch (caught) {
      setActionError(caught.message || '连接验证失败')
    } finally {
      setBusy(false)
    }
  }

  const saveModels = async (values) => {
    setBusy(true)
    setActionError('')
    try {
      const next = await bridge.settings.saveModels(values)
      setSettings(next)
      setOnboarding('')
      await Promise.all([
        refreshDesk(),
        refreshWorkflows(),
        bridge.ibkrPaper.status().then(setIbkrStatus),
      ])
      return true
    } catch (caught) {
      setActionError(caught.message || '模型配置保存失败')
      return false
    } finally {
      setBusy(false)
    }
  }

  const reset = async () => {
    if (!window.confirm('确认清除本机安全存储中的所有模型与行情凭据？')) return
    setBusy(true)
    setActionError('')
    try {
      const next = await bridge.settings.clear()
      setSettings(next)
      setDesk(null)
      setModels([])
      setRuntimeStatus(null)
      setWorkflowStatus(null)
      setRuntimeError('')
      setDeskError('')
      setWorkflowError('')
      setOnboarding('connection')
      setPage('today')
    } catch (caught) {
      setActionError(caught.message || '无法清除本地设置')
    } finally {
      setBusy(false)
    }
  }

  const runSelection = async () => {
    if (selectionBusy) return
    setSelectionBusy(true)
    setSelectionRun(null)
    setActionError('')
    try {
      const tradeDate = desk?.target_trade_date || new Date().toISOString().slice(0, 10)
      const result = await bridge.workflows.start('run_today', tradeDate)
      setSelectionRun(result.accepted ? { ...result, status: 'running' } : result)
      await Promise.all([refreshDesk(), refreshRuntime(), refreshWorkflows()])
    } catch (caught) {
      setActionError(caught.message || '今日选股运行失败')
    } finally {
      setSelectionBusy(false)
    }
  }

  const startWorkflow = async (action) => {
    const tradeDate = desk?.target_trade_date || new Date().toISOString().slice(0, 10)
    setActionError('')
    try {
      await bridge.workflows.start(action, tradeDate)
      await refreshWorkflows()
    } catch (caught) {
      setActionError(caught.message || '后台任务启动失败')
    }
  }

  const toggleMonitor = async () => {
    const tradeDate = desk?.target_trade_date || new Date().toISOString().slice(0, 10)
    setActionError('')
    try {
      if (workflowStatus?.monitor?.status === 'running') {
        await bridge.monitor.stop()
      } else {
        await bridge.monitor.start(tradeDate)
      }
      await refreshWorkflows()
    } catch (caught) {
      setActionError(caught.message || '盘中监控操作失败')
    }
  }

  const activeCandidates = desk?.selection?.candidates || []
  const navCount = useMemo(() => ({
    today: activeCandidates.length,
    review: desk?.review?.opportunity_count || 0,
  }), [activeCandidates.length, desk?.review?.opportunity_count])

  if (loading) return <LoadingScreen />
  if (!settings) {
    return (
      <main className="onboarding-shell">
        <section className="onboarding-card">
          <div className="setup-error"><AlertTriangle size={16} />{actionError || '客户端初始化失败'}</div>
          <button className="primary-action" type="button" onClick={() => window.location.reload()}><RefreshCw size={16} />重新加载客户端</button>
        </section>
      </main>
    )
  }
  if (onboarding === 'connection') return <ConnectionSetup busy={busy} error={error} runtimeStatus={runtimeStatus} onValidated={validateConnection} />
  if (onboarding === 'models') return <ModelSetup models={models} initialValues={settings.models} busy={busy} error={error} onSave={saveModels} />

  const pageContent = page === 'today'
    ? <TodayPage desk={desk} workflowStatus={workflowStatus} runBusy={selectionBusy} runResult={selectionRun} onRunSelection={runSelection} onToggleMonitor={toggleMonitor} />
    : page === 'data'
      ? <DataPage workflowStatus={workflowStatus} onSyncData={() => startWorkflow('sync_data')} />
    : page === 'assistant'
      ? <AssistantPage desk={desk} model={settings.models.question} />
      : page === 'review'
        ? <ReviewPage desk={desk} workflowStatus={workflowStatus} onRunReview={() => startWorkflow('run_review')} />
        : page === 'agents'
          ? <AgentsPage settings={settings} desk={desk} />
          : <SettingsPage settings={settings} models={models} runtimeStatus={runtimeStatus} workflowStatus={workflowStatus} ibkrStatus={ibkrStatus} busy={busy} error={error} onLoadModels={loadModels} onSaveModels={saveModels} onReset={reset} />

  return (
    <main className="analyst-app">
      <header className="analyst-topbar">
        <div className="analyst-brand"><span><Activity size={21} /></span><div><strong>AI 量化研究台</strong><small>DESKTOP RESEARCH EDITION</small></div></div>
        <div className="analyst-connection"><i className={connectionError ? 'bad' : ''} /><span>{connectionError ? '本地研究服务异常' : '本地研究内核在线'}</span></div>
        <button type="button" onClick={() => Promise.all([refreshDesk(), refreshRuntime(), refreshWorkflows()])}><RefreshCw size={15} />刷新全部</button>
      </header>
      <section className="analyst-boundary"><ShieldCheck size={17} /><div><strong>本地研究版 · 无交易能力</strong><span>选股、因子、复盘在本机执行；AI 答疑会把最小化证据发送至 OpenRouter；IBKR Paper 仅预留接口。</span></div><b>LOCAL · NO ORDERS</b></section>
      {error && <div className="analyst-global-error"><AlertTriangle size={16} />{error}。保留最后一次已知证据，不生成新的确定性结论。</div>}
      <div className="analyst-workspace">
        <nav className="analyst-nav">
          {PAGES.map(({ id, label, icon: Icon }) => (
            <button type="button" className={page === id ? 'active' : ''} onClick={() => setPage(id)} key={id}>
              <Icon size={18} /><span>{label}</span>{navCount[id] ? <b>{navCount[id]}</b> : null}
            </button>
          ))}
        </nav>
        <section className="analyst-content">{pageContent}</section>
      </div>
      <footer className="analyst-footer"><span><Database size={13} />本地不可变研究证据</span><span><KeyRound size={13} />凭据：系统安全存储 · AI 证据：最小化后外发</span><span>行情源：Alpaca SIP 代理 · 模拟盘：无 · 订单：禁止</span></footer>
    </main>
  )
}
