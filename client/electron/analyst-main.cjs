const { app, BrowserWindow, ipcMain, safeStorage } = require('electron')
const fs = require('node:fs')
const path = require('node:path')

const { createIbkrPaperAdapter } = require('./analyst/ibkr-paper.cjs')
const { createOpenRouterClient } = require('./analyst/openrouter.cjs')
const { agentMessages, assistantMessages } = require('./analyst/prompts.cjs')
const { createResearchDataClient } = require('./analyst/research-data.cjs')
const {
  MODEL_KEYS,
  createSecureSettingsStore,
  normalizeModels,
  validateServiceUrl,
} = require('./analyst/settings.cjs')

const openRouter = createOpenRouterClient()
const researchData = createResearchDataClient()
const ibkrPaper = createIbkrPaperAdapter()
let settingsStore
let distribution = { defaultDataServiceUrl: '' }

function loadDistributionConfig() {
  const pathFromApp = path.join(app.getAppPath(), 'analyst-distribution.json')
  try {
    const payload = JSON.parse(fs.readFileSync(pathFromApp, 'utf8'))
    if (
      payload?.schema_version !== 'macos-research-distribution.v1'
      || !payload.default_data_service_url
    ) {
      return { defaultDataServiceUrl: '' }
    }
    return {
      defaultDataServiceUrl: validateServiceUrl(
        payload.default_data_service_url,
      ),
    }
  } catch {
    return { defaultDataServiceUrl: '' }
  }
}

function requireConfiguredSettings() {
  const settings = settingsStore.loadPublic()
  if (!settings.configured) throw new Error('请先完成首次配置')
  return settings
}

async function fetchDesk() {
  const settings = settingsStore.loadPublic()
  const secrets = settingsStore.loadSecrets()
  if (!settings.dataServiceUrl) throw new Error('研究数据服务尚未配置')
  return researchData.fetchDesk({
    baseUrl: settings.dataServiceUrl,
    accessToken: secrets.dataAccessToken,
  })
}

function registerHandlers() {
  ipcMain.handle('analyst:settings:get', () => ({
    ...settingsStore.loadPublic(),
    ...distribution,
  }))

  ipcMain.handle(
    'analyst:settings:validate-connection',
    async (_event, connection) => {
      const candidate = {
        dataServiceUrl: String(connection?.dataServiceUrl || '').trim(),
        dataAccessToken: String(connection?.dataAccessToken || '').trim(),
        openRouterApiKey: String(connection?.openRouterApiKey || '').trim(),
      }
      const [models, desk] = await Promise.all([
        openRouter.listModels(candidate.openRouterApiKey),
        researchData.fetchDesk({
          baseUrl: candidate.dataServiceUrl,
          accessToken: candidate.dataAccessToken,
        }),
      ])
      settingsStore.saveConnection(candidate)
      return {
        settings: settingsStore.loadPublic(),
        models,
        desk: {
          targetTradeDate: desk.target_trade_date,
          pipelineStatus: desk.pipeline_status,
        },
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

  ipcMain.handle('analyst:settings:clear', () => {
    settingsStore.clear()
    return {
      ...settingsStore.loadPublic(),
      ...distribution,
    }
  })

  ipcMain.handle('analyst:models:list', async () => {
    const secrets = settingsStore.loadSecrets()
    return openRouter.listModels(secrets.openRouterApiKey)
  })

  ipcMain.handle('analyst:desk:get', () => fetchDesk())

  ipcMain.handle('analyst:assistant:ask', async (_event, payload) => {
    const settings = requireConfiguredSettings()
    const secrets = settingsStore.loadSecrets()
    const desk = await fetchDesk()
    return openRouter.complete({
      apiKey: secrets.openRouterApiKey,
      model: settings.models.question,
      messages: assistantMessages(payload?.question, desk),
    })
  })

  ipcMain.handle('analyst:agents:run', async (_event, payload) => {
    const role = String(payload?.role || '')
    if (!['catalyst', 'red_team', 'supervisor'].includes(role)) {
      throw new Error('未知 Agent 角色')
    }
    const settings = requireConfiguredSettings()
    const secrets = settingsStore.loadSecrets()
    const desk = await fetchDesk()
    return openRouter.complete({
      apiKey: secrets.openRouterApiKey,
      model: settings.models[role],
      messages: agentMessages(role, desk),
    })
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
  settingsStore = createSecureSettingsStore({
    filePath: path.join(app.getPath('userData'), 'research-settings.json'),
    safeStorage,
  })
  distribution = loadDistributionConfig()
  registerHandlers()
  await createWindow()
}).catch((error) => {
  console.error(error.message)
  app.quit()
})

app.on('window-all-closed', () => {
  app.quit()
})
