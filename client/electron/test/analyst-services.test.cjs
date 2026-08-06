const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const test = require('node:test')

const {
  createSecureSettingsStore,
} = require('../analyst/settings.cjs')
const {
  createLocalRuntimeClient,
  runtimeLaunch,
  validateRuntimeHandshake,
} = require('../analyst/local-runtime.cjs')
const { createOpenRouterClient } = require('../analyst/openrouter.cjs')
const {
  agentMessages,
  assistantMessages,
  compactDeskEvidence,
  evidenceReference,
} = require('../analyst/prompts.cjs')
const { createIbkrPaperAdapter } = require('../analyst/ibkr-paper.cjs')

const MODELS = {
  question: 'openai/gpt-4o-mini',
  catalyst: 'deepseek/deepseek-chat',
  red_team: 'anthropic/claude-3.5-haiku',
  supervisor: 'google/gemini-2.0-flash',
}

function fakeSafeStorage() {
  return {
    isEncryptionAvailable: () => true,
    encryptString: (value) => Buffer.from(`encrypted:${value}`, 'utf8'),
    decryptString: (value) => value.toString('utf8').replace(/^encrypted:/, ''),
  }
}

function jsonResponse(body, { status = 200, headers = {} } = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', ...headers },
  })
}

test('secure settings encrypt model and market-data secrets and expose redacted state', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'analyst-settings-'))
  const settingsPath = path.join(root, 'settings.json')
  const store = createSecureSettingsStore({
    filePath: settingsPath,
    safeStorage: fakeSafeStorage(),
  })

  store.saveConnectionSecrets({
    openRouterApiKey: 'openrouter-key-value',
    marketDataKey: 'market-key-value',
    marketDataSecret: 'market-secret-value',
  })
  store.saveModels(MODELS)

  const publicSettings = store.loadPublic()
  assert.equal(publicSettings.configured, true)
  assert.equal(publicSettings.openRouterKeyConfigured, true)
  assert.equal(publicSettings.marketDataConfigured, true)
  assert.equal(publicSettings.marketDataProvider, 'alpaca_proxy_sip')
  assert.equal('dataServiceUrl' in publicSettings, false)
  assert.equal('dataAccessTokenConfigured' in publicSettings, false)
  assert.deepEqual(publicSettings.models, MODELS)
  assert.equal(JSON.stringify(publicSettings).includes('openrouter-key-value'), false)

  const persisted = fs.readFileSync(settingsPath, 'utf8')
  assert.equal(persisted.includes('openrouter-key-value'), false)
  assert.equal(persisted.includes('market-key-value'), false)
  assert.equal(persisted.includes('market-secret-value'), false)
  assert.deepEqual(store.loadSecrets(), {
    openRouterApiKey: 'openrouter-key-value',
    marketDataKey: 'market-key-value',
    marketDataSecret: 'market-secret-value',
  })
})

test('secure settings fail closed when OS encryption is unavailable', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'analyst-settings-'))
  const settingsPath = path.join(root, 'settings.json')
  const store = createSecureSettingsStore({
    filePath: settingsPath,
    safeStorage: {
      isEncryptionAvailable: () => false,
      encryptString: () => {
        throw new Error('must not run')
      },
      decryptString: () => {
        throw new Error('must not run')
      },
    },
  })

  assert.throws(
    () => store.saveConnectionSecrets({
      openRouterApiKey: 'openrouter-key-value',
      marketDataKey: 'market-key-value',
      marketDataSecret: 'market-secret-value',
    }),
    /不会以明文保存/,
  )
  assert.equal(fs.existsSync(settingsPath), false)
})

test('local runtime handshake accepts only loopback and non-executable mode', () => {
  assert.deepEqual(
    validateRuntimeHandshake({
      schema_version: 'macos_local_research_handshake.v1',
      url: 'http://127.0.0.1:54321',
      local_execution: true,
      orders_authorized: false,
    }),
    {
      url: 'http://127.0.0.1:54321',
      localExecution: true,
    },
  )
  assert.throws(
    () => validateRuntimeHandshake({
      schema_version: 'macos_local_research_handshake.v1',
      url: 'https://research.example.com',
      local_execution: true,
      orders_authorized: false,
    }),
    /loopback/,
  )
  assert.throws(
    () => validateRuntimeHandshake({
      schema_version: 'macos_local_research_handshake.v1',
      url: 'http://127.0.0.1:54321',
      local_execution: true,
      orders_authorized: true,
    }),
    /non-executable/,
  )
})

test('packaged runtime launch fixes the proxy endpoint and keeps secrets out of args', () => {
  const launch = runtimeLaunch({
    app: {
      isPackaged: true,
      getPath: (name) => {
        assert.equal(name, 'userData')
        return '/Users/research/Library/Application Support/AIQuant'
      },
    },
    projectRoot: '/unused',
    resourcesPath: '/Applications/AIQuant.app/Contents/Resources',
    token: 'ephemeral-runtime-token-value',
    marketData: {
      key: 'market-key-value',
      secret: 'market-secret-value',
    },
  })

  assert.equal(
    launch.command,
    path.join(
      '/Applications/AIQuant.app/Contents/Resources',
      'runtime',
      'macos-research-runtime',
    ),
  )
  assert.equal(launch.args.includes('alpaca_proxy'), true)
  assert.equal(
    launch.args.includes(
      path.join(
        '/Users/research/Library/Application Support/AIQuant',
        'research-data',
      ),
    ),
    true,
  )
  assert.equal(launch.args.some((value) => value.startsWith('http')), false)
  assert.equal(launch.args.includes('market-key-value'), false)
  assert.equal(launch.args.includes('market-secret-value'), false)
  assert.deepEqual(launch.marketDataEnv, {
    ALPACA_PROXY_KEY: 'market-key-value',
    ALPACA_PROXY_SECRET: 'market-secret-value',
  })
})

test('OpenRouter client lists text models and sends non-streaming chat safely', async () => {
  const requests = []
  const client = createOpenRouterClient({
    fetchImpl: async (url, options = {}) => {
      requests.push({ url, options })
      if (String(url).endsWith('/models?output_modalities=text')) {
        return jsonResponse({
          data: [
            {
              id: 'model/a',
              name: 'Model A',
              context_length: 32768,
              pricing: { prompt: '0.000001', completion: '0.000002' },
            },
          ],
        })
      }
      return jsonResponse({
        model: 'model/a',
        choices: [{ message: { role: 'assistant', content: '只读分析结论' } }],
        usage: { prompt_tokens: 10, completion_tokens: 5, total_tokens: 15 },
      })
    },
  })

  const models = await client.listModels('secret-key')
  const completion = await client.complete({
    apiKey: 'secret-key',
    model: 'model/a',
    messages: [{ role: 'user', content: '解释今天的候选股' }],
  })

  assert.deepEqual(models, [
    {
      id: 'model/a',
      name: 'Model A',
      contextLength: 32768,
      promptPrice: '0.000001',
      completionPrice: '0.000002',
    },
  ])
  assert.equal(completion.content, '只读分析结论')
  assert.equal(completion.model, 'model/a')
  assert.equal(completion.usage.totalTokens, 15)
  assert.equal(requests[0].options.headers.Authorization, 'Bearer secret-key')
  assert.equal(requests[1].options.headers.Authorization, 'Bearer secret-key')
  assert.equal(JSON.parse(requests[1].options.body).stream, false)
  assert.equal(JSON.parse(requests[1].options.body).max_tokens, 8192)
})

test('OpenRouter errors expose typed status but never echo the API key', async () => {
  const client = createOpenRouterClient({
    fetchImpl: async () => jsonResponse(
      {
        error: {
          message: 'invalid key secret-key-value',
          metadata: { error_type: 'authentication' },
        },
      },
      { status: 401 },
    ),
  })

  await assert.rejects(
    client.listModels('secret-key-value'),
    (error) => {
      assert.match(error.message, /authentication/)
      assert.equal(error.message.includes('secret-key-value'), false)
      return true
    },
  )
})

test('local runtime client authenticates locally and requires local evidence', async () => {
  const requests = []
  const client = createLocalRuntimeClient({
    fetchImpl: async (url, options) => {
      requests.push({ url, options })
      return jsonResponse({
        schema_version: 'trading_desk_evidence.v1',
        stage: 'research_only',
        orders_authorized: false,
        selection: { status: 'ready', candidates: [] },
        review: { status: 'ready', opportunities: [] },
        agents: [],
        jobs: [],
        maturity: {},
        runtime: {
          schema_version: 'macos_local_research_runtime.v1',
          local_execution: true,
          orders_authorized: false,
        },
      })
    },
  })
  client.connect({
    url: 'http://127.0.0.1:54321',
    token: 'ephemeral-runtime-token-value',
  })

  const desk = await client.fetchDesk()

  assert.equal(desk.orders_authorized, false)
  assert.equal(desk.runtime.local_execution, true)
  assert.equal(requests[0].url, 'http://127.0.0.1:54321/v1/desk')
  assert.equal(
    requests[0].options.headers.Authorization,
    'Bearer ephemeral-runtime-token-value',
  )
})

test('local runtime client rejects remote or executable evidence', async () => {
  const client = createLocalRuntimeClient({
    fetchImpl: async () => jsonResponse({
      schema_version: 'trading_desk_evidence.v1',
      stage: 'paper',
      orders_authorized: true,
      selection: {},
      review: {},
      runtime: {
        schema_version: 'macos_local_research_runtime.v1',
        local_execution: false,
      },
    }),
  })
  client.connect({
    url: 'http://127.0.0.1:54321',
    token: 'ephemeral-runtime-token-value',
  })

  await assert.rejects(
    client.fetchDesk(),
    /local non-executable/,
  )
})

test('IBKR Paper seam is present but fail-closed in the research edition', async () => {
  const adapter = createIbkrPaperAdapter()

  assert.deepEqual(await adapter.status(), {
    adapter: 'ibkr-paper-reserved.v1',
    configured: false,
    connected: false,
    orderSubmissionEnabled: false,
  })
  await assert.rejects(adapter.connect(), /reserved/)
  await assert.rejects(adapter.placeOrder({ symbol: 'AAPL' }), /disabled/)
})

test('assistant and agent prompts are evidence bounded and force non-executable context', () => {
  const desk = {
    target_trade_date: '2026-07-31',
    stage: 'research_only',
    orders_authorized: true,
    selection: {
      status: 'blocked',
      stale: true,
      candidates: [{ symbol: 'AAA', selection_rank: 1 }],
    },
    review: { opportunities: [] },
    jobs: [],
  }

  const compact = compactDeskEvidence(desk)
  const assistant = assistantMessages('为什么今天不能买？', desk)
  const redTeam = agentMessages('red_team', desk)

  assert.equal(compact.orders_authorized, false)
  assert.match(assistant[0].content, /不能把候选名单当作买入指令/)
  assert.match(assistant[1].content, /2026-07-31/)
  assert.match(redTeam[0].content, /不能授权买卖/)
  assert.throws(() => agentMessages('trader', desk), /未知 Agent/)
})


test('AI evidence is minimized and carries an immutable reference', () => {
  const desk = {
    observed_at_utc: '2026-07-31T12:00:00+00:00',
    target_trade_date: '2026-07-31',
    stage: 'research_only',
    selection: {
      status: 'ready',
      session_date: '2026-07-31',
      snapshot_id: 'selection-snapshot-1',
      asof_utc: '2026-07-31T11:59:00+00:00',
      stale: false,
      candidates: [{
        rank: 1,
        symbol: 'AAA',
        rvol: 5.5,
        api_secret: 'must-not-leave-device',
      }],
    },
    review: {
      status: 'ready',
      session_date: '2026-07-30',
      snapshot_id: 'review-snapshot-1',
      stale: true,
      opportunities: [{
        rank: 1,
        symbol: 'BBB',
        close_return: 0.1,
        raw_payload: 'must-not-leave-device',
      }],
    },
    jobs: [{
      job_name: 'postmarket_review',
      trade_date: '2026-07-30',
      status: 'succeeded',
      run_token: 'must-not-leave-device',
    }],
    maturity: { paper_trading_sessions: 2, secret: 'hidden' },
  }

  const compact = compactDeskEvidence(desk)
  const supervisor = agentMessages('supervisor', desk)
  const reference = evidenceReference(desk)
  const serializedCompact = JSON.stringify(compact)
  const serializedSupervisor = JSON.stringify(supervisor)

  assert.equal(compact.jobs, undefined)
  assert.equal(compact.maturity, undefined)
  assert.doesNotMatch(serializedCompact, /must-not-leave-device/)
  assert.match(serializedSupervisor, /postmarket_review/)
  assert.doesNotMatch(serializedSupervisor, /run_token|must-not-leave-device/)
  assert.deepEqual(reference, {
    observedAtUtc: '2026-07-31T12:00:00+00:00',
    targetTradeDate: '2026-07-31',
    selectionSnapshotId: 'selection-snapshot-1',
    selectionAsofUtc: '2026-07-31T11:59:00+00:00',
    selectionSessionDate: '2026-07-31',
    selectionStale: false,
    reviewSnapshotId: 'review-snapshot-1',
    reviewSessionDate: '2026-07-30',
    reviewStale: true,
  })
})
