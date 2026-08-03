const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const test = require('node:test')
const vm = require('node:vm')

const {
  createSecureSettingsStore,
  normalizeExecutionCommand,
  normalizePaperAutopilotCommand,
  parseIbkrProfileText,
  sanitizeExecutionCommandResult,
} = require('../analyst/settings.cjs')
const {
  cleanupOrphanedWindowsRuntimes,
  createLocalRuntimeClient,
  runtimeLaunch,
  terminateRuntimeProcess,
  validateRuntimeHandshake,
} = require('../analyst/local-runtime.cjs')
const {
  RESEARCH_QA_MAX_TOKENS,
  createOpenRouterClient,
} = require('../analyst/openrouter.cjs')
const {
  agentMessages,
  assistantMessages,
  compactDeskEvidence,
  evidenceReference,
} = require('../analyst/prompts.cjs')

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
    massiveApiKey: 'massive-key-value',
    marketDataKey: 'market-key-value',
    marketDataSecret: 'market-secret-value',
    secUserAgent: 'Research User research@example.com',
  })
  store.saveModels(MODELS)

  const publicSettings = store.loadPublic()
  assert.equal(publicSettings.configured, true)
  assert.equal(publicSettings.openRouterKeyConfigured, true)
  assert.equal(publicSettings.marketDataConfigured, true)
  assert.equal(publicSettings.massiveConfigured, true)
  assert.equal(publicSettings.secConfigured, true)
  assert.equal(publicSettings.marketDataProvider, 'standalone_massive_alpaca')
  assert.equal('dataServiceUrl' in publicSettings, false)
  assert.equal('dataAccessTokenConfigured' in publicSettings, false)
  assert.deepEqual(publicSettings.models, MODELS)
  assert.equal(JSON.stringify(publicSettings).includes('openrouter-key-value'), false)

  const persisted = fs.readFileSync(settingsPath, 'utf8')
  assert.equal(persisted.includes('openrouter-key-value'), false)
  assert.equal(persisted.includes('market-key-value'), false)
  assert.equal(persisted.includes('market-secret-value'), false)
  assert.equal(persisted.includes('massive-key-value'), false)
  assert.equal(persisted.includes('research@example.com'), false)
  assert.deepEqual(store.loadSecrets(), {
    openRouterApiKey: 'openrouter-key-value',
    massiveApiKey: 'massive-key-value',
    marketDataKey: 'market-key-value',
    marketDataSecret: 'market-secret-value',
    secUserAgent: 'Research User research@example.com',
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
      massiveApiKey: 'massive-key-value',
      marketDataKey: 'market-key-value',
      marketDataSecret: 'market-secret-value',
      secUserAgent: 'Research User research@example.com',
    }),
    /不会以明文保存/,
  )
  assert.equal(fs.existsSync(settingsPath), false)
})

test('IBKR execution settings support first-use account discovery and encrypted binding', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'analyst-execution-settings-'))
  const settingsPath = path.join(root, 'settings.json')
  const store = createSecureSettingsStore({
    filePath: settingsPath,
    safeStorage: fakeSafeStorage(),
  })

  store.saveExecutionSettings({
    host: '127.0.0.1',
    clientId: 17,
    liveAccount: 'LOGIN-NAME-MUST-BE-IGNORED',
    maxOrderNotional: 25000,
  })

  assert.deepEqual(store.loadExecutionSecrets(), {
    host: '127.0.0.1',
    clientId: 17,
    liveAccount: '',
    port: 4001,
    maxOrderNotional: 25000,
  })
  assert.deepEqual(store.loadPublic().execution, {
    configured: true,
    hostConfigured: true,
    clientIdConfigured: true,
    accountBound: false,
    liveAccountMasked: '',
    maxOrderNotional: 25000,
  })

  store.saveBoundExecutionAccount('U7654321')
  assert.deepEqual(store.loadPublic().execution, {
    configured: true,
    hostConfigured: true,
    clientIdConfigured: true,
    accountBound: true,
    liveAccountMasked: 'U***4321',
    maxOrderNotional: 25000,
  })
  assert.throws(
    () => store.saveBoundExecutionAccount('U0000002'),
    /already bound|宸茬粦瀹?/
  )

  const persisted = fs.readFileSync(settingsPath, 'utf8')
  assert.equal(persisted.includes('127.0.0.1'), false)
  assert.equal(persisted.includes('U7654321'), false)

  store.clearBoundExecutionAccount()
  assert.equal(store.loadPublic().execution.accountBound, false)
  assert.equal(store.loadExecutionSecrets().liveAccount, '')
})

test('IBKR Paper settings use a separate encrypted DU-only 4002 profile', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'analyst-paper-settings-'))
  const settingsPath = path.join(root, 'settings.json')
  const store = createSecureSettingsStore({
    filePath: settingsPath,
    safeStorage: fakeSafeStorage(),
  })

  store.saveExecutionSettings({
    host: '127.0.0.1',
    clientId: 17,
    maxOrderNotional: 25000,
  })
  store.savePaperExecutionSettings({
    host: '192.0.2.44',
    clientId: 91,
    paperAccount: 'DU7654321',
  })

  assert.deepEqual(store.loadPaperExecutionSecrets(), {
    host: '192.0.2.44',
    clientId: 91,
    paperAccount: 'DU7654321',
    port: 4002,
  })
  assert.deepEqual(store.loadPublic().paperExecution, {
    configured: true,
    hostConfigured: true,
    clientIdConfigured: true,
    accountConfigured: true,
    paperAccountMasked: 'DU***4321',
    port: 4002,
  })

  assert.throws(
    () => store.savePaperExecutionSettings({
      host: '192.0.2.44',
      clientId: 91,
      paperAccount: 'U7654321',
    }),
    /DU/,
  )
  const persisted = fs.readFileSync(settingsPath, 'utf8')
  assert.equal(persisted.includes('DU7654321'), false)
  assert.equal(persisted.includes('192.0.2.44'), false)
})

test('execution IPC accepts only explicit long-only command shapes', () => {
  assert.deepEqual(normalizeExecutionCommand({
    kind: 'preview',
    order: {
      client_order_id: 'desktop-123',
      symbol: ' aapl ',
      security_type: 'STK',
      exchange: 'SMART',
      currency: 'USD',
      action: 'OpenLong',
      quantity: 25,
      limit_price: 219.35,
      order_type: 'LMT',
      tif: 'DAY',
    },
    password: 'must-not-cross-ipc',
  }), {
    kind: 'preview',
    order: {
      client_order_id: 'desktop-123',
      symbol: 'AAPL',
      security_type: 'STK',
      exchange: 'SMART',
      currency: 'USD',
      action: 'OpenLong',
      quantity: 25,
      limit_price: 219.35,
      order_type: 'LMT',
      tif: 'DAY',
    },
  })
  assert.deepEqual(normalizeExecutionCommand({ kind: 'connect' }), {
    kind: 'connect',
  })
  assert.deepEqual(normalizeExecutionCommand({ kind: 'disconnect' }), {
    kind: 'disconnect',
  })
  assert.deepEqual(normalizeExecutionCommand({
    kind: 'bind_account',
    confirmation: '绑定实盘账户 U***4321',
  }), {
    kind: 'bind_account',
    confirmation: '绑定实盘账户 U***4321',
  })
  assert.deepEqual(normalizeExecutionCommand({
    kind: 'arm',
    confirmation: '启用实盘 U***4321',
  }), { kind: 'arm', confirmation: '启用实盘 U***4321' })
  assert.deepEqual(normalizeExecutionCommand({ kind: 'disarm' }), {
    kind: 'disarm',
  })
  assert.deepEqual(normalizeExecutionCommand({ kind: 'recover' }), {
    kind: 'recover',
  })
  assert.deepEqual(normalizeExecutionCommand({
    kind: 'submit',
    preview_id: 'preview-123',
    confirmation: '确认提交 AAPL 25',
    order: {
      client_order_id: 'desktop-123',
      symbol: 'AAPL',
      security_type: 'STK',
      exchange: 'SMART',
      currency: 'USD',
      action: 'OpenLong',
      quantity: 25,
      limit_price: 219.35,
      order_type: 'LMT',
      tif: 'DAY',
    },
  }), {
    kind: 'submit',
    preview_id: 'preview-123',
    confirmation: '确认提交 AAPL 25',
    order: {
      client_order_id: 'desktop-123',
      symbol: 'AAPL',
      security_type: 'STK',
      exchange: 'SMART',
      currency: 'USD',
      action: 'OpenLong',
      quantity: 25,
      limit_price: 219.35,
      order_type: 'LMT',
      tif: 'DAY',
    },
  })
  assert.throws(
    () => normalizeExecutionCommand({
      kind: 'preview',
      order: {
        client_order_id: 'desktop-124',
        symbol: 'AAPL',
        security_type: 'STK',
        exchange: 'SMART',
        currency: 'USD',
        action: 'SellShort',
        quantity: 1,
        limit_price: 200,
        order_type: 'LMT',
        tif: 'DAY',
      },
    }),
    /OpenLong.*ReduceLong/,
  )
})

test('Paper autopilot IPC accepts only its fixed lifecycle commands', () => {
  assert.deepEqual(normalizePaperAutopilotCommand({ kind: 'connect' }), {
    kind: 'connect',
  })
  assert.deepEqual(normalizePaperAutopilotCommand({ kind: 'validate_plan' }), {
    kind: 'validate_plan',
  })
  assert.deepEqual(normalizePaperAutopilotCommand({
    kind: 'start',
    confirmation: '启用模拟盘自动执行 DU***4321',
    symbol: 'must-not-cross-ipc',
  }), {
    kind: 'start',
    confirmation: '启用模拟盘自动执行 DU***4321',
  })
  assert.throws(
    () => normalizePaperAutopilotCommand({ kind: 'submit', order: {} }),
    /未知模拟盘自动执行命令/,
  )
})

test('execution receipts never expose full accounts or broker order refs to renderer', () => {
  const safe = sanitizeExecutionCommandResult('submit', {
    schema_version: 'ibkr.execution.v1',
    kind: 'execution_receipt',
    status: 'filled',
    client_order_id: 'desktop-123',
    broker_order_id: 91,
    perm_id: 456,
    account_masked: 'U***4321',
    order_ref: 'vq:live:U7654321:17:desktop-123',
    actual_account_id: 'U7654321',
    account_id: 'U7654321',
    post_submit_snapshot_refreshed: true,
    snapshot_refresh_error: null,
    extra: 'must-not-cross-ipc',
  })

  assert.equal(safe.status, 'filled')
  assert.equal(safe.broker_order_id, 91)
  const serialized = JSON.stringify(safe)
  assert.equal(serialized.includes('U7654321'), false)
  assert.equal(serialized.includes('order_ref'), false)
  assert.equal(serialized.includes('must-not-cross-ipc'), false)
})

test('execution preview keeps bounded What-If evidence and warning confirmation hash', () => {
  const safe = sanitizeExecutionCommandResult('preview', {
    schema_version: 'ibkr.execution.v1',
    kind: 'execution_preview',
    status: 'previewed',
    preview_id: 'preview-abc',
    mode: 'live',
    account_masked: 'U***4321',
    intent: {
      client_order_id: 'desktop-123',
      symbol: 'AAPL',
      security_type: 'STK',
      exchange: 'SMART',
      currency: 'USD',
      action: 'OpenLong',
      quantity: 2,
      limit_price: '200.00',
      order_type: 'LMT',
      tif: 'DAY',
      notional: '400.00',
      account_id: 'U7654321',
    },
    what_if: {
      accepted: true,
      estimated_commission: '1.00',
      initial_margin_change: '200.00',
      warning: 'Warning for U7654321',
    },
    warning_confirmation_hash: 'A1B2C3D4',
    confirmation_phrase: '确认实盘下单 U***4321 警告A1B2C3D4',
    expires_at_utc: '2026-08-03T12:35:00Z',
    order_ref: 'vq:live:U7654321:17:desktop-123',
  })

  assert.equal(safe.warning_confirmation_hash, 'A1B2C3D4')
  assert.equal(safe.what_if.estimated_commission, '1.00')
  assert.equal(safe.what_if.warning.includes('U7654321'), false)
  assert.equal(JSON.stringify(safe).includes('order_ref'), false)
})

test('IBKR profile import ignores login names and only confirms fixed 4001 port', () => {
  const profile = parseIbkrProfileText([
    '实盘账号=U7654321',
    '实盘密码=live-password-must-disappear',
    '实盘端口=4001',
    '虚拟账号=DU1234567',
    '虚拟密码=paper-password-must-disappear',
    '虚拟端口=4002',
    '备注=本地 Gateway',
  ].join('\n'))

  assert.deepEqual(profile, {
    livePort: 4001,
    accountDiscoveryRequired: true,
  })
  assert.equal(JSON.stringify(profile).includes('password'), false)
  assert.equal(JSON.stringify(profile).includes('U7654321'), false)
  assert.equal(JSON.stringify(profile).includes('DU1234567'), false)
})

test('preload exposes isolated execution IPC and main registers matching handlers', async () => {
  const invocations = []
  let exposed
  const preload = fs.readFileSync(
    path.join(__dirname, '..', 'analyst-preload.cjs'),
    'utf8',
  )
  const context = {
    require: (name) => {
      assert.equal(name, 'electron')
      return {
        contextBridge: {
          exposeInMainWorld: (key, value) => { exposed = { key, value } },
        },
        ipcRenderer: {
          invoke: (...args) => {
            invocations.push(args)
            return Promise.resolve({ ok: true })
          },
        },
      }
    },
    Object,
  }
  vm.runInNewContext(preload, context, { filename: 'analyst-preload.cjs' })

  assert.equal(exposed.key, 'analystDesktop')
  await exposed.value.execution.snapshot()
  await exposed.value.execution.command({ kind: 'connect' })
  await exposed.value.paperAutopilot.snapshot()
  await exposed.value.paperAutopilot.command({ kind: 'connect' })
  await exposed.value.settings.saveExecution({ host: '127.0.0.1' })
  await exposed.value.settings.savePaperExecution({ host: '127.0.0.1' })
  await exposed.value.settings.importExecutionProfile()
  await exposed.value.settings.clearExecutionAccountBinding()
  assert.deepEqual(invocations, [
    ['analyst:execution:snapshot'],
    ['analyst:execution:command', { kind: 'connect' }],
    ['analyst:paper-autopilot:snapshot'],
    ['analyst:paper-autopilot:command', { kind: 'connect' }],
    ['analyst:settings:save-execution', { host: '127.0.0.1' }],
    ['analyst:settings:save-paper-execution', { host: '127.0.0.1' }],
    ['analyst:settings:import-execution-profile'],
    ['analyst:settings:clear-execution-account'],
  ])

  const main = fs.readFileSync(
    path.join(__dirname, '..', 'analyst-main.cjs'),
    'utf8',
  )
  assert.match(main, /ipcMain\.handle\('analyst:execution:snapshot'/)
  assert.match(main, /ipcMain\.handle\('analyst:execution:command'/)
  assert.match(main, /ipcMain\.handle\('analyst:paper-autopilot:snapshot'/)
  assert.match(main, /ipcMain\.handle\('analyst:paper-autopilot:command'/)
  assert.match(main, /normalizePaperAutopilotCommand\(command\)/)
  assert.match(main, /analyst:settings:save-paper-execution/)
  assert.match(main, /normalizeExecutionCommand\(command\)/)
  assert.match(main, /saveBoundExecutionAccount/)
  assert.match(main, /actual_account_id/)
  assert.match(main, /delete safeReceipt\.actual_account_id/)
  assert.match(main, /analyst:settings:clear-execution-account/)
  assert.match(main, /recent_orders: executionRowsForRenderer/)
  assert.match(main, /account_refreshed_at_utc/)
  assert.match(main, /execution: sanitizeExecutionSnapshot/)
  assert.match(main, /sanitizeExecutionCommandResult\(normalized\.kind, result\)/)
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
    platform: 'darwin',
    marketData: {
      key: 'market-key-value',
      secret: 'market-secret-value',
      massiveApiKey: 'massive-key-value',
      secUserAgent: 'Research User research@example.com',
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
  assert.equal(launch.args.includes('standalone'), true)
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
    MASSIVE_API_KEY: 'massive-key-value',
    SEC_USER_AGENT: 'Research User research@example.com',
    DESKTOP_MARKET_DATA_PROVIDER: 'alpaca_proxy_rest',
  })
})

test('development macOS runtime reads the repository bootstrap archive', () => {
  const launch = runtimeLaunch({
    app: {
      isPackaged: false,
      getPath: () => '/Users/test/Library/Application Support/AIQuant',
    },
    projectRoot: '/private/tmp/ai-quant-trading-system',
    token: 'ephemeral-runtime-token-value',
    marketData: {},
    platform: 'darwin',
    resourcesPath: '/private/tmp/electron/Electron.app/Contents/Resources',
  })

  const archiveIndex = launch.args.indexOf('--bootstrap-archive')
  assert.notEqual(archiveIndex, -1)
  assert.equal(
    launch.args[archiveIndex + 1],
    path.join(
      '/private/tmp/ai-quant-trading-system',
      'client',
      'build',
      'bootstrap',
      'research-bootstrap.zip',
    ),
  )
})
test('packaged Windows runtime launches the bundled executable without external Python', () => {
  const launch = runtimeLaunch({
    app: {
      isPackaged: true,
      getPath: (name) => {
        assert.equal(name, 'userData')
        return 'C:\\Users\\research\\AppData\\Roaming\\AIQuant'
      },
    },
    projectRoot: 'C:\\unused',
    resourcesPath: 'C:\\Program Files\\AIQuant\\resources',
    token: 'ephemeral-runtime-token-value',
    platform: 'win32',
  })

  assert.equal(
    launch.command,
    path.join(
      'C:\\Program Files\\AIQuant\\resources',
      'runtime',
      'windows-research-runtime.exe',
    ),
  )
  assert.equal(launch.args.includes('unconfigured'), true)
  assert.equal(launch.command.toLowerCase().includes('python'), false)
  assert.equal(launch.args.some((value) => value.includes('.venv')), false)
})

test('research Q&A allows detailed answers up to 8192 tokens', () => {
  assert.equal(RESEARCH_QA_MAX_TOKENS, 8_192)
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

test('OpenRouter completion falls back when the selected model route returns 404', async () => {
  const requestedModels = []
  const client = createOpenRouterClient({
    fetchImpl: async (_url, options = {}) => {
      const model = JSON.parse(options.body).model
      requestedModels.push(model)
      if (model === 'model/unroutable') {
        return jsonResponse({ error: { message: 'no route' } }, { status: 404 })
      }
      return jsonResponse({
        model,
        choices: [{ message: { role: 'assistant', content: '回退模型回答' } }],
        usage: { total_tokens: 9 },
      })
    },
  })

  const completion = await client.complete({
    apiKey: 'secret-key',
    model: 'model/unroutable',
    fallbackModels: ['model/available'],
    messages: [{ role: 'user', content: '测试回退' }],
  })

  assert.deepEqual(requestedModels, ['model/unroutable', 'model/available'])
  assert.equal(completion.content, '回退模型回答')
  assert.equal(completion.model, 'model/available')
  assert.equal(completion.fallbackFrom, 'model/unroutable')
})

test('OpenRouter completion accepts reasoning-only assistant responses', async () => {
  const client = createOpenRouterClient({
    fetchImpl: async () => jsonResponse({
      model: 'model/reasoning',
      choices: [{
        message: {
          role: 'assistant',
          content: null,
          reasoning: '这是模型返回的推理结论',
        },
      }],
      usage: { total_tokens: 12 },
    }),
  })

  const completion = await client.complete({
    apiKey: 'secret-key',
    model: 'model/reasoning',
    messages: [{ role: 'user', content: '测试 reasoning 响应' }],
  })

  assert.equal(completion.content, '这是模型返回的推理结论')
})

test('OpenRouter completion reports truncated responses', async () => {
  const client = createOpenRouterClient({
    fetchImpl: async () => jsonResponse({
      model: 'model/a',
      choices: [{
        finish_reason: 'length',
        message: { role: 'assistant', content: '未完成回答' },
      }],
      usage: { total_tokens: 99 },
    }),
  })
  const completion = await client.complete({
    apiKey: 'secret-key',
    model: 'model/a',
    messages: [{ role: 'user', content: '测试截断' }],
  })
  assert.equal(completion.truncated, true)
  assert.equal(completion.finishReason, 'length')
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
  assert.match(assistant[0].content, /不要复述快照 ID/)
  assert.match(assistant[0].content, /禁止输出 snake_case/)
  assert.match(assistant[0].content, /结论、关键数据、为什么只有它、风险与未知/)
  assert.match(assistant[0].content, /600 个中文字符以内/)
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

test('Windows runtime lifecycle kills stale instances and the full PyInstaller tree', () => {
  const calls = []
  const spawnSyncImpl = (command, args, options) => {
    calls.push({ command, args, options })
    return { status: 0 }
  }
  let directKillCalled = false
  const child = {
    pid: 43210,
    killed: false,
    kill: () => {
      directKillCalled = true
    },
  }

  cleanupOrphanedWindowsRuntimes({
    platform: 'win32',
    packaged: true,
    spawnSyncImpl,
  })
  terminateRuntimeProcess(child, {
    platform: 'win32',
    spawnSyncImpl,
  })

  assert.deepEqual(calls.map((value) => value.command), [
    'taskkill.exe',
    'taskkill.exe',
  ])
  assert.deepEqual(calls[0].args, [
    '/IM',
    'windows-research-runtime.exe',
    '/T',
    '/F',
  ])
  assert.deepEqual(calls[1].args, ['/PID', '43210', '/T', '/F'])
  assert.equal(calls.every((value) => value.options.windowsHide === true), true)
  assert.equal(directKillCalled, false)
})
