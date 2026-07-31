import assert from 'node:assert/strict'
import test from 'node:test'

import {
  booleanLabel,
  evaluationPresentation,
  evidenceReferenceFromDesk,
  evidenceReferenceKey,
  loadAssistantHistory,
  saveAssistantHistory,
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

test('assistant history survives page remounts and drops unknown fields', () => {
  const values = new Map()
  const storage = {
    getItem: (key) => values.get(key) || null,
    setItem: (key, value) => values.set(key, value),
  }
  saveAssistantHistory([
    { role: 'user', content: '为什么选择 BRKR？', apiKey: 'must-not-persist' },
    {
      role: 'assistant',
      content: '因为量价与催化共同确认。',
      model: 'model/a',
      truncated: false,
      usage: { totalTokens: 321 },
      evidence: { selectionSnapshotId: 'selection-1' },
      rawPayload: 'must-not-persist',
    },
  ], storage)

  const restored = loadAssistantHistory(storage)
  assert.equal(restored.length, 2)
  assert.equal(restored[0].content, '为什么选择 BRKR？')
  assert.equal(restored[1].model, 'model/a')
  assert.equal(restored[1].usage.totalTokens, 321)
  assert.equal(restored[1].evidence.selectionSnapshotId, 'selection-1')
  assert.equal(JSON.stringify(restored).includes('must-not-persist'), false)
})
