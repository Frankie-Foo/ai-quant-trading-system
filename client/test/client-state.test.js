import assert from 'node:assert/strict'
import test from 'node:test'

import {
  booleanLabel,
  evaluationPresentation,
  evidenceReferenceFromDesk,
  evidenceReferenceKey,
  executionErrorMessage,
  loadAssistantHistory,
  normalizeExecutionSnapshot,
  orderPreviewCommand,
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

test('execution snapshot keeps research and manual-order authority separate', () => {
  const snapshot = normalizeExecutionSnapshot({
    enabled: true,
    port: 4001,
    connected: true,
    account_bound: true,
    account_masked: 'U***4321',
    api_read_only: false,
    writes_armed: true,
    recovery_required: false,
    arm_confirmation_phrase: '启用实盘 U***4321',
    positions: [{ symbol: 'AAPL', quantity: 25, average_cost: 210.5 }],
    open_orders: [{ order_id: '91', symbol: 'AAPL', action: 'OpenLong' }],
    account_refreshed_at_utc: '2026-08-03T12:34:56Z',
    recent_orders: [{
      client_order_id: 'desktop-123',
      broker_order_id: 91,
      perm_id: 456,
      account_masked: 'U***4321',
      actual_account_id: 'must-not-reach-ui',
      order_ref: 'must-not-reach-ui',
      symbol: 'AAPL',
      action: 'OpenLong',
      quantity: 25,
      limit_price: 219.35,
      status: 'Submitted',
      updated_at_utc: '2026-08-03T12:34:55Z',
    }],
    secret: 'must-not-reach-ui',
  })

  assert.equal(snapshot.enabled, true)
  assert.equal(snapshot.port, 4001)
  assert.equal(snapshot.connected, true)
  assert.equal(snapshot.accountBound, true)
  assert.equal(snapshot.apiReadOnly, false)
  assert.equal(snapshot.writesArmed, true)
  assert.equal(snapshot.canPreview, true)
  assert.equal(snapshot.armConfirmationPhrase, '启用实盘 U***4321')
  assert.equal(snapshot.positions[0].symbol, 'AAPL')
  assert.equal(snapshot.accountRefreshedAtUtc, '2026-08-03T12:34:56Z')
  assert.equal(snapshot.recentOrders[0].broker_order_id, 91)
  assert.equal('account_masked' in snapshot.recentOrders[0], false)
  assert.equal('order_ref' in snapshot.recentOrders[0], false)
  assert.equal(JSON.stringify(snapshot).includes('must-not-reach-ui'), false)
})

test('unbound IBKR account exposes only a masked discovery and cannot trade', () => {
  const snapshot = normalizeExecutionSnapshot({
    enabled: true,
    port: 4001,
    connected: true,
    account_bound: false,
    account_masked: 'U***9876',
    binding_confirmation_phrase: '绑定实盘账户 U***9876',
    api_read_only: false,
    writes_armed: false,
    actual_account_id: 'must-never-reach-renderer-state',
  })

  assert.equal(snapshot.accountBound, false)
  assert.equal(snapshot.accountMasked, 'U***9876')
  assert.equal(snapshot.bindingConfirmationPhrase, '绑定实盘账户 U***9876')
  assert.equal(snapshot.canPreview, false)
  assert.equal(
    JSON.stringify(snapshot).includes('must-never-reach-renderer-state'),
    false,
  )
})

test('order preview command is limit-day and permanently long-only', () => {
  assert.deepEqual(orderPreviewCommand({
    symbol: ' brk.b ',
    action: 'ReduceLong',
    quantity: '5',
    limitPrice: '479.25',
  }, 'desktop-order-1'), {
    kind: 'preview',
    order: {
      client_order_id: 'desktop-order-1',
      symbol: 'BRK.B',
      security_type: 'STK',
      exchange: 'SMART',
      currency: 'USD',
      action: 'ReduceLong',
      quantity: 5,
      limit_price: 479.25,
      order_type: 'LMT',
      tif: 'DAY',
    },
  })
  assert.throws(
    () => orderPreviewCommand({
      symbol: 'AAPL',
      action: 'SellShort',
      quantity: 1,
      limitPrice: 200,
    }, 'desktop-order-2'),
    /只允许开多或减多/,
  )
})

test('IBKR technical failures are rendered as actionable Chinese messages', () => {
  assert.equal(
    executionErrorMessage(new Error('IBKR API is read-only')),
    '盈透 API 当前为只读，请在 TWS / IB Gateway 中关闭 Read-Only API 后重连。',
  )
  assert.equal(
    executionErrorMessage(new Error('account_mismatch')),
    '当前登录的盈透账户与已绑定实盘账户不一致，连接已拒绝。',
  )
  assert.equal(
    executionErrorMessage(new Error('preview expired')),
    '订单预览已过期，请重新生成预览。',
  )
  assert.equal(
    executionErrorMessage(new Error('multiple_managed_accounts')),
    '当前 TWS 会话返回多个账户。为防止绑错或下错账户，请改用单一账户会话，或在网关中明确限制账户后重连。',
  )
  assert.equal(
    executionErrorMessage(new Error('multiple_accounts_require_selection')),
    '当前 TWS 会话返回多个账户。为防止绑错或下错账户，请改用单一账户会话，或在网关中明确限制账户后重连。',
  )
  assert.equal(
    executionErrorMessage(new Error('api_read_only')),
    '盈透 API 当前为只读，请在 TWS / IB Gateway 中关闭 Read-Only API 后重连。',
  )
  assert.equal(
    executionErrorMessage(new Error('preview_required')),
    '没有可提交的匹配预览，请重新生成订单预览后再确认。',
  )
  assert.equal(
    executionErrorMessage(new Error('preview_changed')),
    '提交前 IBKR What-If 结果或警告发生变化，原预览已失效；请重新生成并重新确认。',
  )
  assert.equal(
    executionErrorMessage(new Error('duplicate_exposure')),
    '该标的已有同方向活动订单，为避免重复暴露，本次操作已拒绝。',
  )
  assert.equal(
    executionErrorMessage(new Error('max_notional_exceeded')),
    '新增开多金额超过已配置的单笔最大新增开仓金额；该限制仅适用于 OpenLong。',
  )
  assert.equal(
    executionErrorMessage(new Error('execution_failed')),
    '实盘执行失败，结果未被视为成功；请先核对最近订单和 IBKR 状态。',
  )
})
