const { contextBridge, ipcRenderer } = require('electron')

const bridge = Object.freeze({
  edition: 'macos-research',
  settings: Object.freeze({
    get: () => ipcRenderer.invoke('analyst:settings:get'),
    validateConnection: (connection) => (
      ipcRenderer.invoke('analyst:settings:validate-connection', connection)
    ),
    saveModels: (models) => (
      ipcRenderer.invoke('analyst:settings:save-models', models)
    ),
    saveExecution: (settings) => (
      ipcRenderer.invoke('analyst:settings:save-execution', settings)
    ),
    importExecutionProfile: () => (
      ipcRenderer.invoke('analyst:settings:import-execution-profile')
    ),
    clearExecutionAccountBinding: () => (
      ipcRenderer.invoke('analyst:settings:clear-execution-account')
    ),
    clear: () => ipcRenderer.invoke('analyst:settings:clear'),
  }),
  models: Object.freeze({
    list: () => ipcRenderer.invoke('analyst:models:list'),
  }),
  desk: Object.freeze({
    get: () => ipcRenderer.invoke('analyst:desk:get'),
  }),
  runtime: Object.freeze({
    status: () => ipcRenderer.invoke('analyst:runtime:status'),
    runDue: () => ipcRenderer.invoke('analyst:runtime:run-due'),
  }),
  workflows: Object.freeze({
    status: () => ipcRenderer.invoke('analyst:workflows:status'),
    start: (action, tradeDate) => (
      ipcRenderer.invoke('analyst:workflows:start', { action, tradeDate })
    ),
  }),
  monitor: Object.freeze({
    start: (tradeDate) => (
      ipcRenderer.invoke('analyst:monitor:start', { tradeDate })
    ),
    stop: () => ipcRenderer.invoke('analyst:monitor:stop'),
  }),
  execution: Object.freeze({
    snapshot: () => ipcRenderer.invoke('analyst:execution:snapshot'),
    command: (command) => (
      ipcRenderer.invoke('analyst:execution:command', command)
    ),
  }),
  assistant: Object.freeze({
    ask: (question) => ipcRenderer.invoke('analyst:assistant:ask', { question }),
  }),
  agents: Object.freeze({
    run: (role) => ipcRenderer.invoke('analyst:agents:run', { role }),
  }),
})

contextBridge.exposeInMainWorld('analystDesktop', bridge)
