import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Activity,
  AlertTriangle,
  ArrowLeftRight,
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
  executionErrorMessage,
  loadAssistantHistory,
  normalizeExecutionSnapshot,
  normalizePaperAutopilotSnapshot,
  paperAutopilotErrorMessage,
  orderPreviewCommand,
  saveAssistantHistory,
} from './client-state'

const bridge = window.analystDesktop

const PAGES = [
  { id: 'today', label: '选股', icon: Radar },
  { id: 'data', label: '数据', icon: Database },
  { id: 'execution', label: '交易', icon: ArrowLeftRight },
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

const currency = (value) => (
  value == null || !Number.isFinite(Number(value))
    ? 'N/A'
    : new Intl.NumberFormat('en-US', {
        style: 'currency',
        currency: 'USD',
        maximumFractionDigits: 2,
      }).format(Number(value))
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
            <small>用于历史日线、新闻和证券参考数据；盘前分钟历史由 Alpaca 代理在选股时按候选补齐并缓存。</small>
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
  return <div className="analyst-page-stack"><header className="analyst-page-header"><div><span className="eyebrow">LOCAL DATA PLANE</span><h2>数据与同步</h2></div><button type="button" disabled={Boolean(active)} onClick={onSyncData}><RefreshCw size={15} />同步缺失增量</button></header><div className="review-summary"><article><span>日线快照</span><strong>{inventory.grouped_daily || 0}</strong><small>安装包预置 + 增量</small></article><article><span>参考数据</span><strong>{inventory.reference || 0}</strong><small>安装包预置 + 增量</small></article><article><span>新闻分区</span><strong>{inventory.news || 0}</strong><small>安装包预置 + 增量</small></article><article><span>选股准备</span><strong>{inventory.ready_for_selection ? '已就绪' : '未就绪'}</strong><small>从最新日线续传，已有快照复用</small></article></div><section className="panel boundary-note"><Database size={15} /><span>盘前分钟历史不在存量包中；今日选股时只按锁定候选及最近 20 个历史会话下载，下载后保存在本机缓存。</span></section>{active?.action === 'sync_data' && <section className="panel workflow-progress-panel"><header><div><span className="eyebrow">SYNC PROGRESS</span><h3>正在同步数据</h3></div><strong>{(active.overall_progress_percent || 0).toFixed(1)}%</strong></header><div className="workflow-progress-track"><i style={{ width: `${active.overall_progress_percent || 0}%` }} /></div><div className="workflow-progress-meta"><span>{active.completed_steps}/{active.total_steps}</span><span>{active.current_step}</span><span>{active.step_progress_current || 0}/{active.step_progress_total || 0}</span><span>{active.progress_detail || ''}</span></div></section>}</div>
}

function AssistantPage({ desk, model, messages, setMessages }) {
  const currentEvidenceKey = evidenceReferenceKey(evidenceReferenceFromDesk(desk))
  const [question, setQuestion] = useState('')
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

function PaperAutopilotPanel({ snapshot, settings, onRefresh, onCommand }) {
  const paper = normalizePaperAutopilotSnapshot(snapshot)
  const [busy, setBusy] = useState(false)
  const [confirmation, setConfirmation] = useState('')
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')
  const phrase = paper.armConfirmationPhrase
    || `启用模拟盘自动执行 ${paper.accountMasked || '当前账户'}`

  const run = async (command, successMessage = '') => {
    setBusy(true)
    setError('')
    setMessage('')
    try {
      await onCommand(command)
      if (successMessage) setMessage(successMessage)
    } catch (caught) {
      setError(paperAutopilotErrorMessage(caught))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="panel execution-control-panel paper-autopilot-panel">
      <header className="analyst-panel-header"><div><span className="eyebrow">ISOLATED PAPER AUTOMATION</span><h3>IBKR 模拟盘自动执行</h3></div><span>固定端口 {paper.port}</span></header>
      <p>这条通道只接收今日冻结的执行计划与安全包；Agent 只能影响安全包，不能把自然语言直接变成订单。它与 4001 实盘手动执行完全隔离。</p>
      <div className="execution-status-grid" aria-live="polite">
        <article><span>模拟盘配置</span><strong>{paper.configured && settings?.paperExecution?.configured ? '已保存' : '未配置'}</strong><small>{paper.accountMasked || settings?.paperExecution?.paperAccountMasked || '需填写 DU 账户'}</small></article>
        <article><span>网关连接</span><strong>{paper.connected ? '已连接' : '未连接'}</strong><small>仅模拟盘 4002</small></article>
        <article><span>冻结计划</span><strong>{paper.planStatus === 'valid' ? '已校验' : '未就绪'}</strong><small>{paper.planError || '必须是今天的计划和安全包'}</small></article>
        <article><span>自动执行</span><strong className={paper.running ? 'danger-text' : ''}>{paper.running ? '运行中' : '关闭'}</strong><small>{paper.lastTickAtUtc ? `最近轮询 ${localTime(paper.lastTickAtUtc)}` : '重启后默认关闭'}</small></article>
      </div>
      {paper.lastError && <div className="analyst-warning" role="alert"><AlertTriangle size={16} />{paperAutopilotErrorMessage(paper.lastError)}</div>}
      {error && <div className="analyst-warning" role="alert"><AlertTriangle size={16} />{error}</div>}
      <div className="execution-arm-control">
        <div><strong>自动模拟盘总开关</strong><small>启动前必须连接、校验当日冻结计划、通过 What-If；停止只禁止后续决策，不会撤销已提交的保护性订单。</small></div>
        {!paper.running && <div className="execution-form-actions"><button type="button" disabled={busy || !settings?.paperExecution?.configured} onClick={() => run(paper.connected ? { kind: 'disconnect' } : { kind: 'connect' }, paper.connected ? '模拟盘连接已关闭。' : '模拟盘已连接；尚未启用自动下单。')}>{paper.connected ? '关闭模拟盘连接' : '连接模拟盘'}</button><button type="button" disabled={busy || !paper.connected} onClick={() => run({ kind: 'validate_plan' }, '冻结计划与安全包校验完成。')}>校验今日冻结计划</button></div>}
        {paper.running ? <button type="button" className="danger-button" disabled={busy} onClick={() => run({ kind: 'stop' }, '自动模拟盘已停止；请在 IBKR 中核对未完成的保护性订单。')}>停止自动模拟盘</button> : <div className="paper-arm-row"><label><span>输入“{phrase}”</span><input value={confirmation} onChange={(event) => setConfirmation(event.target.value)} autoComplete="off" /></label><button type="button" className="danger-button" disabled={busy || !paper.connected || paper.planStatus !== 'valid' || confirmation !== phrase} onClick={() => run({ kind: 'start', confirmation }, '自动模拟盘已启动：仅会执行冻结计划中的标的与风控规则。')}>启动自动模拟盘</button></div>}
      </div>
      {paper.lastOutcomes.length > 0 && <div className="execution-ledger"><header className="analyst-panel-header"><h3>最近自动决策</h3><span>{paper.lastOutcomes.length} 条</span></header>{paper.lastOutcomes.map((outcome) => <p key={`${outcome.planId}-${outcome.symbol}`}><strong>{outcome.symbol || 'N/A'} · {outcome.action || 'N/A'}</strong> {outcome.reasons.join('；') || '无补充原因'}{outcome.degradedReasons.length > 0 ? ` · 降级：${outcome.degradedReasons.join('；')}` : ''}</p>)}</div>}
      {message && <p className="execution-status-message" aria-live="polite">{message}</p>}
      <button type="button" className="subtle-button" disabled={busy} onClick={onRefresh}><RefreshCw size={14} />刷新模拟盘状态</button>
    </section>
  )
}

function ExecutionPage({ snapshot, paperSnapshot, settings, error, onRefresh, onCommand, onPaperRefresh, onPaperCommand }) {
  const execution = normalizeExecutionSnapshot(snapshot)
  const [busy, setBusy] = useState(false)
  const [localError, setLocalError] = useState('')
  const [statusMessage, setStatusMessage] = useState('')
  const [bindingConfirmation, setBindingConfirmation] = useState('')
  const [armConfirmation, setArmConfirmation] = useState('')
  const [draft, setDraft] = useState({
    symbol: '',
    action: 'OpenLong',
    quantity: '',
    limitPrice: '',
  })
  const [preview, setPreview] = useState(null)
  const [submitConfirmation, setSubmitConfirmation] = useState('')
  const [warningAcknowledged, setWarningAcknowledged] = useState(false)
  const armPhrase = execution.armConfirmationPhrase
    || `启用实盘 ${execution.accountMasked || '当前账户'}`
  const bindingPhrase = execution.bindingConfirmationPhrase
  const executionError = localError
    || (error ? executionErrorMessage(error) : '')
    || (execution.lastError ? executionErrorMessage(execution.lastError) : '')

  const runCommand = async (command, successMessage = '') => {
    setBusy(true)
    setLocalError('')
    setStatusMessage('')
    try {
      const result = await onCommand(command)
      if (successMessage) setStatusMessage(successMessage)
      return result
    } catch (caught) {
      setLocalError(executionErrorMessage(caught))
      return null
    } finally {
      setBusy(false)
    }
  }

  const toggleConnection = async () => {
    setPreview(null)
    setBindingConfirmation('')
    setArmConfirmation('')
    setSubmitConfirmation('')
    setWarningAcknowledged(false)
    const command = execution.enabled
      ? { kind: 'disconnect' }
      : { kind: 'connect' }
    await runCommand(
      command,
      execution.enabled
        ? '实盘执行已关闭；仅禁止新单，不会撤销 IBKR 已有挂单，请继续在最近订单中核对状态。'
        : '实盘执行已开启连接；已连接不等于可以下单。',
    )
  }

  const bindAccount = async () => {
    if (!bindingPhrase || bindingConfirmation !== bindingPhrase) {
      setLocalError('账户绑定确认文字不匹配，未保存任何账户信息。')
      return
    }
    const result = await runCommand({
      kind: 'bind_account',
      confirmation: bindingConfirmation,
    })
    if (!result) return
    setBindingConfirmation('')
    setStatusMessage('实盘账户已安全绑定；连接已自动关闭，请重新开启后再单独解锁下单权限。')
  }

  const toggleArm = async () => {
    if (execution.writesArmed) {
      await runCommand({ kind: 'disarm' }, '手动下单权限已关闭。')
      setArmConfirmation('')
      setPreview(null)
      return
    }
    if (armConfirmation !== armPhrase) {
      setLocalError('实盘确认文字不匹配，未启用下单权限。')
      return
    }
    await runCommand(
      { kind: 'arm', confirmation: armConfirmation },
      '实盘手动下单权限已临时启用。',
    )
  }

  const recoverOrders = async () => {
    const result = await runCommand({ kind: 'recover' })
    if (!result) return
    const count = Number.isFinite(Number(result.reconciled_count))
      ? `，已核对 ${Number(result.reconciled_count)} 笔`
      : ''
    setStatusMessage(`券商订单状态核对已完成${count}；请查看“最近提交订单”和恢复状态。`)
  }

  const updateDraft = (key, value) => {
    setDraft((current) => ({ ...current, [key]: value }))
    setPreview(null)
    setSubmitConfirmation('')
    setWarningAcknowledged(false)
  }

  const createPreview = async (event) => {
    event.preventDefault()
    let command
    try {
      const id = globalThis.crypto?.randomUUID?.() || `${Date.now()}`
      command = orderPreviewCommand(draft, `desktop-${id}`)
    } catch (caught) {
      setLocalError(caught.message)
      return
    }
    const result = await runCommand(command)
    const nextPreview = result?.preview || result
    if (nextPreview?.preview_id) {
      setPreview({ ...nextPreview, order: command.order })
      setWarningAcknowledged(false)
      setStatusMessage('订单预览已生成，尚未提交。')
    } else if (result) {
      setLocalError('执行内核没有返回有效的订单预览。')
    }
  }

  const submitOrder = async () => {
    if (!preview?.preview_id) return
    const result = await runCommand({
      kind: 'submit',
      preview_id: preview.preview_id,
      confirmation: submitConfirmation,
      order: preview.order,
    })
    if (!result) return
    setStatusMessage(
      result.status === 'unknown'
        ? '订单状态未知，禁止重复提交；请先完成券商对账。'
        : `订单已交给 IBKR：${result.broker_order_id || result.status || '已受理'}`,
    )
    setPreview(null)
    setSubmitConfirmation('')
    setWarningAcknowledged(false)
  }

  return (
    <div className="analyst-page-stack execution-page">
      <header className="analyst-page-header">
        <div><span className="eyebrow">ISOLATED MANUAL EXECUTION</span><h2>盈透手动执行台</h2></div>
        <button type="button" disabled={busy} onClick={onRefresh}><RefreshCw size={15} />刷新账户</button>
      </header>
      <div className="execution-safety-note">
        <ShieldCheck size={17} />
        <div><strong>研究内核永远不能下单</strong><span>这里只接受你手动输入的 DAY 限价单；仅允许开多和减多，不支持卖空。</span></div>
      </div>
      {execution.enabled && <div className="execution-live-warning" role="alert"><AlertTriangle size={17} /><strong>实盘执行已开启（固定端口 4001）</strong><span>已连接 ≠ 可下单；仍需单独解锁、预览并输入动态确认文字。重启后总开关默认关闭。</span></div>}
      {execution.recoveryRequired && <div className="analyst-warning execution-recovery-warning" role="alert"><AlertTriangle size={16} /><span>存在状态未知的实盘订单；核对完成前禁止提交新订单。</span><button type="button" disabled={busy || !execution.connected} onClick={recoverOrders}>核对未知订单</button></div>}
      {executionError && <div className="analyst-warning" role="alert"><AlertTriangle size={16} />{executionError}</div>}
      <div className="execution-status-grid" aria-live="polite">
        <article><span>实盘执行</span><strong className={execution.enabled ? 'danger-text' : ''}>{execution.enabled ? '已开启' : '已关闭'}</strong><small>固定端口 {execution.port}</small></article>
        <article><span>IBKR 连接</span><strong>{execution.connected ? '已连接' : '未连接'}</strong><small>{execution.accountMasked || '等待连接后自动识别账户'}</small></article>
        <article><span>账户绑定</span><strong>{execution.accountBound ? '已绑定' : '未绑定'}</strong><small>{execution.accountBound ? '后续连接将严格核对账户' : '绑定前禁止解锁和下单'}</small></article>
        <article><span>API 写入配置</span><strong>{execution.apiReadOnly == null ? '未知' : execution.apiReadOnly ? '只读' : '请求写入'}</strong><small>未实际探测；What-If / 提交时由 IBKR 验证</small></article>
        <article><span>手动下单</span><strong>{execution.writesArmed ? '已临时启用' : '关闭'}</strong><small>断开连接或重启后自动关闭</small></article>
      </div>
      <section className="panel execution-control-panel">
        <div className="execution-master-control"><div><strong>实盘执行总开关</strong><small>关闭只会断开连接并禁止新单，不会撤销 IBKR 已有挂单；最近订单记录仍会保留。</small></div><button type="button" role="switch" aria-checked={execution.enabled} className={execution.enabled ? 'danger-button' : ''} disabled={busy || (!execution.enabled && !settings?.execution?.configured)} onClick={toggleConnection}>{execution.enabled ? '关闭实盘执行' : '开启实盘执行'}</button></div>
        {execution.enabled && execution.connected && !execution.accountBound && <div className="execution-binding-control">
          <div><strong>首次绑定实盘账户</strong><small>账户由 TWS 自动检测。客户端只显示脱敏值；完整账户 ID 仅由主进程加密保存。</small></div>
          <label><span>输入“{bindingPhrase || '等待 TWS 返回确认文字'}”</span><input value={bindingConfirmation} onChange={(event) => setBindingConfirmation(event.target.value)} autoComplete="off" /></label>
          <button type="button" className="danger-button" disabled={busy || !bindingPhrase || bindingConfirmation !== bindingPhrase} onClick={bindAccount}>确认绑定此实盘账户</button>
        </div>}
        <div className="execution-arm-control">
          <div><strong>写权限闸门</strong><small>研究选股和 Agent 无法触发；必须由你输入动态文字解锁。</small></div>
          {!execution.writesArmed && <label><span>输入“{armPhrase}”</span><input value={armConfirmation} onChange={(event) => setArmConfirmation(event.target.value)} autoComplete="off" /></label>}
          <button type="button" className={execution.writesArmed ? 'danger-button' : ''} disabled={busy || !execution.enabled || !execution.connected || !execution.accountBound || execution.apiReadOnly !== false || execution.recoveryRequired || (!execution.writesArmed && armConfirmation !== armPhrase)} onClick={toggleArm}>
            {execution.writesArmed ? '立即关闭下单权限' : '确认启用实盘下单'}
          </button>
        </div>
      </section>
      <section className="panel execution-order-panel">
        <header className="analyst-panel-header"><div><span className="eyebrow">LIMIT DAY ORDER</span><h3>手动订单</h3></div><span>单笔最大新增开仓金额（仅 OpenLong） {currency(execution.maxOrderNotional || settings?.execution?.maxOrderNotional)}</span></header>
        <form className="execution-order-form" onSubmit={createPreview}>
          <label><span>股票代码</span><input value={draft.symbol} onChange={(event) => updateDraft('symbol', event.target.value.toUpperCase())} placeholder="AAPL" autoComplete="off" required /></label>
          <label><span>操作</span><select value={draft.action} onChange={(event) => updateDraft('action', event.target.value)}><option value="OpenLong">开多</option><option value="ReduceLong">减多 / 平多</option></select></label>
          <label><span>股数</span><input type="number" min="1" step="1" value={draft.quantity} onChange={(event) => updateDraft('quantity', event.target.value)} required /></label>
          <label><span>限价（USD）</span><input type="number" min="0.01" step="0.01" value={draft.limitPrice} onChange={(event) => updateDraft('limitPrice', event.target.value)} required /></label>
          <button type="submit" disabled={busy || !execution.canPreview}>生成预览</button>
        </form>
        {preview && <div className="execution-preview" aria-live="polite">
          <div><span>待提交</span><strong>{preview.order?.symbol || draft.symbol} · {preview.order?.action || draft.action} · {preview.order?.quantity || draft.quantity} 股 @ {currency(preview.order?.limit_price || draft.limitPrice)}</strong><small>预览编号 {preview.preview_id}</small></div>
          <div className="execution-what-if"><span>IBKR What-If</span><strong>预计佣金 {currency(preview.what_if?.estimated_commission)} · 初始保证金变化 {currency(preview.what_if?.initial_margin_change)}</strong>{preview.what_if?.warning && <p role="alert">IBKR 警告：{preview.what_if.warning}</p>}{preview.warning_confirmation_hash && <small>警告确认码 {preview.warning_confirmation_hash} 已写入下方动态确认短语；提交时 IBKR 警告若发生变化，订单会被拒绝并要求重新预览。</small>}{preview.what_if?.warning && <label className="execution-warning-ack"><input type="checkbox" checked={warningAcknowledged} onChange={(event) => setWarningAcknowledged(event.target.checked)} /><span>我已阅读并接受上述 IBKR 警告</span></label>}</div>
          <label><span>输入“{preview.confirmation_phrase}”确认提交</span><input value={submitConfirmation} onChange={(event) => setSubmitConfirmation(event.target.value)} autoComplete="off" /></label>
          <button type="button" disabled={busy || !execution.canPreview || !preview.confirmation_phrase || submitConfirmation !== preview.confirmation_phrase || (Boolean(preview.what_if?.warning) && !warningAcknowledged)} onClick={submitOrder}>提交到实盘</button>
        </div>}
        {statusMessage && <p className="execution-status-message" aria-live="polite">{statusMessage}</p>}
      </section>
      <div className="execution-ledger-grid">
        <section className="panel execution-ledger"><header className="analyst-panel-header"><h3>当前持仓</h3><span>{execution.positions.length} 项</span></header><div className="execution-table-wrap"><table><thead><tr><th>标的</th><th>股数</th><th>成本</th><th>现价</th><th>浮盈亏</th></tr></thead><tbody>{execution.positions.map((position) => <tr key={position.symbol}><td>{position.symbol || 'N/A'}</td><td>{position.quantity ?? 'N/A'}</td><td>{currency(position.average_cost)}</td><td>{currency(position.market_price)}</td><td>{percent(position.unrealized_pnl_percent)}</td></tr>)}</tbody></table>{!execution.positions.length && <p className="table-empty">暂无持仓。</p>}</div></section>
        <section className="panel execution-ledger"><header className="analyst-panel-header"><h3>未完成订单</h3><span>{execution.openOrders.length} 笔</span></header><div className="execution-table-wrap"><table><thead><tr><th>标的</th><th>操作</th><th>数量</th><th>限价</th><th>状态</th></tr></thead><tbody>{execution.openOrders.map((order) => <tr key={order.order_id || order.client_order_id}><td>{order.symbol || 'N/A'}</td><td>{order.action || order.side || 'N/A'}</td><td>{order.quantity ?? 'N/A'}</td><td>{currency(order.limit_price)}</td><td>{order.status || 'N/A'}</td></tr>)}</tbody></table>{!execution.openOrders.length && <p className="table-empty">暂无未完成订单。</p>}</div></section>
      </div>
      <section className="panel execution-ledger execution-recent-orders">
        <header className="analyst-panel-header"><div><h3>最近提交订单</h3><small>包括已成交、已取消及瞬时离开挂单列表的订单，避免重复提交。</small></div><span>账户数据刷新：{localTime(execution.accountRefreshedAtUtc)}</span></header>
        <div className="execution-table-wrap"><table><thead><tr><th>更新时间</th><th>标的</th><th>方向</th><th>数量</th><th>限价</th><th>状态</th><th>券商 ID</th></tr></thead><tbody>{execution.recentOrders.map((order) => <tr key={order.client_order_id || `${order.broker_order_id}-${order.updated_at_utc}`}><td>{localTime(order.updated_at_utc)}</td><td>{order.symbol || 'N/A'}</td><td>{order.action || 'N/A'}</td><td>{order.quantity ?? 'N/A'}</td><td>{currency(order.limit_price)}</td><td>{order.status || 'N/A'}</td><td>{order.broker_order_id ?? order.perm_id ?? 'N/A'}</td></tr>)}</tbody></table>{!execution.recentOrders.length && <p className="table-empty">暂无本机提交记录。</p>}</div>
      </section>
      <PaperAutopilotPanel snapshot={paperSnapshot} settings={settings} onRefresh={onPaperRefresh} onCommand={onPaperCommand} />
    </div>
  )
}

function ExecutionSettingsForm({ settings, busy, error, onImport, onSave, onClearBinding }) {
  const configured = settings.execution?.configured === true
  const accountBound = settings.execution?.accountBound === true
  const [importing, setImporting] = useState(false)
  const [importResult, setImportResult] = useState('')
  const [importError, setImportError] = useState('')
  const [values, setValues] = useState({
    host: '',
    clientId: '',
    maxOrderNotional: settings.execution?.maxOrderNotional || 25000,
  })
  const update = (key, value) => setValues((current) => ({ ...current, [key]: value }))
  const importProfile = async () => {
    setImporting(true)
    setImportError('')
    setImportResult('')
    try {
      const result = await onImport()
      if (result?.canceled) return
      const profile = result?.profile || {}
      setImportResult(`已确认固定实盘端口 ${profile.livePort}；登录名、密码及虚拟盘字段均已忽略。账户 ID 将在首次连接后由 TWS 自动检测。`)
    } catch (caught) {
      setImportError(caught.message || '无法导入盈透配置文件')
    } finally {
      setImporting(false)
    }
  }
  return (
    <section className="panel execution-settings-panel">
      <header className="analyst-panel-header"><div><span className="eyebrow">IBKR TWS API</span><h3>盈透执行连接</h3></div><span>不需要、也不会保存盈透用户名或密码</span></header>
      <div className="execution-import-row"><button type="button" disabled={busy || importing} onClick={importProfile}>{importing ? <LoaderCircle className="spin" size={15} /> : <Database size={15} />}从 quan.env / 文本导入</button><span>只确认固定实盘端口 4001；登录名不是账户 ID，因此不会导入，密码与虚拟盘字段也会直接忽略。</span></div>
      <form onSubmit={(event) => { event.preventDefault(); onSave(values) }}>
        <label><span>主机</span><input value={values.host} onChange={(event) => update('host', event.target.value)} placeholder={settings.execution?.hostConfigured ? '已安全保存；留空保持不变' : '127.0.0.1'} required={!configured} /></label>
        <label><span>Client ID</span><input type="number" min="0" step="1" value={values.clientId} onChange={(event) => update('clientId', event.target.value)} placeholder={settings.execution?.clientIdConfigured ? '已安全保存；留空保持不变' : '17'} required={!configured} /></label>
        <div className="execution-bound-account"><span>安全绑定账户</span><strong>{accountBound ? settings.execution?.liveAccountMasked : '尚未绑定'}</strong><small>{accountBound ? '连接时将严格核对此账户' : '保存连接配置后，在交易页首次连接并确认自动检测结果'}</small></div>
        <label><span>单笔最大新增开仓金额（仅 OpenLong，USD）</span><input type="number" min="1" step="1" value={values.maxOrderNotional} onChange={(event) => update('maxOrderNotional', event.target.value)} required /></label>
        <button type="submit" disabled={busy}>{busy ? <LoaderCircle className="spin" size={15} /> : <Save size={15} />}{configured ? '更新执行配置' : '保存执行配置'}</button>
      </form>
      {accountBound && <button type="button" className="secondary-danger" disabled={busy} onClick={onClearBinding}>清除账户绑定并重新检测</button>}
      {(error || importError) && <div className="inline-error" role="alert"><AlertTriangle size={15} />{importError || error}</div>}
      {importResult && <div className="execution-import-result" aria-live="polite">{importResult}</div>}
      <p>只连接实盘端口 4001，不提供模拟盘切换。请在 TWS 或 IB Gateway 中启用 Socket API；交易密码只用于登录官方客户端，不能填写到这里。</p>
    </section>
  )
}

function PaperExecutionSettingsForm({ settings, busy, error, onSave }) {
  const configured = settings.paperExecution?.configured === true
  const [values, setValues] = useState({ host: '', clientId: '', paperAccount: '' })
  const update = (key, value) => setValues((current) => ({ ...current, [key]: value }))
  return (
    <section className="panel execution-settings-panel paper-autopilot-settings">
      <header className="analyst-panel-header"><div><span className="eyebrow">IBKR PAPER AUTO EXECUTION</span><h3>盈透模拟盘自动执行连接</h3></div><span>固定端口 4002</span></header>
      <p>这是与实盘 4001 分开的连接。只接受 DU 开头的模拟账户；不保存盈透登录名或密码，登录仍由 TWS / IB Gateway 完成。</p>
      <form onSubmit={(event) => { event.preventDefault(); onSave(values) }}>
        <label><span>模拟网关主机</span><input value={values.host} onChange={(event) => update('host', event.target.value)} placeholder={configured ? '已安全保存；仍需填写以修改' : '127.0.0.1'} required /></label>
        <label><span>模拟盘 Client ID</span><input type="number" min="0" step="1" value={values.clientId} onChange={(event) => update('clientId', event.target.value)} placeholder={configured ? '已安全保存；仍需填写以修改' : '91'} required /></label>
        <label><span>模拟账户 ID</span><input value={values.paperAccount} onChange={(event) => update('paperAccount', event.target.value.toUpperCase())} placeholder={settings.paperExecution?.paperAccountMasked || 'DU1234567'} autoComplete="off" required /></label>
        <button type="submit" disabled={busy}>{busy ? <LoaderCircle className="spin" size={15} /> : <Save size={15} />}{configured ? '更新模拟盘配置' : '保存模拟盘配置'}</button>
      </form>
      {error && <div className="inline-error" role="alert"><AlertTriangle size={15} />{error}</div>}
      <p>保存后请到“交易”页面先连接，再校验今日冻结计划，最后输入动态文字启动自动模拟盘。任何一步失败都不会提交订单。</p>
    </section>
  )
}

function SettingsPage({ settings, models, runtimeStatus, workflowStatus, executionSnapshot, paperSnapshot, busy, error, onLoadModels, onSaveModels, onImportExecution, onSaveExecution, onSavePaperExecution, onClearExecutionBinding, onReset }) {
  const [editingModels, setEditingModels] = useState(false)
  const execution = normalizeExecutionSnapshot(executionSnapshot)
  return (
    <div className="analyst-page-stack">
      <header className="analyst-page-header"><div><span className="eyebrow">SECURITY & PROVIDERS</span><h2>设置</h2></div><p>凭据不显示、不导出、不进入 Git；AI 请求会发送最小化研究证据至 OpenRouter</p></header>
      <div className="settings-grid">
        <section className="panel settings-card"><KeyRound size={20} /><h3>安全凭据</h3><div><span>OpenRouter</span><strong>{settings.openRouterKeyConfigured ? '已安全保存' : '未配置'}</strong></div><div><span>Massive</span><strong>{settings.massiveConfigured ? '已安全保存' : '未配置'}</strong></div><div><span>Alpaca SIP</span><strong>{settings.marketDataConfigured ? '已安全保存' : '未配置'}</strong></div><div><span>SEC</span><strong>{settings.secConfigured ? '已安全保存' : '未配置'}</strong></div></section>
        <section className="panel settings-card"><ShieldCheck size={20} /><h3>数据源状态</h3><div><span>执行位置</span><strong>{runtimeStatus?.local_execution ? '本机' : '不可用'}</strong></div><div><span>实时行情</span><strong>{runtimeStatus?.market_data?.realtime_ready ? 'Alpaca SIP 已连接' : '不可用'}</strong></div><div><span>Massive 日线</span><strong>{workflowStatus?.data_inventory?.grouped_daily || 0} 个快照</strong></div><div><span>选股准备</span><strong>{workflowStatus?.data_inventory?.ready_for_selection ? '已就绪' : '等待首次同步'}</strong></div></section>
        <section className="panel settings-card"><Clock3 size={20} /><h3>IBKR 实盘执行</h3><div><span>连接配置</span><strong>{settings.execution?.configured ? '已安全保存' : '未配置'}</strong></div><div><span>账户绑定</span><strong>{settings.execution?.accountBound ? settings.execution?.liveAccountMasked : '未绑定'}</strong></div><div><span>总开关</span><strong>{execution.enabled ? '开启' : '关闭'}</strong></div><div><span>连接</span><strong>{execution.connected ? '已连接' : '未连接'}</strong></div><div><span>下单</span><strong>{execution.writesArmed ? '临时启用' : '关闭'}</strong></div></section>
        <section className="panel settings-card"><Bot size={20} /><h3>IBKR 模拟盘自动执行</h3><div><span>连接配置</span><strong>{settings.paperExecution?.configured ? '已安全保存' : '未配置'}</strong></div><div><span>账户</span><strong>{settings.paperExecution?.paperAccountMasked || '未配置'}</strong></div><div><span>端口</span><strong>4002</strong></div><div><span>状态</span><strong>{normalizePaperAutopilotSnapshot(paperSnapshot).running ? '运行中' : '关闭'}</strong></div></section>
      </div>
      <ExecutionSettingsForm settings={settings} busy={busy} error={error} onImport={onImportExecution} onSave={onSaveExecution} onClearBinding={onClearExecutionBinding} />
      <PaperExecutionSettingsForm settings={settings} busy={busy} error={error} onSave={onSavePaperExecution} />
      {!editingModels ? (
        <section className="panel current-models">
          <header className="analyst-panel-header"><div><span className="eyebrow">MODEL ROUTING</span><h3>当前模型</h3></div><button type="button" onClick={async () => { await onLoadModels(); setEditingModels(true) }}>更改模型</button></header>
          {Object.entries(MODEL_LABELS).map(([key, label]) => <div key={key}><span>{label}</span><strong>{settings.models?.[key] || '未配置'}</strong></div>)}
        </section>
      ) : (
        <ModelSetup models={models} initialValues={settings.models} busy={busy} error={error} embedded onSave={async (values) => { if (await onSaveModels(values)) setEditingModels(false) }} />
      )}
      <section className="panel reset-zone"><div><h3>重新配置连接</h3><p>清除本机安全存储中的 OpenRouter、行情、IBKR 执行配置和模型选择。</p></div><button type="button" onClick={onReset}>清除并重新配置</button></section>
    </div>
  )
}

export default function AnalystApp() {
  const [settings, setSettings] = useState(null)
  const [models, setModels] = useState([])
  const [desk, setDesk] = useState(null)
  const [executionSnapshot, setExecutionSnapshot] = useState(null)
  const [paperAutopilotSnapshot, setPaperAutopilotSnapshot] = useState(null)
  const [executionError, setExecutionError] = useState('')
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
  const [assistantHistory, setAssistantHistory] = useState(
    () => loadAssistantHistory(),
  )
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
  useEffect(() => {
    saveAssistantHistory(assistantHistory)
  }, [assistantHistory])

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

  const refreshExecution = useCallback(async () => {
    try {
      const snapshot = await bridge.execution.snapshot()
      setExecutionSnapshot(snapshot)
      setExecutionError('')
      return snapshot
    } catch (caught) {
      setExecutionError(caught.message || '盈透执行服务不可用')
      return null
    }
  }, [])

  const refreshPaperAutopilot = useCallback(async () => {
    try {
      const snapshot = await bridge.paperAutopilot.snapshot()
      setPaperAutopilotSnapshot(snapshot)
      return snapshot
    } catch (caught) {
      setActionError(caught.message || 'IBKR 模拟盘自动执行服务不可用')
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
          await Promise.all([
            refreshDesk(),
            refreshWorkflows(),
            refreshExecution(),
            refreshPaperAutopilot(),
          ])
        }
      } catch (caught) {
        if (!cancelled) setActionError(caught.message || '客户端初始化失败')
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    initialize()
    return () => { cancelled = true }
  }, [refreshDesk, refreshExecution, refreshPaperAutopilot, refreshWorkflows])

  useEffect(() => {
    if (!settings?.configured || onboarding) return undefined
    const timer = window.setInterval(() => {
      refreshDesk()
      refreshRuntime()
      refreshWorkflows()
      refreshExecution()
      refreshPaperAutopilot()
    }, 5_000)
    return () => window.clearInterval(timer)
  }, [onboarding, refreshDesk, refreshExecution, refreshPaperAutopilot, refreshRuntime, refreshWorkflows, settings?.configured])

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
        refreshExecution(),
        refreshPaperAutopilot(),
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
    if (!window.confirm('确认清除本机安全存储中的模型、行情与 IBKR 执行配置？')) return
    setBusy(true)
    setActionError('')
    try {
      const next = await bridge.settings.clear()
      setSettings(next)
      setDesk(null)
      setModels([])
      setRuntimeStatus(null)
      setWorkflowStatus(null)
      setExecutionSnapshot(null)
      setPaperAutopilotSnapshot(null)
      setExecutionError('')
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

  const saveExecution = async (values) => {
    setBusy(true)
    setActionError('')
    try {
      const result = await bridge.settings.saveExecution(values)
      setSettings(result.settings)
      setRuntimeStatus(result.runtime)
      setExecutionSnapshot(result.execution)
      setExecutionError('')
      return true
    } catch (caught) {
      setActionError(caught.message || '盈透执行配置保存失败')
      return false
    } finally {
      setBusy(false)
    }
  }

  const savePaperExecution = async (values) => {
    setBusy(true)
    setActionError('')
    try {
      const result = await bridge.settings.savePaperExecution(values)
      setSettings(result.settings)
      setRuntimeStatus(result.runtime)
      setPaperAutopilotSnapshot(result.paper_autopilot)
      return true
    } catch (caught) {
      setActionError(caught.message || '盈透模拟盘配置保存失败')
      return false
    } finally {
      setBusy(false)
    }
  }

  const clearExecutionBinding = async () => {
    if (!window.confirm('确认清除当前实盘账户绑定？这会立即断开执行连接并禁止新单，但不会撤销 IBKR 已有挂单；下次连接需重新检测并确认账户。')) return false
    setBusy(true)
    setActionError('')
    try {
      const result = await bridge.settings.clearExecutionAccountBinding()
      setSettings(result.settings)
      setRuntimeStatus(result.runtime)
      setExecutionSnapshot(result.execution)
      setExecutionError('')
      return true
    } catch (caught) {
      setActionError(caught.message || '无法清除实盘账户绑定')
      return false
    } finally {
      setBusy(false)
    }
  }

  const sendExecutionCommand = async (command) => {
    const result = await bridge.execution.command(command)
    if (result?.settings) setSettings(result.settings)
    if (result?.execution) setExecutionSnapshot(result.execution)
    else await refreshExecution()
    return result
  }

  const sendPaperAutopilotCommand = async (command) => {
    const result = await bridge.paperAutopilot.command(command)
    setPaperAutopilotSnapshot(result)
    return result
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
      : page === 'execution'
        ? <ExecutionPage snapshot={executionSnapshot} paperSnapshot={paperAutopilotSnapshot} settings={settings} error={executionError} onRefresh={refreshExecution} onCommand={sendExecutionCommand} onPaperRefresh={refreshPaperAutopilot} onPaperCommand={sendPaperAutopilotCommand} />
        : page === 'assistant'
          ? <AssistantPage desk={desk} model={settings.models.question} messages={assistantHistory} setMessages={setAssistantHistory} />
          : page === 'review'
            ? <ReviewPage desk={desk} workflowStatus={workflowStatus} onRunReview={() => startWorkflow('run_review')} />
            : page === 'agents'
              ? <AgentsPage settings={settings} desk={desk} />
              : <SettingsPage settings={settings} models={models} runtimeStatus={runtimeStatus} workflowStatus={workflowStatus} executionSnapshot={executionSnapshot} paperSnapshot={paperAutopilotSnapshot} busy={busy} error={actionError} onLoadModels={loadModels} onSaveModels={saveModels} onImportExecution={() => bridge.settings.importExecutionProfile()} onSaveExecution={saveExecution} onSavePaperExecution={savePaperExecution} onClearExecutionBinding={clearExecutionBinding} onReset={reset} />

  return (
    <main className="analyst-app">
      <header className="analyst-topbar">
        <div className="analyst-brand"><span><Activity size={21} /></span><div><strong>AI 量化研究台</strong><small>DESKTOP RESEARCH EDITION</small></div></div>
        <div className="analyst-connection"><i className={connectionError ? 'bad' : ''} /><span>{connectionError ? '本地研究服务异常' : '本地研究内核在线'}</span></div>
        <button type="button" onClick={() => Promise.all([refreshDesk(), refreshRuntime(), refreshWorkflows(), refreshExecution(), refreshPaperAutopilot()])}><RefreshCw size={15} />刷新全部</button>
      </header>
      <div className="analyst-boundary-stack">
        <section className="analyst-boundary"><ShieldCheck size={17} /><div><strong>研究内核 · orders_authorized=false</strong><span>选股、因子、复盘和 Agent 永远只读，不能调用订单接口。</span></div><b>RESEARCH ONLY</b></section>
        <section className={`analyst-boundary manual ${normalizeExecutionSnapshot(executionSnapshot).enabled ? 'live' : ''}`}><ArrowLeftRight size={17} /><div><strong>手动执行台 · 仅实盘 4001</strong><span>隔离的人工订单入口；实盘总开关与写权限分离，重启默认全部关闭。</span></div><b>{normalizeExecutionSnapshot(executionSnapshot).writesArmed ? 'ARMED' : normalizeExecutionSnapshot(executionSnapshot).enabled ? 'CONNECTED' : 'OFF'}</b></section>
        <section className={`analyst-boundary paper ${normalizePaperAutopilotSnapshot(paperAutopilotSnapshot).running ? 'live' : ''}`}><Bot size={17} /><div><strong>自动模拟盘 · 仅 Paper 4002</strong><span>仅执行今日冻结计划；重启默认停止，实盘 4001 永远不受此开关影响。</span></div><b>{normalizePaperAutopilotSnapshot(paperAutopilotSnapshot).running ? 'RUNNING' : normalizePaperAutopilotSnapshot(paperAutopilotSnapshot).connected ? 'CONNECTED' : 'OFF'}</b></section>
      </div>
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
      <footer className="analyst-footer"><span><Database size={13} />本地不可变研究证据</span><span><KeyRound size={13} />凭据：系统安全存储 · AI 证据：最小化后外发</span><span>研究内核：只读 · 实盘执行：{normalizeExecutionSnapshot(executionSnapshot).enabled ? '连接开启' : '关闭'} / 下单{normalizeExecutionSnapshot(executionSnapshot).writesArmed ? '已解锁' : '锁定'}</span></footer>
    </main>
  )
}
