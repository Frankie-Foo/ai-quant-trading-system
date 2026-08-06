import assert from 'node:assert/strict'
import test from 'node:test'

import {
  booleanLabel,
  evaluationPresentation,
  evidenceReferenceFromDesk,
  evidenceReferenceKey,
} from '../src/client-state.js'


test('missing candidate booleans render as N/A instead of a negative fact', () => {
  assert.equal(booleanLabel(null, 'VWAP 上方', 'VWAP 下方'), 'N/A')
  assert.equal(booleanLabel(undefined, '强确认', '未确认'), 'N/A')
})


test('incomplete entry evidence cannot render as permission to buy', () => {
  const presentation = evaluationPresentation({
    action: 'enter_probe',
    suggested_shares: 10,
    blockers: [],
    facts: {
      one_minute_trigger: true,
      five_minute_confirmed: true,
      fifteen_minute_confirmed: true,
      benchmark_above_vwap: true,
      sector_above_vwap: true,
    },
  })

  assert.equal(presentation.complete, false)
  assert.equal(presentation.marketEnvironment, 'N/A')
  assert.equal(presentation.allConditionsPassed, false)
  assert.equal(presentation.action, 'no_action')
  assert.equal(presentation.suggestedShares, null)
})


test('complete explicit facts preserve a positive deterministic decision', () => {
  const presentation = evaluationPresentation({
    action: 'enter_probe',
    suggested_shares: 10,
    blockers: [],
    facts: {
      one_minute_trigger: true,
      five_minute_confirmed: true,
      fifteen_minute_confirmed: true,
      market_risk_off: false,
      benchmark_above_vwap: true,
      sector_above_vwap: true,
    },
  })

  assert.equal(presentation.complete, true)
  assert.equal(presentation.marketEnvironment, '允许做多')
  assert.equal(presentation.allConditionsPassed, true)
  assert.equal(presentation.action, 'enter_probe')
  assert.equal(presentation.suggestedShares, 10)
})


test('evidence references change when the immutable selection snapshot changes', () => {
  const first = evidenceReferenceFromDesk({
    observed_at_utc: '2026-07-31T12:00:00+00:00',
    target_trade_date: '2026-07-31',
    selection: {
      snapshot_id: 'selection-1',
      asof_utc: '2026-07-31T11:59:00+00:00',
      session_date: '2026-07-31',
      stale: false,
    },
    review: { snapshot_id: 'review-1', session_date: '2026-07-30' },
  })
  const second = { ...first, selectionSnapshotId: 'selection-2' }

  assert.notEqual(evidenceReferenceKey(first), evidenceReferenceKey(second))
  assert.equal(evidenceReferenceKey(first), [
    '2026-07-31',
    'selection-1',
    '2026-07-31T11:59:00+00:00',
    'review-1',
  ].join('|'))
})

