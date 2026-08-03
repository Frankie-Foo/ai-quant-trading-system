const { app, BrowserWindow, dialog, ipcMain, safeStorage } = require('electron')
const fs = require('node:fs')
const path = require('node:path')

const { createLocalRuntimeProcess } = require('./analyst/local-runtime.cjs')
const {
  RESEARCH_QA_MAX_TOKENS,
  createOpenRouterClient,
} = require('./analyst/openrouter.cjs')
const {
  agentMessages,
  assistantMessages,
  evidenceReference,
} = require('./analyst/prompts.cjs')
const {
  MODEL_KEYS,
  createSecureSettingsStore,
  normalizeExecutionCommand,
  normalizePaperAutopilotCommand,
  normalizeModels,
  parseIbkrProfileText,
  safeResultText,
  sanitizeExecutionCommandResult,
} = require('./analyst/settings.cjs')

const openRouter = createOpenRouterClient()
let settingsStore
let localRuntime
let projectRoot
let runtimeRecovery

function marketDataFromSecrets(secrets) {
  return {
    key: String(secrets?.marketDataKey || '').trim(),
    secret: String(secrets?.marketDataSecret || '').trim(),
    massiveApiKey: String(secrets?.massiveApiKey || '').trim(),
    secUserAgent: String(secrets?.secUserAgent || '').trim(),
    openRouterApiKey: String(secrets?.openRouterApiKey || '').trim(),
  }
}

function runtimeConfiguration(
  connectionSecrets,
  executionSecrets,
  paperExecutionSecrets,
) {
  const models = settingsStore?.loadPublic()?.models || {}
  return {
    ...marketDataFromSecrets(connectionSecrets),
    runtimeModels: {
      catalyst: String(models.catalyst || '').trim(),
      redTeam: String(models.red_team || '').trim(),
    },
    ibkr: {
      ...executionSecrets,
      paper: paperExecutionSecrets,
    },
  }
}

function executionRowsForRenderer(rows, keys) {
  if (!Array.isArray(rows)) return []
  return rows.slice(0, 250).map((row) => {
    if (!row || typeof row !== 'object' || Array.isArray(row)) return {}
    const safe = {}
    for (const key of keys) {
      const value = row[key]
      if (typeof value === 'string') {
        safe[key] = safeResultText(value, 500)
      } else if (['number', 'boolean'].includes(typeof value) || value === null) {
        safe[key] = value
      }
    }
    return safe
  })
}

function sanitizeExecutionSnapshot(snapshot) {
  const source = snapshot && typeof snapshot === 'object' ? snapshot : {}
  return {
    schema_version: String(source.schema_version || 'ibkr.execution.v1'),
    kind: 'execution_snapshot',
    mode: source.mode === 'live' ? 'live' : 'live',
    port: 4001,
    enabled: source.enabled === true,
    connected: source.connected === true,
    account_bound: source.account_bound === true,
    account_masked: safeResultText(source.account_masked, 80) || '',
    binding_confirmation_phrase:
      safeResultText(source.binding_confirmation_phrase, 200),
    api_read_only: typeof source.api_read_only === 'boolean'
      ? source.api_read_only
      : null,
    writes_armed: source.writes_armed === true,
    arm_confirmation_phrase:
      safeResultText(source.arm_confirmation_phrase, 200),
    recovery_required: source.recovery_required === true,
    max_order_notional: Number.isFinite(Number(source.max_order_notional))
      ? Number(source.max_order_notional)
      : null,
    positions: executionRowsForRenderer(source.positions, [
      'symbol',
      'quantity',
      'average_cost',
      'market_price',
      'unrealized_pnl_percent',
    ]),
    open_orders: executionRowsForRenderer(source.open_orders, [
      'order_id',
      'client_order_id',
      'broker_order_id',
      'symbol',
      'action',
      'side',
      'quantity',
      'filled_quantity',
      'limit_price',
      'status',
      'updated_at_utc',
    ]),
    account_refreshed_at_utc:
      safeResultText(source.account_refreshed_at_utc, 50),
    recent_orders: executionRowsForRenderer(source.recent_orders, [
      'client_order_id',
      'broker_order_id',
      'perm_id',
      'symbol',
      'action',
      'quantity',
      'limit_price',
      'status',
      'updated_at_utc',
    ]),
    last_error: safeResultText(source.last_error, 500) || '',
  }
}

function sanitizePaperAutopilotSnapshot(snapshot) {
  const source = snapshot && typeof snapshot === 'object' ? snapshot : {}
  const outcomes = Array.isArray(source.last_outcomes)
    ? source.last_outcomes.slice(0, 20).map((outcome) => {
        const value = outcome && typeof outcome === 'object' ? outcome : {}
        return {
          plan_id: safeResultText(value.plan_id, 120) || '',
          symbol: safeResultText(value.symbol, 15) || '',
          action: safeResultText(value.action, 40) || '',
          reasons: Array.isArray(value.reasons)
            ? value.reasons.slice(0, 12).map((item) => safeResultText(item, 120) || '')
            : [],
          degraded_reasons: Array.isArray(value.degraded_reasons)
            ? value.degraded_reasons.slice(0, 12).map((item) => safeResultText(item, 120) || '')
            : [],
        }
      })
    : []
  return {
    schema_version: 'ibkr.paper_autopilot.v1',
    mode: 'paper',
    port: 4002,
    configured: source.configured === true,
    connected: source.connected === true,
    running: source.running === true,
    paper_writes_armed: source.paper_writes_armed === true,
    account_masked: safeResultText(source.account_masked, 80) || '',
    arm_confirmation_phrase:
      safeResultText(source.arm_confirmation_phrase, 200) || '',
    plan_status: safeResultText(source.plan_status, 40) || 'missing',
    plan_error: safeResultText(source.plan_error, 200) || '',
    plan_symbol: safeResultText(source.plan_symbol, 15) || '',
    safety_refreshed_at_utc:
      safeResultText(source.safety_refreshed_at_utc, 50) || '',
    safety_error: safeResultText(source.safety_error, 200) || '',
    last_tick_at_utc: safeResultText(source.last_tick_at_utc, 50) || '',
    last_outcomes: outcomes,
    last_error: safeResultText(source.last_error, 200) || '',
    audit_event_count: Number.isSafeInteger(source.audit_event_count)
      ? source.audit_event_count
      : 0,
    root_research_orders_authorized: false,
  }
}

function fallbackModels(models, primaryKey) {
  const primary = models[primaryKey]
  return [...new Set(
    MODEL_KEYS
      .filter((key) => key !== primaryKey)
      .map((key) => models[key])
      .filter((model) => model && model !== primary),
  )]
}

async function restartLocalRuntime(marketData = {}) {
  localRuntime?.stop()
  localRuntime = createLocalRuntimeProcess({
    app,
    projectRoot,
    marketData,
  })
  return localRuntime.start()
}

function runtimeUnavailable(error) {
  const message = String(error?.message || '')
  return error?.name === 'TimeoutError'
    || message === 'fetch failed'
    || message.includes('operation was aborted due to timeout')
}

async function recoverLocalRuntime() {
  if (!runtimeRecovery) {
    runtimeRecovery = restartLocalRuntime(runtimeConfiguration(
      settingsStore.loadSecrets(),
      settingsStore.loadExecutionSecrets(),
      settingsStore.loadPaperExecutionSecrets(),
    )).finally(() => { runtimeRecovery = null })
  }
  return runtimeRecovery
}

async function readLocalRuntime(read) {
  try {
    return await read(localRuntime.client)
  } catch (error) {
    if (!runtimeUnavailable(error)) throw error
    await recoverLocalRuntime()
    return read(localRuntime.client)
  }
}

function requireConfiguredSettings() {
  const settings = settingsStore.loadPublic()
  if (!settings.configured) throw new Error('请先完成首次配置')
  return settings
}

const fetchDesk = () => readLocalRuntime((client) => client.fetchDesk())

function registerHandlers() {
  ipcMain.handle('analyst:settings:get', () => settingsStore.loadPublic())

  ipcMain.handle(
    'analyst:settings:validate-connection',
    async (_event, connection) => {
      const candidate = {
        openRouterApiKey: String(connection?.openRouterApiKey || '').trim(),
        massiveApiKey: String(connection?.massiveApiKey || '').trim(),
        marketDataKey: String(connection?.marketDataKey || '').trim(),
        marketDataSecret: String(connection?.marketDataSecret || '').trim(),
        secUserAgent: String(connection?.secUserAgent || '').trim(),
      }
      const models = await openRouter.listModels(candidate.openRouterApiKey)
      const previousSecrets = settingsStore.loadSecrets()
      const executionSecrets = settingsStore.loadExecutionSecrets()
      const paperExecutionSecrets = settingsStore.loadPaperExecutionSecrets()
      let runtime
      try {
        runtime = await restartLocalRuntime(runtimeConfiguration(
          candidate,
          executionSecrets,
          paperExecutionSecrets,
        ))
        if (runtime?.market_data?.healthy !== true) {
          throw new Error(
            `Alpaca 代理行情验证失败：${runtime?.market_data?.reason || 'unknown'}`,
          )
        }
        settingsStore.saveConnectionSecrets(candidate)
      } catch (error) {
        await restartLocalRuntime(runtimeConfiguration(
          previousSecrets,
          executionSecrets,
          paperExecutionSecrets,
        ))
        throw error
      }
      return {
        settings: settingsStore.loadPublic(),
        models,
        runtime,
      }
    },
  )

  ipcMain.handle('analyst:settings:save-models', async (_event, values) => {
    const models = normalizeModels(values)
    const secrets = settingsStore.loadSecrets()
    const available = await openRouter.listModels(secrets.openRouterApiKey)
    const availableIds = new Set(available.map((model) => model.id))
    for (const key of MODEL_KEYS) {
      if (!availableIds.has(models[key])) {
        throw new Error(`模型当前不可用：${models[key]}`)
      }
    }
    settingsStore.saveModels(models)
    await restartLocalRuntime(runtimeConfiguration(
      settingsStore.loadSecrets(),
      settingsStore.loadExecutionSecrets(),
      settingsStore.loadPaperExecutionSecrets(),
    ))
    return settingsStore.loadPublic()
  })

  ipcMain.handle('analyst:settings:save-execution', async (_event, values) => {
    settingsStore.saveExecutionSettings(values)
    const runtime = await restartLocalRuntime(runtimeConfiguration(
      settingsStore.loadSecrets(),
      settingsStore.loadExecutionSecrets(),
      settingsStore.loadPaperExecutionSecrets(),
    ))
    return {
      settings: settingsStore.loadPublic(),
      execution: sanitizeExecutionSnapshot(
        await localRuntime.client.executionSnapshot(),
      ),
      runtime,
    }
  })

  ipcMain.handle('analyst:settings:save-paper-execution', async (_event, values) => {
    settingsStore.savePaperExecutionSettings(values)
    const runtime = await restartLocalRuntime(runtimeConfiguration(
      settingsStore.loadSecrets(),
      settingsStore.loadExecutionSecrets(),
      settingsStore.loadPaperExecutionSecrets(),
    ))
    return {
      settings: settingsStore.loadPublic(),
      paper_autopilot: sanitizePaperAutopilotSnapshot(
        await localRuntime.client.paperAutopilotSnapshot(),
      ),
      runtime,
    }
  })

  ipcMain.handle('analyst:settings:import-execution-profile', async () => {
    const selected = await dialog.showOpenDialog({
      title: '选择盈透配置文件',
      properties: ['openFile'],
      filters: [
        { name: '配置文件', extensions: ['env', 'txt'] },
        { name: '所有文件', extensions: ['*'] },
      ],
    })
    if (selected.canceled || !selected.filePaths[0]) return { canceled: true }
    const filePath = selected.filePaths[0]
    if (fs.statSync(filePath).size > 64 * 1024) {
      throw new Error('盈透配置文件过大')
    }
    return {
      canceled: false,
      profile: parseIbkrProfileText(fs.readFileSync(filePath, 'utf8')),
    }
  })

  ipcMain.handle('analyst:settings:clear-execution-account', async () => {
    settingsStore.clearBoundExecutionAccount()
    const runtime = await restartLocalRuntime(runtimeConfiguration(
      settingsStore.loadSecrets(),
      settingsStore.loadExecutionSecrets(),
      settingsStore.loadPaperExecutionSecrets(),
    ))
    return {
      settings: settingsStore.loadPublic(),
      execution: sanitizeExecutionSnapshot(
        await localRuntime.client.executionSnapshot(),
      ),
      runtime,
    }
  })

  ipcMain.handle('analyst:settings:clear', async () => {
    settingsStore.clear()
    await restartLocalRuntime()
    return settingsStore.loadPublic()
  })

  ipcMain.handle('analyst:models:list', async () => {
    const secrets = settingsStore.loadSecrets()
    return openRouter.listModels(secrets.openRouterApiKey)
  })

  ipcMain.handle('analyst:desk:get', () => fetchDesk())
  ipcMain.handle('analyst:runtime:status', () => (
    readLocalRuntime((client) => client.status())
  ))
  ipcMain.handle('analyst:runtime:run-due', () => localRuntime.client.runDue())
  ipcMain.handle('analyst:workflows:status', () => (
    readLocalRuntime((client) => client.workflowStatus())
  ))
  ipcMain.handle('analyst:workflows:start', (_event, payload) => (
    localRuntime.client.startWorkflow(
      String(payload?.action || ''),
      String(payload?.tradeDate || ''),
    )
  ))
  ipcMain.handle('analyst:monitor:start', (_event, payload) => (
    localRuntime.client.startMonitor(String(payload?.tradeDate || ''))
  ))
  ipcMain.handle('analyst:monitor:stop', () => (
    localRuntime.client.stopMonitor()
  ))
  ipcMain.handle('analyst:execution:snapshot', async () => (
    sanitizeExecutionSnapshot(
      await readLocalRuntime((client) => client.executionSnapshot()),
    )
  ))
  ipcMain.handle('analyst:execution:command', async (_event, command) => {
    const normalized = normalizeExecutionCommand(command)
    const result = await localRuntime.client.executionCommand(normalized)
    if (['connect', 'disconnect', 'arm', 'disarm', 'recover'].includes(
      normalized.kind,
    )) {
      return sanitizeExecutionSnapshot(result)
    }
    if (['preview', 'submit'].includes(normalized.kind)) {
      return sanitizeExecutionCommandResult(normalized.kind, result)
    }
    if (normalized.kind !== 'bind_account') {
      throw new Error('未知的执行结果类型')
    }

    const actualAccountId = String(result?.actual_account_id || '')
      .trim()
      .toUpperCase()
    settingsStore.saveBoundExecutionAccount(actualAccountId)

    const safeReceipt = { ...result }
    delete safeReceipt.actual_account_id
    await restartLocalRuntime(runtimeConfiguration(
      settingsStore.loadSecrets(),
      settingsStore.loadExecutionSecrets(),
      settingsStore.loadPaperExecutionSecrets(),
    ))
    const execution = sanitizeExecutionSnapshot(
      await localRuntime.client.executionSnapshot(),
    )
    return {
      schema_version: String(safeReceipt.schema_version || 'ibkr.execution.v1'),
      kind: 'account_binding_receipt',
      account_bound: safeReceipt.account_bound === true,
      account_masked: safeResultText(safeReceipt.account_masked, 80)
        || execution.account_masked,
      settings: settingsStore.loadPublic(),
      execution,
    }
  })

  ipcMain.handle('analyst:paper-autopilot:snapshot', async () => (
    sanitizePaperAutopilotSnapshot(
      await readLocalRuntime((client) => client.paperAutopilotSnapshot()),
    )
  ))
  ipcMain.handle('analyst:paper-autopilot:command', async (_event, command) => {
    const normalized = normalizePaperAutopilotCommand(command)
    return sanitizePaperAutopilotSnapshot(
      await localRuntime.client.paperAutopilotCommand(normalized),
    )
  })

  ipcMain.handle('analyst:assistant:ask', async (_event, payload) => {
    const settings = requireConfiguredSettings()
    const secrets = settingsStore.loadSecrets()
    const desk = await fetchDesk()
    const result = await openRouter.complete({
      apiKey: secrets.openRouterApiKey,
      model: settings.models.question,
      fallbackModels: fallbackModels(settings.models, 'question'),
      messages: assistantMessages(payload?.question, desk),
      maxTokens: RESEARCH_QA_MAX_TOKENS,
      temperature: 0.1,
    })
    return { ...result, evidence: evidenceReference(desk) }
  })

  ipcMain.handle('analyst:agents:run', async (_event, payload) => {
    const role = String(payload?.role || '')
    if (!['catalyst', 'red_team', 'supervisor'].includes(role)) {
      throw new Error('未知 Agent 角色')
    }
    const settings = requireConfiguredSettings()
    const secrets = settingsStore.loadSecrets()
    const desk = await fetchDesk()
    const result = await openRouter.complete({
      apiKey: secrets.openRouterApiKey,
      model: settings.models[role],
      fallbackModels: fallbackModels(settings.models, role),
      messages: agentMessages(role, desk),
    })
    return { ...result, evidence: evidenceReference(desk) }
  })

}

async function createWindow() {
  const window = new BrowserWindow({
    width: 1440,
    height: 930,
    minWidth: 1080,
    minHeight: 720,
    backgroundColor: '#080c0e',
    autoHideMenuBar: true,
    title: 'AI 量化研究台',
    webPreferences: {
      preload: path.join(__dirname, 'analyst-preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  })
  window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }))
  window.webContents.on('will-navigate', (event, url) => {
    if (url !== window.webContents.getURL()) event.preventDefault()
  })
  window.webContents.session.setPermissionRequestHandler(
    (_webContents, _permission, callback) => callback(false),
  )
  await window.loadFile(path.join(__dirname, '..', 'dist', 'index.html'))
}

app.whenReady().then(async () => {
  projectRoot = path.resolve(__dirname, '..', '..')
  settingsStore = createSecureSettingsStore({
    filePath: path.join(app.getPath('userData'), 'research-settings.json'),
    safeStorage,
  })
  await restartLocalRuntime(runtimeConfiguration(
    settingsStore.loadSecrets(),
    settingsStore.loadExecutionSecrets(),
    settingsStore.loadPaperExecutionSecrets(),
  ))
  registerHandlers()
  await createWindow()
}).catch((error) => {
  console.error(error.message)
  app.quit()
})

app.on('window-all-closed', () => {
  localRuntime?.stop()
  app.quit()
})
