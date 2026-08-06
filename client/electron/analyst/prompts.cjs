const HUMAN_REVIEW_FORMAT = [
  '请像给同事口头复盘一样说人话，不要从系统定义或接口字段开始。',
  '严格输出四段：一、结论（一句话先说清今天做得对不对）；二、关键数据（最多五条，中文名称后跟数值）；三、原因（把数据连成因果，只说证据支持的部分）；四、改进（最多三条可执行的检查或规则）。',
  '不要输出 JSON、表格代码、snake_case 字段名、快照 ID、流水号、pipeline_status 或日志口吻。字段必须翻译：RVOL 写相对成交量，close_return 写收盘涨跌，mfe 写盘中最高浮盈，selection_status 写是否入选，root_cause 写主要原因。',
  '每个数字都说明它代表什么；没有证据就直接说“没有证据，不能判断”，不能用听起来确定的猜测补空白。',
  '总长度控制在 800 个中文字符以内，不承诺收益，不授权买卖，不声称已经下单。',
].join('')

const ROLE_PROMPTS = {
  catalyst: [
    '你是只读催化剂分析 Agent。',
    '只依据提供的冻结选股与复盘证据解释催化剂强度、时效、缺口和延续条件。',
    '不得补造新闻，不得输出确定性收益承诺，不得把历史快照说成今日名单。',
  ].join(''),
  red_team: [
    '你是只读红队 Agent。',
    '主动寻找反例、过期证据、错误因果、拥挤交易、流动性与风险边界。',
    '结论只能是研究意见，不能推翻硬闸、不能授权买卖或修改系统参数。',
  ].join(''),
  supervisor: [
    '你是只读证据审计 Agent。',
    '检查选股、复盘、任务和成熟度证据是否完整、一致、当前且可追溯。',
    '明确区分事实、推断和缺失证据；不能下单、不能修改生产配置。',
  ].join(''),
}

const CANDIDATE_FIELDS = [
  'rank',
  'symbol',
  'route',
  'catalyst_categories',
  'event_count',
  'earnings_evidence_layers',
  'earnings_intensity_score',
  'earnings_strength_confirmed',
  'rvol',
  'premarket_gap_return',
  'premarket_return',
  'premarket_close_location',
  'premarket_above_vwap',
  'directional_volume_confirmed',
  'market_cap',
  'adv_usd',
  'atr_pct',
]

const OPPORTUNITY_FIELDS = [
  'rank',
  'symbol',
  'close_return',
  'mfe',
  'selection_status',
  'root_cause',
]

const JOB_FIELDS = [
  'job_name',
  'trade_date',
  'status',
  'attempts',
  'error_code',
  'finished_at_utc',
]

const MATURITY_FIELDS = [
  'asof_utc',
  'paper_trading_sessions',
  'point_in_time_history_sessions',
  'net_labeled_trade_count',
  'quote_cost_coverage',
  'purged_oos_fold_count',
  'duplicate_order_count',
  'reconciliation_match_rate',
]

function pickFields(value, fields) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {}
  const selected = {}
  for (const field of fields) {
    if (value[field] !== undefined) selected[field] = value[field]
  }
  return selected
}

function evidenceReference(desk) {
  const selection = desk?.selection || {}
  const review = desk?.review || {}
  return {
    observedAtUtc: desk?.observed_at_utc || null,
    targetTradeDate: desk?.target_trade_date || null,
    selectionSnapshotId: selection.snapshot_id || null,
    selectionAsofUtc: selection.asof_utc || null,
    selectionSessionDate: selection.session_date || null,
    selectionStale: Boolean(selection.stale),
    reviewSnapshotId: review.snapshot_id || null,
    reviewSessionDate: review.session_date || null,
    reviewStale: Boolean(review.stale),
  }
}

function compactDeskEvidence(desk, { includeOperations = false } = {}) {
  const selection = desk?.selection || {}
  const review = desk?.review || {}
  const compact = {
    observed_at_utc: desk?.observed_at_utc || null,
    target_trade_date: desk?.target_trade_date || null,
    stage: desk?.stage || null,
    pipeline_status: desk?.pipeline_status || null,
    orders_authorized: false,
    runtime: {
      execution_mode: desk?.runtime?.execution_mode || null,
      local_execution: desk?.runtime?.local_execution === true,
      market_data: {
        provider_id: desk?.runtime?.market_data?.provider_id || null,
        configured: desk?.runtime?.market_data?.configured === true,
        healthy: desk?.runtime?.market_data?.healthy === true,
        reason: desk?.runtime?.market_data?.reason || null,
      },
    },
    selection: {
      status: selection.status || null,
      blocker: selection.blocker || null,
      target_trade_date: selection.target_trade_date || null,
      session_date: selection.session_date || null,
      snapshot_id: selection.snapshot_id || null,
      asof_utc: selection.asof_utc || null,
      stale: Boolean(selection.stale),
      pass_count: selection.pass_count ?? 0,
      candidates: Array.isArray(selection.candidates)
        ? selection.candidates.slice(0, 20).map((item) => (
            pickFields(item, CANDIDATE_FIELDS)
          ))
        : [],
    },
    review: {
      status: review.status || null,
      session_date: review.session_date || null,
      snapshot_id: review.snapshot_id || null,
      stale: Boolean(review.stale),
      opportunity_count: review.opportunity_count ?? 0,
      opportunities: Array.isArray(review.opportunities)
        ? review.opportunities.slice(0, 12).map((item) => (
            pickFields(item, OPPORTUNITY_FIELDS)
          ))
        : [],
    },
  }
  if (includeOperations) {
    compact.maturity = pickFields(desk?.maturity, MATURITY_FIELDS)
    compact.jobs = Array.isArray(desk?.jobs)
      ? desk.jobs.slice(0, 12).map((item) => pickFields(item, JOB_FIELDS))
      : []
  }
  return compact
}

function assistantMessages(question, desk) {
  const text = String(question || '').trim()
  if (!text || text.length > 4_000) {
    throw new Error('问题必须为 1–4000 个字符')
  }
  return [
    {
      role: 'system',
      content: [
        '你是美股日内量化研究助手，只能解释所给证据。',
        '用中文口语化回答，不使用 Markdown 星号、代码块或英文日志口吻。',
        '严格按“结论、关键数据、为什么只有它、风险与未知”四段输出。',
        '关键数据使用短横线逐项列出，并把字段翻译成人话，例如 RVOL 写作相对成交量、premarket_gap_return 写作盘前缺口。',
        '禁止输出 snake_case 原始字段名。不要复述快照 ID、观测时间、pipeline_status 等元数据，界面会单独展示证据引用。',
        '整段控制在 600 个中文字符以内，结论必须在前两句说清楚。',
        '不能承诺收益，不能把候选名单当作买入指令，不能声称已经下单。',
        '如果证据不足，明确说“不知道/证据不足”并指出需要什么。',
      ].join(''),
    },
    {
      role: 'user',
      content: `研究证据：${JSON.stringify(compactDeskEvidence(desk))}\n\n用户问题：${text}`,
    },
  ]
}

function agentMessages(role, desk) {
  const rolePrompt = ROLE_PROMPTS[role]
  if (!rolePrompt) throw new Error('未知 Agent 角色')
  return [
    {
      role: 'system',
      content: `${rolePrompt}${HUMAN_REVIEW_FORMAT}`,
    },
    {
      role: 'user',
      content: `请审阅以下研究证据：${JSON.stringify(compactDeskEvidence(
        desk,
        { includeOperations: role === 'supervisor' },
      ))}`,
    },
  ]
}

module.exports = {
  ROLE_PROMPTS,
  agentMessages,
  assistantMessages,
  compactDeskEvidence,
  evidenceReference,
}
