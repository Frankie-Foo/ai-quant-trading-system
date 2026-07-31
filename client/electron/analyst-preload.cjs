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
  assistant: Object.freeze({
    ask: (question) => ipcRenderer.invoke('analyst:assistant:ask', { question }),
  }),
  agents: Object.freeze({
    run: (role) => ipcRenderer.invoke('analyst:agents:run', { role }),
  }),
  ibkrPaper: Object.freeze({
    status: () => ipcRenderer.invoke('analyst:ibkr:status'),
  }),
})

contextBridge.exposeInMainWorld('analystDesktop', bridge)
