const assert = require('node:assert/strict')
const path = require('node:path')
const test = require('node:test')

const {
  createLocalRuntimeClient,
  runtimeLaunch,
} = require('../analyst/local-runtime.cjs')

function jsonResponse(payload, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
  }
}

test('local runtime client sends authenticated JSON execution commands', async () => {
  const requests = []
  const client = createLocalRuntimeClient({
    fetchImpl: async (url, options) => {
      requests.push({ url, options })
      return jsonResponse({
        schema_version: 'ibkr.execution.v1',
        mode: 'live',
        port: 4001,
        enabled: options.method === 'POST',
        connected: options.method === 'POST',
        writes_armed: false,
      })
    },
  })
  client.connect({
    url: 'http://127.0.0.1:54321',
    token: 'ephemeral-runtime-token-value',
  })

  await client.executionSnapshot()
  const connected = await client.executionCommand({ kind: 'connect' })

  assert.equal(connected.port, 4001)
  assert.equal(requests[0].url, 'http://127.0.0.1:54321/v1/execution')
  assert.equal(requests[0].options.method, 'GET')
  assert.equal(
    requests[1].url,
    'http://127.0.0.1:54321/v1/execution/commands',
  )
  assert.equal(requests[1].options.method, 'POST')
  assert.equal(requests[1].options.headers['Content-Type'], 'application/json')
  assert.equal(requests[1].options.body, JSON.stringify({ kind: 'connect' }))
  assert.equal(
    requests[1].options.headers.Authorization,
    'Bearer ephemeral-runtime-token-value',
  )
})

test('local runtime client exposes only bounded execution error codes', async () => {
  const client = createLocalRuntimeClient({
    fetchImpl: async () => jsonResponse({
      error: 'raw broker text must stay hidden',
      error_code: 'account_mismatch',
    }, 409),
  })
  client.connect({
    url: 'http://127.0.0.1:54321',
    token: 'ephemeral-runtime-token-value',
  })

  await assert.rejects(
    client.executionCommand({ kind: 'connect' }),
    (error) => error.message === 'account_mismatch',
  )
})

test('runtime launch forwards only the fixed live execution profile through env', () => {
  const launch = runtimeLaunch({
    app: {
      isPackaged: true,
      getPath: () => 'C:\\Users\\research\\AppData\\Roaming\\AIQuant',
    },
    projectRoot: 'C:\\unused',
    resourcesPath: 'C:\\Program Files\\AIQuant\\resources',
    token: 'ephemeral-runtime-token-value',
    platform: 'win32',
    marketData: {
      ibkr: {
        host: '192.0.2.10',
        clientId: 87,
        liveAccount: 'U1234567',
        port: 4002,
        maxOrderNotional: 25_000,
        username: 'must-not-leak',
        password: 'must-not-leak',
      },
    },
  })

  assert.deepEqual(launch.executionEnv, {
    IBKR_HOST: '192.0.2.10',
    IBKR_CLIENT_ID: '87',
    IBKR_LIVE_ACCOUNT: 'U1234567',
    IBKR_MAX_ORDER_NOTIONAL: '25000',
  })
  assert.equal(Object.hasOwn(launch.executionEnv, 'IBKR_PORT'), false)
  assert.equal(JSON.stringify(launch).includes('must-not-leak'), false)
  assert.equal(
    launch.command,
    path.join(
      'C:\\Program Files\\AIQuant\\resources',
      'runtime',
      'windows-research-runtime.exe',
    ),
  )
})

test('runtime launch can detect an account before first secure binding', () => {
  const launch = runtimeLaunch({
    app: {
      isPackaged: false,
      getPath: () => 'C:\\Users\\research\\AppData\\Roaming\\AIQuant',
    },
    projectRoot: 'C:\\project',
    token: 'ephemeral-runtime-token-value',
    platform: 'win32',
    marketData: {
      ibkr: {
        host: '192.0.2.10',
        clientId: 87,
        liveAccount: '',
        maxOrderNotional: 25_000,
      },
    },
  })

  assert.deepEqual(launch.executionEnv, {
    IBKR_HOST: '192.0.2.10',
    IBKR_CLIENT_ID: '87',
    IBKR_MAX_ORDER_NOTIONAL: '25000',
  })
})
