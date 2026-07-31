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
