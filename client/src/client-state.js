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
