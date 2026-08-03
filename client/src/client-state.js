const REQUIRED_GATE_FACTS = [
  'one_minute_trigger',
  'five_minute_confirmed',
  'fifteen_minute_confirmed',
  'market_risk_off',
  'benchmark_above_vwap',
  'sector_above_vwap',
]

const POSITIVE_ACTIONS = new Set(['arm_entry', 'enter_probe', 'allow_add'])

export function booleanLabel(value, trueLabel, falseLabel) {
  if (typeof value !== 'boolean') return 'N/A'
  return value ? trueLabel : falseLabel
}

export function evaluationPresentation(evaluation) {
  const facts = evaluation?.facts
  const complete = Boolean(
    facts
    && REQUIRED_GATE_FACTS.every((key) => typeof facts[key] === 'boolean'),
  )
  const requestedAction = evaluation?.action || 'no_action'
  const action = !complete && POSITIVE_ACTIONS.has(requestedAction)
    ? 'no_action'
    : requestedAction
  const blockers = Array.isArray(evaluation?.blockers) ? evaluation.blockers : []

  return {
    action,
    suggestedShares: complete ? (evaluation?.suggested_shares ?? null) : null,
    complete,
    allConditionsPassed: complete && blockers.length === 0,
    marketEnvironment: booleanLabel(
      facts?.market_risk_off,
      'Risk-off',
      '允许做多',
    ),
  }
}

export function evidenceReferenceFromDesk(desk) {
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

export function evidenceReferenceKey(reference) {
  return [
    reference?.targetTradeDate || '',
    reference?.selectionSnapshotId || '',
    reference?.selectionAsofUtc || '',
    reference?.reviewSnapshotId || '',
  ].join('|')
}

const ASSISTANT_HISTORY_KEY = 'ai_quant_research_assistant_history.v1'
const MAX_ASSISTANT_HISTORY_MESSAGES = 50
const MAX_ASSISTANT_MESSAGE_CHARACTERS = 50000
const EVIDENCE_KEYS = [
  'observedAtUtc',
  'targetTradeDate',
  'selectionSnapshotId',
  'selectionAsofUtc',
  'selectionSessionDate',
  'selectionStale',
  'reviewSnapshotId',
  'reviewSessionDate',
  'reviewStale',
]

function safeStorage(storage) {
  if (storage) return storage
  try {
    return globalThis.localStorage || null
  } catch {
    return null
  }
}

function sanitizeObject(value, keys) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return undefined
  const result = {}
  for (const key of keys) {
    const candidate = value[key]
    if (['string', 'number', 'boolean'].includes(typeof candidate)) {
      result[key] = candidate
    } else if (candidate === null) {
      result[key] = null
    }
  }
  return Object.keys(result).length ? result : undefined
}

function sanitizeAssistantMessage(message) {
  if (!message || !['user', 'assistant'].includes(message.role)) return null
  const content = typeof message.content === 'string'
    ? message.content.trim().slice(0, MAX_ASSISTANT_MESSAGE_CHARACTERS)
    : ''
  if (!content) return null
  const result = { role: message.role, content }
  if (message.role === 'assistant') {
    if (typeof message.model === 'string' && message.model.trim()) {
      result.model = message.model.trim().slice(0, 300)
    }
    if (typeof message.truncated === 'boolean') result.truncated = message.truncated
    const usage = sanitizeObject(message.usage, [
      'promptTokens',
      'completionTokens',
      'totalTokens',
    ])
    const evidence = sanitizeObject(message.evidence, EVIDENCE_KEYS)
    if (usage) result.usage = usage
    if (evidence) result.evidence = evidence
  }
  return result
}

function sanitizedAssistantHistory(messages) {
  if (!Array.isArray(messages)) return []
  return messages
    .map(sanitizeAssistantMessage)
    .filter(Boolean)
    .slice(-MAX_ASSISTANT_HISTORY_MESSAGES)
}

export function loadAssistantHistory(storage) {
  const target = safeStorage(storage)
  if (!target || typeof target.getItem !== 'function') return []
  try {
    const raw = target.getItem(ASSISTANT_HISTORY_KEY)
    return raw ? sanitizedAssistantHistory(JSON.parse(raw)) : []
  } catch {
    return []
  }
}

export function saveAssistantHistory(messages, storage) {
  const sanitized = sanitizedAssistantHistory(messages)
  const target = safeStorage(storage)
  if (!target || typeof target.setItem !== 'function') return sanitized
  try {
    target.setItem(ASSISTANT_HISTORY_KEY, JSON.stringify(sanitized))
  } catch {
    // Keep the in-memory conversation when local storage is unavailable or full.
  }
  return sanitized
}

function finiteNumber(value) {
  const normalized = Number(value)
  return Number.isFinite(normalized) ? normalized : null
}

export function executionErrorMessage(error) {
  const message = String(error?.message || error || '').toLowerCase()
  if (
    message.includes('multiple_accounts_require_selection')
    || message.includes('multiple_managed_accounts')
    || message.includes('multiple managed accounts')
    || message.includes('multiple ibkr accounts')
  ) {
    return '当前 TWS 会话返回多个账户。为防止绑错或下错账户，请改用单一账户会话，或在网关中明确限制账户后重连。'
  }
  if (
    message.includes('api_read_only')
    || message.includes('read-only')
    || message.includes('read only')
  ) {
    return '盈透 API 当前为只读，请在 TWS / IB Gateway 中关闭 Read-Only API 后重连。'
  }
  if (message.includes('account_mismatch') || message.includes('does not match')) {
    return '当前登录的盈透账户与已绑定实盘账户不一致，连接已拒绝。'
  }
  if (message.includes('account_not_bound') || message.includes('account not bound')) {
    return '当前实盘账户尚未绑定，请先连接并按动态确认文字完成首次绑定。'
  }
  if (message.includes('preview_expired') || message.includes('preview expired')) {
    return '订单预览已过期，请重新生成预览。'
  }
  if (message.includes('preview_required')) {
    return '没有可提交的匹配预览，请重新生成订单预览后再确认。'
  }
  if (message.includes('preview_changed')) {
    return '提交前 IBKR What-If 结果或警告发生变化，原预览已失效；请重新生成并重新确认。'
  }
  if (message.includes('not configured')) {
    return '请先在设置中保存盈透实盘连接配置。'
  }
  if (
    message.includes('connection_timeout')
    || message.includes('connection_failed')
    || message.includes('connect call failed')
    || message.includes('connection refused')
  ) {
    return '无法连接盈透实盘端口 4001，请确认 TWS / IB Gateway 已登录并启用 Socket API。'
  }
  if (message.includes('client id')) {
    return '盈透 Client ID 无效或已被占用，请更换后重连。'
  }
  if (message.includes('confirmation')) {
    return '确认文字不匹配，操作未执行。'
  }
  if (
    message.includes('max_notional_exceeded')
    || message.includes('exceeds max_notional')
  ) {
    return '新增开多金额超过已配置的单笔最大新增开仓金额；该限制仅适用于 OpenLong。'
  }
  if (
    message.includes('reduce_long_exceeds_position')
    || message.includes('exceeds the current long position')
  ) {
    return '减多股数超过当前可用多头持仓。'
  }
  if (message.includes('duplicate_exposure')) {
    return '该标的已有同方向活动订单，为避免重复暴露，本次操作已拒绝。'
  }
  if (message.includes('broker_rejected')) {
    return 'IBKR 已拒绝该订单。请查看 What-If、账户权限及最近订单状态后重新评估。'
  }
  if (message.includes('invalid_execution_command')) {
    return '执行请求格式无效，未向 IBKR 提交任何订单。'
  }
  if (message.includes('execution_failed')) {
    return '实盘执行失败，结果未被视为成功；请先核对最近订单和 IBKR 状态。'
  }
  if (message.includes('recovery') || message.includes('status unknown')) {
    return '存在状态未知的订单，请先完成券商对账，暂不允许新委托。'
  }
  return String(error?.message || error || '盈透执行操作失败').slice(0, 500)
}

export function paperAutopilotErrorMessage(error) {
  const message = String(error?.message || error || '').toLowerCase()
  if (message.includes('paper_profile_invalid') || message.includes('not configured')) {
    return '请先在设置中保存 IBKR 模拟盘主机、Client ID 和 DU 账户。'
  }
  if (
    message.includes('connection_timeout')
    || message.includes('connection_failed')
  ) {
    return '无法连接盈透模拟盘端口 4002，请确认 Paper TWS / IB Gateway 已登录并启用 Socket API。'
  }
  if (message.includes('paper_plan_invalid')) {
    return '今日冻结执行计划无效或已过期，自动模拟盘不会启动。'
  }
  if (message.includes('paper_safety_envelope_invalid')) {
    return '安全包缺失、过期或不一致，自动模拟盘已保持关闭。'
  }
  if (message.includes('account_mismatch')) {
    return '当前模拟网关账户与配置的 DU 账户不一致，连接已拒绝。'
  }
  if (message.includes('confirmation')) {
    return '模拟盘自动执行确认文字不匹配，未启动。'
  }
  return String(error?.message || error || '模拟盘自动执行操作失败').slice(0, 500)
}

function executionRows(rows, keys) {
  if (!Array.isArray(rows)) return []
  return rows.slice(0, 250).map((row) => {
    if (!row || typeof row !== 'object' || Array.isArray(row)) return {}
    const result = {}
    for (const key of keys) {
      const value = row[key]
      if (['string', 'number', 'boolean'].includes(typeof value) || value === null) {
        result[key] = value
      }
    }
    return result
  })
}

export function normalizeExecutionSnapshot(snapshot) {
  const source = snapshot && typeof snapshot === 'object' ? snapshot : {}
  const enabled = source.enabled === true
  const connected = source.connected === true
  const apiReadOnly = typeof source.api_read_only === 'boolean'
    ? source.api_read_only
    : null
  const writesArmed = source.writes_armed === true
  const recoveryRequired = source.recovery_required === true
  const accountBound = source.account_bound === true
  return {
    enabled,
    port: finiteNumber(source.port) || 4001,
    connected,
    accountBound,
    accountMasked: typeof source.account_masked === 'string'
      ? source.account_masked.slice(0, 80)
      : '',
    apiReadOnly,
    writesArmed,
    recoveryRequired,
    canPreview: enabled
      && connected
      && accountBound
      && apiReadOnly === false
      && writesArmed
      && !recoveryRequired,
    armConfirmationPhrase: typeof source.arm_confirmation_phrase === 'string'
      ? source.arm_confirmation_phrase.slice(0, 200)
      : '',
    bindingConfirmationPhrase:
      typeof source.binding_confirmation_phrase === 'string'
        ? source.binding_confirmation_phrase.slice(0, 200)
        : '',
    maxOrderNotional: finiteNumber(source.max_order_notional),
    positions: executionRows(source.positions, [
      'symbol',
      'quantity',
      'average_cost',
      'market_price',
      'unrealized_pnl_percent',
    ]),
    openOrders: executionRows(source.open_orders, [
      'order_id',
      'client_order_id',
      'symbol',
      'action',
      'side',
      'quantity',
      'filled_quantity',
      'limit_price',
      'status',
    ]),
    accountRefreshedAtUtc:
      typeof source.account_refreshed_at_utc === 'string'
        ? source.account_refreshed_at_utc.slice(0, 50)
        : '',
    recentOrders: executionRows(source.recent_orders, [
      'client_order_id',
      'broker_order_id',
      'perm_id',
      'symbol',
      'action',
      'quantity',
      'limit_price',
      'status',
      'updated_at_utc',
    ]),
    lastError: typeof source.last_error === 'string'
      ? source.last_error.slice(0, 500)
      : '',
  }
}

export function normalizePaperAutopilotSnapshot(snapshot) {
  const source = snapshot && typeof snapshot === 'object' ? snapshot : {}
  const outcomes = Array.isArray(source.last_outcomes)
    ? source.last_outcomes.slice(0, 20).map((item) => ({
        planId: typeof item?.plan_id === 'string' ? item.plan_id.slice(0, 120) : '',
        symbol: typeof item?.symbol === 'string' ? item.symbol.slice(0, 15) : '',
        action: typeof item?.action === 'string' ? item.action.slice(0, 40) : '',
        reasons: Array.isArray(item?.reasons)
          ? item.reasons.filter((reason) => typeof reason === 'string').slice(0, 12)
          : [],
        degradedReasons: Array.isArray(item?.degraded_reasons)
          ? item.degraded_reasons.filter((reason) => typeof reason === 'string').slice(0, 12)
          : [],
      }))
    : []
  return {
    configured: source.configured === true,
    connected: source.connected === true,
    running: source.running === true,
    paperWritesArmed: source.paper_writes_armed === true,
    port: finiteNumber(source.port) || 4002,
    accountMasked: typeof source.account_masked === 'string'
      ? source.account_masked.slice(0, 80)
      : '',
    armConfirmationPhrase: typeof source.arm_confirmation_phrase === 'string'
      ? source.arm_confirmation_phrase.slice(0, 200)
      : '',
    planStatus: typeof source.plan_status === 'string'
      ? source.plan_status.slice(0, 40)
      : 'missing',
    planError: typeof source.plan_error === 'string'
      ? source.plan_error.slice(0, 200)
      : '',
    lastTickAtUtc: typeof source.last_tick_at_utc === 'string'
      ? source.last_tick_at_utc.slice(0, 50)
      : '',
    lastOutcomes: outcomes,
    lastError: typeof source.last_error === 'string'
      ? source.last_error.slice(0, 200)
      : '',
  }
}

export function orderPreviewCommand(draft, clientOrderId) {
  const symbol = String(draft?.symbol || '').trim().toUpperCase()
  if (!/^[A-Z][A-Z0-9.-]{0,14}$/.test(symbol)) {
    throw new Error('请输入有效的股票代码')
  }
  const action = String(draft?.action || '')
  if (!['OpenLong', 'ReduceLong'].includes(action)) {
    throw new Error('只允许开多或减多，不允许卖空')
  }
  const quantity = Number(draft?.quantity)
  if (!Number.isSafeInteger(quantity) || quantity <= 0) {
    throw new Error('股数必须是正整数')
  }
  const limitPrice = Number(draft?.limitPrice)
  if (!Number.isFinite(limitPrice) || limitPrice <= 0) {
    throw new Error('限价必须大于 0')
  }
  const normalizedId = String(clientOrderId || '').trim()
  if (!/^[A-Za-z0-9_.-]{1,80}$/.test(normalizedId)) {
    throw new Error('客户端订单号无效')
  }
  return {
    kind: 'preview',
    order: {
      client_order_id: normalizedId,
      symbol,
      security_type: 'STK',
      exchange: 'SMART',
      currency: 'USD',
      action,
      quantity,
      limit_price: limitPrice,
      order_type: 'LMT',
      tif: 'DAY',
    },
  }
}
