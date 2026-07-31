const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const test = require('node:test')

const {
  createSecureSettingsStore,
  validateServiceUrl,
} = require('../analyst/settings.cjs')
const { createOpenRouterClient } = require('../analyst/openrouter.cjs')
const {
  agentMessages,
  assistantMessages,
  compactDeskEvidence,
} = require('../analyst/prompts.cjs')
const { createResearchDataClient } = require('../analyst/research-data.cjs')
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

test('secure settings persist secrets encrypted and expose only redacted state', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'analyst-settings-'))
  const settingsPath = path.join(root, 'settings.json')
  const store = createSecureSettingsStore({
    filePath: settingsPath,
    safeStorage: fakeSafeStorage(),
  })

  store.saveConnection({
    dataServiceUrl: 'https://research.example.com',
    dataAccessToken: 'data-token-value',
    openRouterApiKey: 'openrouter-key-value',
  })
  store.saveModels(MODELS)

  const publicSettings = store.loadPublic()
  assert.equal(publicSettings.configured, true)
  assert.equal(publicSettings.openRouterKeyConfigured, true)
  assert.equal(publicSettings.dataAccessTokenConfigured, true)
  assert.deepEqual(publicSettings.models, MODELS)
  assert.equal(JSON.stringify(publicSettings).includes('openrouter-key-value'), false)
  assert.equal(JSON.stringify(publicSettings).includes('data-token-value'), false)

  const persisted = fs.readFileSync(settingsPath, 'utf8')
  assert.equal(persisted.includes('openrouter-key-value'), false)
  assert.equal(persisted.includes('data-token-value'), false)
  assert.deepEqual(store.loadSecrets(), {
    dataAccessToken: 'data-token-value',
    openRouterApiKey: 'openrouter-key-value',
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
    () => store.saveConnection({
      dataServiceUrl: 'https://research.example.com',
      dataAccessToken: 'data-token-value',
      openRouterApiKey: 'openrouter-key-value',
    }),
    /不会以明文保存/,
  )
  assert.equal(fs.existsSync(settingsPath), false)
})

test('remote data services require HTTPS while loopback remains available for QA', () => {
  assert.equal(validateServiceUrl('https://research.example.com'), 'https://research.example.com')
  assert.equal(validateServiceUrl('http://127.0.0.1:8787/'), 'http://127.0.0.1:8787')
  assert.throws(
    () => validateServiceUrl('http://research.example.com'),
    /HTTPS/,
  )
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

test('research data client requires read-only evidence and sends optional bearer token', async () => {
  const requests = []
  const client = createResearchDataClient({
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
      })
    },
  })

  const desk = await client.fetchDesk({
    baseUrl: 'https://research.example.com/',
    accessToken: 'read-only-token',
  })

  assert.equal(desk.orders_authorized, false)
  assert.equal(requests[0].url, 'https://research.example.com/v1/desk')
  assert.equal(requests[0].options.headers.Authorization, 'Bearer read-only-token')
})

test('research data client rejects a remotely executable payload', async () => {
  const client = createResearchDataClient({
    fetchImpl: async () => jsonResponse({
      schema_version: 'trading_desk_evidence.v1',
      stage: 'paper',
      orders_authorized: true,
      selection: {},
      review: {},
    }),
  })

  await assert.rejects(
    client.fetchDesk({
      baseUrl: 'https://research.example.com',
      accessToken: '',
    }),
    /non-executable/,
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
