const assert = require('node:assert/strict')
const { EventEmitter } = require('node:events')
const path = require('node:path')
const test = require('node:test')

const {
  createLocalRuntimeClient,
  createLocalRuntimeProcess,
  runtimeLaunch,
} = require('../analyst/local-runtime.cjs')

function jsonResponse(payload, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
  }
}

test('local runtime respawns after its child exits', async () => {
  let spawns = 0
  let latestChild
  const client = {
    connect: () => {},
    status: async () => ({ local_execution: true }),
  }
  const runtime = createLocalRuntimeProcess({
    app: {
      isPackaged: false,
      getPath: () => 'C:\\Users\\research\\AppData\\Roaming\\AIQuant',
    },
    projectRoot: 'C:\\project',
    client,
    spawnImpl: () => {
      const child = new EventEmitter()
      child.stdout = new EventEmitter()
      child.stderr = new EventEmitter()
      child.killed = false
      child.kill = () => { child.killed = true }
      latestChild = child
      spawns += 1
      queueMicrotask(() => {
        child.stdout.emit('data', Buffer.from(JSON.stringify({
          schema_version: 'macos_local_research_handshake.v1',
          url: `http://127.0.0.1:${54000 + spawns}`,
          local_execution: true,
          orders_authorized: false,
        }) + '\n'))
      })
      return child
    },
  })

  await runtime.start()
  latestChild.emit('exit', 1)
  await runtime.start()

  assert.equal(spawns, 2)
})

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

test('local runtime client keeps Paper auto-execution on its own 4002 routes', async () => {
  const requests = []
  const client = createLocalRuntimeClient({
    fetchImpl: async (url, options) => {
      requests.push({ url, options })
      return jsonResponse({
        schema_version: 'ibkr.paper_autopilot.v1',
        mode: 'paper',
        port: 4002,
        configured: true,
        connected: options.method === 'POST',
        running: false,
        paper_writes_armed: false,
      })
    },
  })
  client.connect({
    url: 'http://127.0.0.1:54321',
    token: 'ephemeral-runtime-token-value',
  })

  await client.paperAutopilotSnapshot()
  await client.paperAutopilotCommand({ kind: 'connect' })

  assert.equal(requests[0].url, 'http://127.0.0.1:54321/v1/paper-autopilot')
  assert.equal(
    requests[1].url,
    'http://127.0.0.1:54321/v1/paper-autopilot/commands',
  )
  assert.equal(requests[1].options.body, JSON.stringify({ kind: 'connect' }))
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

test('runtime launch forwards a separate fixed Paper 4002 profile without changing live 4001', () => {
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
        clientId: 17,
        liveAccount: 'U1234567',
        maxOrderNotional: 25_000,
        paper: {
          host: '192.0.2.44',
          clientId: 91,
          paperAccount: 'DU7654321',
          livermoreAppId: 'vbot-test',
          livermoreAppSecret: 'push-secret-value',
          livermoreChannelId: 'channel-test',
        },
      },
    },
  })

  assert.deepEqual(launch.executionEnv, {
    IBKR_HOST: '192.0.2.10',
    IBKR_CLIENT_ID: '17',
    IBKR_LIVE_ACCOUNT: 'U1234567',
    IBKR_MAX_ORDER_NOTIONAL: '25000',
    IBKR_PAPER_HOST: '192.0.2.44',
    IBKR_PAPER_CLIENT_ID: '91',
    IBKR_PAPER_ACCOUNT: 'DU7654321',
    LIVERMORE_APP_ID: 'vbot-test',
    LIVERMORE_APP_SECRET: 'push-secret-value',
    LIVERMORE_CHANNEL_ID: 'channel-test',
  })
  assert.equal(Object.hasOwn(launch.executionEnv, 'IBKR_PAPER_PORT'), false)
  assert.equal(Object.hasOwn(launch.executionEnv, 'IBKR_PORT'), false)
})

test('runtime safety secrets stay in child environment and out of process arguments', () => {
  const launch = runtimeLaunch({
    app: { isPackaged: false, getPath: () => 'C:\\Users\\research\\AppData' },
    projectRoot: 'C:\\project',
    token: 'ephemeral-runtime-token-value',
    platform: 'win32',
    marketData: {
      key: 'market-key-value',
      secret: 'market-secret-value',
      massiveApiKey: 'massive-key-value',
      secUserAgent: 'Research User research@example.com',
      openRouterApiKey: 'openrouter-secret-value',
      runtimeModels: { catalyst: 'openai/gpt-5.6', redTeam: 'anthropic/claude-sonnet-5' },
    },
  })

  assert.equal(launch.marketDataEnv.OPENROUTER_RUNTIME_API_KEY, 'openrouter-secret-value')
  assert.equal(launch.marketDataEnv.OPENROUTER_RUNTIME_CATALYST_MODEL, 'openai/gpt-5.6')
  assert.equal(JSON.stringify(launch.args).includes('openrouter-secret-value'), false)
  assert.equal(JSON.stringify(launch.args).includes('market-secret-value'), false)
})
