import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const source = fs.readFileSync(
  path.resolve(here, '..', 'src', 'AnalystApp.jsx'),
  'utf8',
)

test('manual execution UI is accessible and keeps live authority explicit', () => {
  assert.match(source, /实盘执行总开关/)
  assert.match(source, /kind: 'connect'/)
  assert.match(source, /kind: 'disconnect'/)
  assert.match(source, /kind: 'bind_account'/)
  assert.match(source, /kind: 'recover'/)
  assert.match(source, /bindingConfirmationPhrase/)
  assert.match(source, /clearExecutionAccountBinding/)
  assert.match(source, /最近提交订单/)
  assert.match(source, /accountRefreshedAtUtc/)
  assert.match(source, /What-If/)
  assert.match(source, /warningAcknowledged/)
  assert.match(source, /不会撤销 IBKR 已有挂单/)
  assert.match(source, /单笔最大新增开仓金额（仅 OpenLong）/)
  assert.match(source, /aria-live="polite"/)
  assert.match(source, /role="alert"/)
  assert.match(source, /研究内核.*orders_authorized=false/)
  assert.match(source, /手动执行台/)
  assert.match(source, /OpenLong/)
  assert.match(source, /ReduceLong/)
  assert.doesNotMatch(source, /SellShort/)
  assert.doesNotMatch(source, /kind: 'switch'/)
  assert.doesNotMatch(source, /4002/)
  assert.doesNotMatch(source, /values\.liveAccount/)
  assert.doesNotMatch(source, /禁止全部委托/)
  assert.doesNotMatch(source, /单笔最大名义金额/)
  assert.doesNotMatch(source, /单笔上限/)
  assert.match(
    source,
    /disabled=\{busy \|\| \(!execution\.enabled && !settings\?\.execution\?\.configured\)\}/,
  )
})
