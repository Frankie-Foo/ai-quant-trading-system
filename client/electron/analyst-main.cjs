const { app, BrowserWindow, ipcMain, safeStorage } = require('electron')
const path = require('node:path')

const { createIbkrPaperAdapter } = require('./analyst/ibkr-paper.cjs')
const { createLocalRuntimeProcess } = require('./analyst/local-runtime.cjs')
const { createOpenRouterClient } = require('./analyst/openrouter.cjs')
const {
  agentMessages,
  assistantMessages,
  evidenceReference,
} = require('./analyst/prompts.cjs')
const {
  MODEL_KEYS,
  createSecureSettingsStore,
  normalizeModels,
} = require('./analyst/settings.cjs')

const openRouter = createOpenRouterClient()
const ibkrPaper = createIbkrPaperAdapter()
let settingsStore
let localRuntime
let projectRoot

function marketDataFromSecrets(secrets) {
  return {
    key: String(secrets?.marketDataKey || '').trim(),
    secret: String(secrets?.marketDataSecret || '').trim(),
  }
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

function requireConfiguredSettings() {
  const settings = settingsStore.loadPublic()
  if (!settings.configured) throw new Error('请先完成首次配置')
  return settings
}

const fetchDesk = () => localRuntime.client.fetchDesk()

function registerHandlers() {
  ipcMain.handle('analyst:settings:get', () => settingsStore.loadPublic())

  ipcMain.handle(
    'analyst:settings:validate-connection',
    async (_event, connection) => {
      const candidate = {
        openRouterApiKey: String(connection?.openRouterApiKey || '').trim(),
        marketDataKey: String(connection?.marketDataKey || '').trim(),
        marketDataSecret: String(connection?.marketDataSecret || '').trim(),
      }
      const models = await openRouter.listModels(candidate.openRouterApiKey)
      const previousSecrets = settingsStore.loadSecrets()
      let runtime
      try {
        runtime = await restartLocalRuntime({
          key: candidate.marketDataKey,
          secret: candidate.marketDataSecret,
        })
        if (runtime?.market_data?.healthy !== true) {
          throw new Error(
            `Alpaca 代理行情验证失败：${runtime?.market_data?.reason || 'unknown'}`,
          )
        }
        settingsStore.saveConnectionSecrets(candidate)
      } catch (error) {
        await restartLocalRuntime(marketDataFromSecrets(previousSecrets))
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
    return settingsStore.loadPublic()
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
  ipcMain.handle('analyst:runtime:status', () => localRuntime.client.status())
  ipcMain.handle('analyst:runtime:run-due', () => localRuntime.client.runDue())

  ipcMain.handle('analyst:assistant:ask', async (_event, payload) => {
    const settings = requireConfiguredSettings()
    const secrets = settingsStore.loadSecrets()
    const desk = await fetchDesk()
    const result = await openRouter.complete({
      apiKey: secrets.openRouterApiKey,
      model: settings.models.question,
      messages: assistantMessages(payload?.question, desk),
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
      messages: agentMessages(role, desk),
    })
    return { ...result, evidence: evidenceReference(desk) }
  })

  ipcMain.handle('analyst:ibkr:status', () => ibkrPaper.status())
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
  await restartLocalRuntime(marketDataFromSecrets(settingsStore.loadSecrets()))
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
